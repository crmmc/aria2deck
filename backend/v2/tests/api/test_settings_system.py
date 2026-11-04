from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import SQLAlchemyError

from aria2deck.bootstrap import create_app
from aria2deck.domain.settings import SECRET_MASK
from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.sqlite.repositories.settings import SettingsRepository
from tests.api.conftest import OTHER_PASSWORD, create_admin, create_user, login


def test_public_site_info_and_settings_roles(client: TestClient) -> None:
    public = client.get("/api/v2/public/site-info")
    assert public.status_code == 200
    assert public.json() == {"site_title": "Aria2Deck"}
    assert client.get("/api/v2/settings").status_code == 401

    create_admin(client)
    login(client)
    create_user(client)
    login(client, "user", OTHER_PASSWORD)
    assert client.get("/api/v2/settings").status_code == 403
    assert client.patch("/api/v2/settings", json={"site_title": "forbidden"}).status_code == 403
    assert client.get("/api/v2/public/site-info").json()["site_title"] == "Aria2Deck"


def test_full_settings_round_trip_survives_restart(settings: Settings) -> None:
    patch = {
        "site_title": "  新站点  ",
        "max_task_size": 1024,
        "min_free_disk": 0,
        "aria2_rpc_url": "https://127.0.0.1:6800/jsonrpc",
        "aria2_rpc_secret": "test-only-rpc-secret",
        "aria2_bt_stop_timeout_seconds": 1,
        "hidden_file_extensions": [" TMP ", ".part", ".tmp"],
        "pack_format": "tar.zst",
        "pack_compression_level": 22,
        "ws_reconnect_max_delay_seconds": 60,
        "ws_reconnect_jitter_seconds": 2,
        "ws_reconnect_factor": 3,
        "history_retention_days": 3650,
        "tracker_fixed_list": ["udp://tracker.example:80", "", "udp://tracker.example:80"],
        "tracker_remote_urls": ["https://example.com/trackers.txt"],
        "tracker_refresh_interval_minutes": 5,
        "download_connection_limits": {"http": 16, "bt": 8},
        "account_security_rate_limit": 11,
        "authenticated_api_rate_limit": 601,
        "public_api_rate_limit": 301,
        "share_access_rate_limit": 31,
        "create_task_rate_limit": 61,
        "create_torrent_rate_limit": 31,
        "create_pack_rate_limit": 31,
        "aria2_test_rate_limit": 11,
        "rpc_rate_limit": 601,
        "file_search_rate_limit": 121,
        "create_share_rate_limit": 61,
    }
    with TestClient(create_app(settings=settings)) as first:
        create_admin(first)
        login(first)
        response = first.patch("/api/v2/settings", json=patch)
        assert response.status_code == 200
        saved = response.json()
        assert saved["site_title"] == "新站点"
        assert saved["aria2_rpc_secret"] == SECRET_MASK
        assert saved["hidden_file_extensions"] == [".tmp", ".part"]
        assert saved["tracker_fixed_list"] == ["udp://tracker.example:80"]
        assert saved["updated_at_ms"] > 0
        assert first.get("/api/v2/public/site-info").json() == {"site_title": "新站点"}

    with TestClient(create_app(settings=settings)) as second:
        login(second)
        restored = second.get("/api/v2/settings").json()
        assert restored == saved
        assert restored["aria2_rpc_secret"] == SECRET_MASK


def test_masked_and_null_secret_never_erase_existing_secret(client: TestClient) -> None:
    create_admin(client)
    login(client)
    plain_rpc_value = "test-only-secret-that-must-survive"
    assert client.patch(
        "/api/v2/settings", json={"aria2_rpc_secret": plain_rpc_value}
    ).status_code == 200

    masked = client.patch(
        "/api/v2/settings",
        json={"aria2_rpc_secret": "********", "site_title": "Masked"},
    )
    assert masked.status_code == 200
    null_value = client.patch(
        "/api/v2/settings", json={"aria2_rpc_secret": None, "site_title": None}
    )
    assert null_value.status_code == 200
    assert null_value.json()["site_title"] == "Masked"
    assert null_value.json()["aria2_rpc_secret"] == SECRET_MASK
    service = client.app.state.settings_service
    assert service.current().aria2_rpc_secret == plain_rpc_value


@pytest.mark.parametrize(
    "patch",
    [
        {"tracker_refresh_interval_minutes": 1},
        {"aria2_rpc_url": "http://127.0.0.1:bad/jsonrpc"},
        {"aria2_rpc_url": "http://[::1"},
        {"hidden_file_extensions": [""]},
        {"download_connection_limits": {"HTTP": 1}},
        {"pack_format": "7z"},
        {"extra": True},
    ],
)
def test_invalid_settings_return_stable_input_error(
    client: TestClient, patch: dict[str, object]
) -> None:
    create_admin(client)
    login(client)

    response = client.patch("/api/v2/settings", json=patch)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"


def test_failed_settings_save_is_not_reported_as_success(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_save(*args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("simulated database failure")

    monkeypatch.setattr(SettingsRepository, "save_settings", fail_save)
    app = create_app(settings=settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        create_admin(client)
        login(client)
        before = client.get("/api/v2/settings").json()

        response = client.patch("/api/v2/settings", json={"site_title": "Not saved"})

        assert response.status_code == 500
        assert response.json() == {
            "error": {"code": "internal_error", "message": "服务器内部错误"}
        }
        assert client.get("/api/v2/settings").json() == before
        assert app.state.settings_service.current().site_title == before["site_title"]


def test_concurrent_settings_patches_do_not_lose_fields(app: FastAPI) -> None:
    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                create_admin_response = await client.post(
                    "/api/v2/users",
                    json={
                        "username": "admin",
                        "password": "ab" * 32,
                        "is_admin": True,
                    },
                )
                assert create_admin_response.status_code == 201
                await client.post(
                    "/api/v2/auth/login",
                    json={"username": "admin", "password": "ab" * 32},
                )
                responses = await asyncio.gather(
                    client.patch("/api/v2/settings", json={"site_title": "Concurrent"}),
                    client.patch("/api/v2/settings", json={"max_task_size": 12345}),
                )
                assert [response.status_code for response in responses] == [200, 200]
                current = app.state.settings_service.current()
                assert current.site_title == "Concurrent"
                assert current.max_task_size == 12345

    asyncio.run(exercise())


def test_backend_status_requires_two_failures_and_recovers_immediately(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings=settings)

    async def frozen_listen(
        on_gid: Callable[[str], Awaitable[None]],
    ) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.download_lifecycle.backend, "listen", frozen_listen)
    with TestClient(app) as client:
        create_admin(client)
        login(client)
        create_user(client)
        connectivity = app.state.connectivity

        assert client.get("/api/v2/system/status").json()["download_backend"] == {
            "status": "ok",
            "message": "下载后端连接正常",
        }
        connectivity.record_failure()
        assert client.get("/api/v2/system/status").json()["download_backend"]["status"] == "ok"
        connectivity.record_failure()
        admin_status = client.get("/api/v2/system/status").json()["download_backend"]
        assert admin_status["status"] == "degraded"
        assert "系统设置" in admin_status["message"]

        login(client, "user", OTHER_PASSWORD)
        user_status = client.get("/api/v2/system/status").json()["download_backend"]
        assert user_status == {
            "status": "degraded",
            "message": "下载后端暂时不可用，请联系管理员",
        }
        connectivity.record_success()
        assert client.get("/api/v2/system/status").json()["download_backend"]["status"] == "ok"
