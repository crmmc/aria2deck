from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import SQLAlchemyError

from aria2deck.bootstrap import create_app
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.settings import SECRET_MASK
from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.sqlite.repositories.settings import SettingsRepository
from tests.api.conftest import OTHER_PASSWORD, create_admin, create_user, login


def test_public_site_info_and_settings_roles(client: TestClient) -> None:
    public = client.get("/api/v2/public/site-info")
    assert public.status_code == 200
    assert public.json() == {
        "site_title": "Aria2Deck",
        "ws_reconnect_max_delay_seconds": 30,
        "ws_reconnect_jitter_seconds": 1,
        "ws_reconnect_factor": 2,
    }
    assert client.get("/api/v2/settings").status_code == 401

    create_admin(client)
    login(client)
    create_user(client)
    login(client, "user", OTHER_PASSWORD)
    assert client.get("/api/v2/settings").status_code == 403
    assert client.patch("/api/v2/settings", json={"site_title": "forbidden"}).status_code == 403
    assert client.get("/api/v2/public/site-info").json()["site_title"] == "Aria2Deck"


def test_public_site_info_uses_the_non_default_public_ip_limit(
    client: TestClient,
) -> None:
    create_admin(client)
    login(client)
    assert (
        client.patch("/api/v2/settings", json={"public_api_rate_limit": 1}).status_code
        == 200
    )
    public = TestClient(client.app, client=("198.51.100.20", 4321))

    assert public.get("/api/v2/public/site-info").status_code == 200
    limited = public.get("/api/v2/public/site-info")
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert "Retry-After" in limited.headers


