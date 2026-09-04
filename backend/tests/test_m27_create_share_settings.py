"""M27 Task 1: 创建分享限流配置键 rate_limit_create_share（后端全链路）。

- fresh bootstrap 后 GET /api/config 含 rate_limit_create_share 默认 10
- PUT 合法值（1-10000）可改写并持久化，越界值 422 且中文错误
- PUT 后热更新 rate_limit_config.create_share 并对 POST /api/shares 生效
- AC-5 计费语义：成功扣减、业务失败不退费、被拒不重复扣、未认证不进桶
- v16 库升级 v17 自动补列默认 10，迁移幂等
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import settings
from app.core.rate_limit import api_limiter
from app.core.rate_limit_config import rate_limit_config
from app.db.bootstrap import SCHEMA_VERSION, bootstrap_database
from app.db.engine import dispose_engine, get_engine, reset_engine
from app.db.migrations import migrate_v17, run_migrations
from app.main import app
from app.services.settings_service import load_runtime_config
from tests.helpers_v0 import create_session_v0, create_user_file_v0, create_user_v0


def _admin_client(session_id: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set(settings.session_cookie_name, session_id)
    return client


@pytest.mark.asyncio
async def test_get_config_contains_create_share_default(temp_db: str) -> None:
    admin = await create_user_v0(username="m27-default-admin", is_admin=True)
    session = await create_session_v0(admin["id"], "m27-default-sess")

    client = _admin_client(session)
    resp = client.get("/api/config")
    assert resp.status_code == 200
    assert resp.json()["rate_limit_create_share"] == 10


@pytest.mark.asyncio
async def test_put_accepts_boundary_values(temp_db: str) -> None:
    admin = await create_user_v0(username="m27-boundary-admin", is_admin=True)
    session = await create_session_v0(admin["id"], "m27-boundary-sess")

    client = _admin_client(session)
    for value in (1, 10000):
        put_resp = client.put("/api/config", json={"rate_limit_create_share": value})
        assert put_resp.status_code == 200
        assert put_resp.json()["rate_limit_create_share"] == value

        get_resp = client.get("/api/config")
        assert get_resp.status_code == 200
        assert get_resp.json()["rate_limit_create_share"] == value


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_value", [-1, 0, 10001])
async def test_put_rejects_out_of_range_with_chinese_error(
    temp_db: str, invalid_value: int
) -> None:
    admin = await create_user_v0(
        username=f"m27-reject-admin-{invalid_value}", is_admin=True
    )
    session = await create_session_v0(admin["id"], f"m27-reject-sess-{invalid_value}")

    client = _admin_client(session)
    put_resp = client.put("/api/config", json={"rate_limit_create_share": invalid_value})
    assert put_resp.status_code == 422
    assert "创建分享限流必须在 1 到 10000 之间" in put_resp.text

    get_resp = client.get("/api/config")
    assert get_resp.json()["rate_limit_create_share"] == 10


@pytest.mark.asyncio
async def test_put_hot_reloads_runtime_config(temp_db: str) -> None:
    admin = await create_user_v0(username="m27-hotreload-admin", is_admin=True)
    session = await create_session_v0(admin["id"], "m27-hotreload-sess")

    client = _admin_client(session)
    original = rate_limit_config.create_share
    try:
        put_resp = client.put("/api/config", json={"rate_limit_create_share": 3})
        assert put_resp.status_code == 200
        assert rate_limit_config.create_share == 3
    finally:
        rate_limit_config.create_share = original


@pytest.mark.asyncio
async def test_create_share_endpoint_enforces_new_limit(
    authenticated_client: TestClient,
    test_user: dict,
    user_file: dict,
    temp_db: str,
) -> None:
    original = rate_limit_config.create_share
    rate_limit_config.create_share = 3
    user_id = test_user["id"]
    try:
        for _ in range(3):
            resp = authenticated_client.post(
                "/api/shares", json={"user_file_id": user_file["id"]}
            )
            assert resp.status_code == 201

        blocked = authenticated_client.post(
            "/api/shares", json={"user_file_id": user_file["id"]}
        )
        assert blocked.status_code == 429
        assert blocked.json()["detail"] == "创建分享过于频繁，请稍后再试"

        remaining_after_block = await api_limiter.get_remaining(
            user_id, "create_share", 3, 60
        )
        assert remaining_after_block == 0
        again = authenticated_client.post(
            "/api/shares", json={"user_file_id": user_file["id"]}
        )
        assert again.status_code == 429
        assert (
            await api_limiter.get_remaining(user_id, "create_share", 3, 60)
            == remaining_after_block
        )

        other_user = await create_user_v0(username="m27-isolated-user-b")
        b_path = Path(settings.download_dir) / "store" / "m27_user_b.bin"
        b_path.parent.mkdir(parents=True, exist_ok=True)
        b_path.write_bytes(b"b" * 16)
        b_file = await create_user_file_v0(
            user_id=other_user["id"],
            real_path=b_path,
            content_hash="m27_user_b_hash",
            display_name="m27_user_b.bin",
            size_bytes=16,
        )
        b_session = await create_session_v0(other_user["id"], "m27-user-b-sess")
        b_client = TestClient(app)
        b_client.cookies.set(settings.session_cookie_name, b_session)

        b_resp = b_client.post("/api/shares", json={"user_file_id": b_file["id"]})
        assert b_resp.status_code == 201

        assert rate_limit_config.window_for("create_share") == 60
    finally:
        rate_limit_config.create_share = original


@pytest.mark.asyncio
async def test_successful_create_charges_once(
    authenticated_client: TestClient,
    test_user: dict,
    user_file: dict,
    temp_db: str,
) -> None:
    original = rate_limit_config.create_share
    rate_limit_config.create_share = 2
    try:
        first = authenticated_client.post(
            "/api/shares", json={"user_file_id": user_file["id"]}
        )
        second = authenticated_client.post(
            "/api/shares", json={"user_file_id": user_file["id"]}
        )
        third = authenticated_client.post(
            "/api/shares", json={"user_file_id": user_file["id"]}
        )

        assert first.status_code == 201
        assert second.status_code == 201
        assert third.status_code == 429
        assert (
            await api_limiter.get_remaining(test_user["id"], "create_share", 2, 60)
            == 0
        )
    finally:
        rate_limit_config.create_share = original


@pytest.mark.asyncio
async def test_business_failure_keeps_charge(
    authenticated_client: TestClient,
    test_user: dict,
    temp_db: str,
) -> None:
    original = rate_limit_config.create_share
    rate_limit_config.create_share = 5
    try:
        resp = authenticated_client.post(
            "/api/shares", json={"user_file_id": 999999}
        )
        assert resp.status_code == 404

        remaining = await api_limiter.get_remaining(
            test_user["id"], "create_share", 5, 60
        )
        assert remaining == 4
    finally:
        rate_limit_config.create_share = original


@pytest.mark.asyncio
async def test_rate_limited_request_does_not_overcharge(
    authenticated_client: TestClient,
    test_user: dict,
    user_file: dict,
    temp_db: str,
) -> None:
    original = rate_limit_config.create_share
    rate_limit_config.create_share = 1
    try:
        first = authenticated_client.post(
            "/api/shares", json={"user_file_id": user_file["id"]}
        )
        assert first.status_code == 201
        assert (
            await api_limiter.get_remaining(test_user["id"], "create_share", 1, 60)
            == 0
        )

        for _ in range(2):
            blocked = authenticated_client.post(
                "/api/shares", json={"user_file_id": user_file["id"]}
            )
            assert blocked.status_code == 429
            assert (
                await api_limiter.get_remaining(test_user["id"], "create_share", 1, 60)
                == 0
            )
    finally:
        rate_limit_config.create_share = original


@pytest.mark.asyncio
async def test_unauthenticated_request_skips_create_share_bucket(
    test_user: dict,
    temp_db: str,
) -> None:
    original = rate_limit_config.create_share
    rate_limit_config.create_share = 5
    try:
        baseline = await api_limiter.get_remaining(
            test_user["id"], "create_share", 5, 60
        )
        assert baseline == 5

        anon_client = TestClient(app)
        resp = anon_client.post("/api/shares", json={"user_file_id": 1})
        assert resp.status_code == 401

        remaining = await api_limiter.get_remaining(
            test_user["id"], "create_share", 5, 60
        )
        assert remaining == baseline
    finally:
        rate_limit_config.create_share = original


@pytest.mark.asyncio
async def test_create_share_limit_is_coerced_as_int_config_column() -> None:
    from app.services.settings_service import coerce_raw_config_value

    assert coerce_raw_config_value("rate_limit_create_share", "30") == 30


@pytest.mark.asyncio
async def test_config_survives_reload_boundary(temp_db: str) -> None:
    admin = await create_user_v0(username="m27-reload-admin", is_admin=True)
    session = await create_session_v0(admin["id"], "m27-reload-sess")

    client = _admin_client(session)
    put_resp = client.put("/api/config", json={"rate_limit_create_share": 30})
    assert put_resp.status_code == 200
    try:
        rate_limit_config.create_share = 10

        await load_runtime_config()

        assert rate_limit_config.create_share == 30
    finally:
        rate_limit_config.create_share = 10


@pytest.mark.asyncio
async def test_v16_to_v17_adds_create_share_column(tmp_path: Path) -> None:
    original_db = settings.database_path
    settings.database_path = str(tmp_path / "m27-upgrade.db")
    reset_engine()
    try:
        await bootstrap_database()
        async with get_engine().begin() as conn:
            await conn.execute(
                text("ALTER TABLE app_settings DROP COLUMN rate_limit_create_share")
            )
            await conn.execute(
                text("UPDATE schema_meta SET version = 16 WHERE id = 1")
            )

        async with get_engine().begin() as conn:
            assert await run_migrations(conn, 16) == SCHEMA_VERSION

            columns = {
                row[1]
                for row in (
                    await conn.execute(text("PRAGMA table_info(app_settings)"))
                ).all()
            }
            assert "rate_limit_create_share" in columns
            value = (
                await conn.execute(
                    text(
                        "SELECT rate_limit_create_share FROM app_settings WHERE id = 1"
                    )
                )
            ).scalar_one()
            assert value == 10
            version = (
                await conn.execute(
                    text("SELECT version FROM schema_meta WHERE id = 1")
                )
            ).scalar_one()
            assert version == SCHEMA_VERSION

            assert await run_migrations(conn, 17) == SCHEMA_VERSION
            await migrate_v17(conn)
            value_again = (
                await conn.execute(
                    text(
                        "SELECT rate_limit_create_share FROM app_settings WHERE id = 1"
                    )
                )
            ).scalar_one()
            assert value_again == 10

        admin = await create_user_v0(username="m27-mig-admin", is_admin=True)
        admin_session = await create_session_v0(admin["id"], "m27-mig-admin-sess")
        client = _admin_client(admin_session)

        get_resp = client.get("/api/config")
        assert get_resp.status_code == 200
        assert get_resp.json()["rate_limit_create_share"] == 10

        await load_runtime_config()
        assert rate_limit_config.create_share == 10

        put_resp = client.put("/api/config", json={"rate_limit_create_share": 30})
        assert put_resp.status_code == 200
        get_resp = client.get("/api/config")
        assert get_resp.json()["rate_limit_create_share"] == 30

        user = await create_user_v0(username="m27-mig-user")
        user_session = await create_session_v0(user["id"], "m27-mig-user-sess")
        user_client = TestClient(app)
        user_client.cookies.set(settings.session_cookie_name, user_session)
        share_resp = user_client.post(
            "/api/shares", json={"user_file_id": 999999}
        )
        assert share_resp.status_code == 404
    finally:
        rate_limit_config.create_share = 10
        await dispose_engine()
        reset_engine()
        settings.database_path = original_db
