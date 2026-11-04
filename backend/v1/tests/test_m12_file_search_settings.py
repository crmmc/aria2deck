"""M12 Task 2: 文件搜索限流设置字段（T25 / T25b / AC-14 设置部分）。

- fresh bootstrap 后 GET /api/config 含 rate_limit_file_search 默认 20
- PUT 合法值（1-60）可改写并持久化
- PUT -1 / 0 / 61 被拒绝且不改写原值
- 不涉及 /api/files/search 路由（Task 3 范围）
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from tests.helpers_v0 import create_session_v0, create_user_v0


@pytest.mark.asyncio
async def test_t25_get_config_contains_file_search_default(temp_db: str) -> None:
    admin = await create_user_v0(username="m12-t25-admin", is_admin=True)
    session = await create_session_v0(admin["id"], "m12-t25-sess")

    client = TestClient(app)
    client.cookies.set(settings.session_cookie_name, session)

    resp = client.get("/api/config")
    assert resp.status_code == 200
    assert resp.json()["rate_limit_file_search"] == 20


@pytest.mark.asyncio
async def test_t25_put_updates_file_search_limit(temp_db: str) -> None:
    admin = await create_user_v0(username="m12-t25b-admin", is_admin=True)
    session = await create_session_v0(admin["id"], "m12-t25b-sess")

    client = TestClient(app)
    client.cookies.set(settings.session_cookie_name, session)

    put_resp = client.put("/api/config", json={"rate_limit_file_search": 2})
    assert put_resp.status_code == 200
    assert put_resp.json()["rate_limit_file_search"] == 2

    get_resp = client.get("/api/config")
    assert get_resp.status_code == 200
    assert get_resp.json()["rate_limit_file_search"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_value", [-1, 0, 61])
async def test_t25b_put_rejects_out_of_range_values(
    temp_db: str, invalid_value: int
) -> None:
    admin = await create_user_v0(
        username=f"m12-t25b-rej-{invalid_value - -1}", is_admin=True
    )
    session = await create_session_v0(admin["id"], f"m12-t25b-rej-{invalid_value}")

    client = TestClient(app)
    client.cookies.set(settings.session_cookie_name, session)

    put_resp = client.put(
        "/api/config", json={"rate_limit_file_search": invalid_value}
    )
    assert put_resp.status_code == 422

    get_resp = client.get("/api/config")
    assert get_resp.json()["rate_limit_file_search"] == 20