def test_settings_contract_does_not_expose_hidden_file_extensions(
    client: TestClient,
) -> None:
    create_admin(client)
    login(client)

    current = client.get("/api/v2/settings")
    assert current.status_code == 200
    assert "hidden_file_extensions" not in current.json()
    rejected = client.patch(
        "/api/v2/settings", json={"hidden_file_extensions": [".tmp"]}
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "invalid_input"


class _FakeVersionPort:
    def __init__(self, outcome: tuple[str, list[str]] | Exception) -> None:
        self.outcome = outcome

    async def version(self) -> tuple[str, list[str]]:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_aria2_connection_test_success_returns_version_and_features(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_admin(client)
    login(client)
    monkeypatch.setattr(
        app.state.settings_service,
        "aria2_version",
        _FakeVersionPort(("1.37.0", ["HTTPS", "BitTorrent"])),
    )

    response = client.post("/api/v2/settings/aria2/test")

    assert response.status_code == 200
    assert response.json() == {
        "connected": True,
        "version": "1.37.0",
        "features": ["HTTPS", "BitTorrent"],
    }


def test_aria2_connection_test_failure_returns_200_without_leaking_details(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_admin(client)
    login(client)
    leaking_message = (
        "连接失败 http://127.0.0.1:6800/jsonrpc token:raw-secret ValueError: boom"
    )
    monkeypatch.setattr(
        app.state.settings_service,
        "aria2_version",
        _FakeVersionPort(DomainError(ErrorCode.SUBMISSION_UNCERTAIN, leaking_message)),
    )

    response = client.post("/api/v2/settings/aria2/test")

    assert response.status_code == 200
    assert response.json() == {
        "connected": False,
        "version": None,
        "features": [],
    }
    assert "6800" not in response.text
    assert "raw-secret" not in response.text
    assert "boom" not in response.text


def test_aria2_connection_test_roles(client: TestClient) -> None:
    assert client.post("/api/v2/settings/aria2/test").status_code == 401

    create_admin(client)
    login(client)
    create_user(client)
    login(client, "user", OTHER_PASSWORD)
    assert client.post("/api/v2/settings/aria2/test").status_code == 403


def test_aria2_connection_test_is_rate_limited_per_user(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_admin(client)
    login(client)
    monkeypatch.setattr(
        app.state.settings_service,
        "aria2_version",
        _FakeVersionPort(("1.37.0", [])),
    )
    assert (
        client.patch("/api/v2/settings", json={"aria2_test_rate_limit": 1}).status_code
        == 200
    )

    first = client.post("/api/v2/settings/aria2/test")
    second = client.post("/api/v2/settings/aria2/test")

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"
    assert int(second.headers["Retry-After"]) >= 59


def test_full_settings_round_trip_survives_restart(settings: Settings) -> None:
    patch = {
        "site_title": "  新站点  ",
        "max_task_size": 1024,
        "min_free_disk": 0,
        "aria2_rpc_url": "https://127.0.0.1:6800/jsonrpc",
        "aria2_rpc_secret": "test-only-rpc-secret",
        "aria2_bt_stop_timeout_seconds": 1,
        "pack_format": "tar.zst",
        "pack_compression_level": 22,
        "ws_reconnect_max_delay_seconds": 60,
        "ws_reconnect_jitter_seconds": 2,
        "ws_reconnect_factor": 3,
        "history_retention_days": 3650,
        "tracker_fixed_list": ["udp://tracker.example:80", "", "udp://tracker.example:80"],
        "tracker_remote_urls": ["https://example.com/trackers.txt"],
        "tracker_refresh_interval_minutes": 5,
        "download_connection_limits": {"authenticated_per_user_connections": 16, "anonymous_per_ip_connections": 8},
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
        assert "hidden_file_extensions" not in saved
        assert saved["tracker_fixed_list"] == ["udp://tracker.example:80"]
        assert saved["updated_at_ms"] > 0
        assert first.get("/api/v2/public/site-info").json() == {
            "site_title": "新站点",
            "ws_reconnect_max_delay_seconds": 60,
            "ws_reconnect_jitter_seconds": 2,
            "ws_reconnect_factor": 3,
        }

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


def test_create_app_wires_aria2_version_port_without_monkeypatch(tmp_path: Path) -> None:
    """CFG-04 装配守卫：真实装配下设置服务必须带上 aria2 版本端口。

    该接线一旦在 bootstrap 中丢失，/settings/aria2/test 会在生产返回 500，
    而普通单测全部用 monkeypatch 替身，无法暴露漏装配。
    """
    settings = Settings(
        database_path=tmp_path / "data" / "v2.db",
        download_directory=tmp_path / "downloads",
        static_directory=tmp_path / "static",
        debug=True,
    )

    app = create_app(settings=settings)

    assert app.state.settings_service.aria2_version is not None


def test_machine_stats_admin_only_and_fields(client: TestClient, settings) -> None:
    # 未登录 401。
    assert client.get("/api/v2/stats/machine").status_code == 401

    create_admin(client)
    login(client)
    create_user(client)
    login(client, "user", OTHER_PASSWORD)
    # 普通用户 403。
    assert client.get("/api/v2/stats/machine").status_code == 403

    login(client)
    body = client.get("/api/v2/stats/machine").json()
    assert body["total_bytes"] > 0
    assert body["free_bytes"] >= 0
    assert body["download_used_bytes"] >= 0
    assert body["system_used_bytes"] >= 0
    assert body["used_bytes"] == body["download_used_bytes"] + body["system_used_bytes"]


class _RecordingVersionPort:
    """记录探测参数的连接测试替身（A4/A8）。"""

    def __init__(self, outcome: tuple[str, list[str]] | Exception) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, str | None]] = []

    async def version(
        self, *, url: str | None = None, secret: str | None = None
    ) -> tuple[str, list[str]]:
        self.calls.append({"url": url, "secret": secret})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_aria2_version_endpoint_is_read_only_probe(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A4：GET /settings/aria2/version 只读探测当前配置，管理员会话专用。"""
    port = _RecordingVersionPort(("1.37.0", ["HTTPS"]))
    monkeypatch.setattr(app.state.settings_service, "aria2_version", port)

    assert client.get("/api/v2/settings/aria2/version").status_code == 401
    create_admin(client)
    login(client)
    create_user(client)
    login(client, "user", OTHER_PASSWORD)
    assert client.get("/api/v2/settings/aria2/version").status_code == 403

    login(client)
    response = client.get("/api/v2/settings/aria2/version")
    assert response.status_code == 200
    assert response.json() == {"connected": True, "version": "1.37.0", "features": ["HTTPS"]}
    assert port.calls == [{"url": None, "secret": None}]

    failure = client.get("/api/v2/settings/aria2/version")  # 限流前的成功探测已消耗 1 次
    assert failure.status_code in {200, 429}


def test_aria2_test_with_form_values_probes_without_saving(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A8：表单值优先测试且不落库；掩码密钥复用已存密钥。"""
    create_admin(client)
    login(client)
    saved_secret = "saved-rpc-secret-for-mask-reuse"  # noqa: S105  # 测试夹具密钥
    assert client.patch("/api/v2/settings", json={"aria2_rpc_secret": saved_secret}).status_code == 200
    port = _RecordingVersionPort(("1.37.0", []))
    monkeypatch.setattr(app.state.settings_service, "aria2_version", port)
    before = client.get("/api/v2/settings").json()
    assert before["aria2_rpc_secret"] == SECRET_MASK  # 读取面只回掩码，不回原文

    probed = client.post(
        "/api/v2/settings/aria2/test",
        json={"aria2_rpc_url": "http://candidate:6800/jsonrpc", "aria2_rpc_secret": "form-secret"},
    )
    assert probed.status_code == 200
    assert probed.json()["connected"] is True
    assert port.calls == [
        {"url": "http://candidate:6800/jsonrpc", "secret": "form-secret"}
    ]

    masked = client.post(
        "/api/v2/settings/aria2/test",
        json={"aria2_rpc_url": "http://masked:6800/jsonrpc", "aria2_rpc_secret": SECRET_MASK},
    )
    assert masked.status_code == 200
    assert port.calls[-1]["url"] == "http://masked:6800/jsonrpc"
    assert port.calls[-1]["secret"] == saved_secret  # 掩码 = 复用已存密钥探测

    # 未保存：设置读取仍是旧值，密钥仍只以掩码出现。
    after = client.get("/api/v2/settings").json()
    assert after["aria2_rpc_url"] == before["aria2_rpc_url"]
    assert after["aria2_rpc_secret"] == SECRET_MASK


def test_aria2_test_rejects_invalid_form_url_with_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_admin(client)
    login(client)
    monkeypatch.setattr(
        client.app.state.settings_service,
        "aria2_version",
        _RecordingVersionPort(("1.37.0", [])),
    )
    response = client.post(
        "/api/v2/settings/aria2/test",
        json={"aria2_rpc_url": "ftp://bad-scheme/jsonrpc"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"


def test_aria2_test_without_body_still_probes_saved_config(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_admin(client)
    login(client)
    port = _RecordingVersionPort(("1.37.0", []))
    monkeypatch.setattr(app.state.settings_service, "aria2_version", port)
    response = client.post("/api/v2/settings/aria2/test")
    assert response.status_code == 200
    assert port.calls == [{"url": None, "secret": None}]
