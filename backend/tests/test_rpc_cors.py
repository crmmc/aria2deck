from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.types import Scope

from app.core.config import Settings, settings
from app.core.security import credential_digest, credential_prefix
from app.main import create_app
from app.repositories import auth as auth_repo
from tests.helpers_v0 import create_user_v0, now_ms

ORIGIN = "https://self-hosted.example:9443"
PREFLIGHT = {
    "Origin": ORIGIN,
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "content-type",
}


@pytest.mark.parametrize("enabled", [False, True])
def test_rpc_cors_switch(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr(settings, "rpc_allow_all_origins", enabled, raising=False)
    client = TestClient(create_app())
    response = client.options("/aria2/jsonrpc", headers=PREFLIGHT)
    assert response.status_code == (200 if enabled else 400)
    assert response.headers.get("access-control-allow-origin") == (
        "*" if enabled else None
    )
    if enabled:
        assert "access-control-allow-credentials" not in response.headers


@pytest.mark.parametrize(
    "path", ["/api/auth/me", "/aria2/jsonrpc/", "/aria2/jsonrpc-other"]
)
def test_rpc_cors_does_not_open_other_paths(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr(settings, "rpc_allow_all_origins", True, raising=False)
    response = TestClient(create_app()).options(path, headers=PREFLIGHT)
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "enabled,origins,warning",
    [
        (False, "", True),
        (False, "  , , ", True),
        (False, "null", True),
        (False, ORIGIN, False),
        (True, "", False),
        (True, ORIGIN, False),
    ],
)
def test_missing_origins_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    enabled: bool,
    origins: str,
    warning: bool,
) -> None:
    monkeypatch.setattr(settings, "rpc_allow_all_origins", enabled, raising=False)
    monkeypatch.setattr(settings, "cors_origins", origins)
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "allow_null_origin", False)
    with caplog.at_level(logging.WARNING):
        create_app()
    messages = [
        record.message
        for record in caplog.records
        if "ARIA2C_RPC_ALLOW_ALL_ORIGINS" in record.message
    ]
    assert len(messages) == int(warning)


@pytest.mark.parametrize(
    "value,expected", [(None, False), ("false", False), ("true", True)]
)
def test_rpc_cors_environment(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    monkeypatch.delenv("ARIA2C_RPC_ALLOW_ALL_ORIGINS", raising=False)
    if value is not None:
        monkeypatch.setenv("ARIA2C_RPC_ALLOW_ALL_ORIGINS", value)
    assert Settings().rpc_allow_all_origins is expected


@pytest.mark.parametrize(
    "body",
    ["not-json", '{"jsonrpc":"2.0","method":"aria2.tellActive","params":[],"id":1}'],
)
def test_rpc_errors_readable_without_cookie_credentials(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    monkeypatch.setattr(settings, "rpc_allow_all_origins", True, raising=False)
    response = TestClient(create_app()).post(
        "/aria2/jsonrpc",
        content=body,
        headers={
            "Origin": ORIGIN,
            "Content-Type": "application/json",
            "Cookie": "aria2_session=invalid",
        },
    )
    assert "error" in response.json()
    # Starlette reflects the origin when a Cookie header is present, but
    # credentials must remain disabled regardless of that header.
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-credentials" not in response.headers


def test_rpc_unhandled_error_has_cors(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers import aria2_rpc

    async def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("test failure")

    monkeypatch.setattr(settings, "rpc_allow_all_origins", True, raising=False)
    monkeypatch.setattr(aria2_rpc, "_handle_jsonrpc_request_body", fail)
    response = TestClient(create_app(), raise_server_exceptions=False).post(
        "/aria2/jsonrpc",
        json={},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers


@pytest.mark.parametrize("origin", [ORIGIN, "https://ariang.js.org", "null"])
def test_rpc_success_authentication_and_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: str,
    origin: str,
) -> None:
    from app.core.rate_limit_config import rate_limit_config

    secret = "rpc-cors-test-secret"

    async def create_rpc_user() -> None:
        user = await create_user_v0(username="cors-user")
        await auth_repo.set_rpc_secret(
            user["id"],
            credential_digest("rpc-secret", secret),
            credential_prefix(secret),
            now_ms(),
        )

    asyncio.run(create_rpc_user())
    monkeypatch.setattr(settings, "rpc_allow_all_origins", True)
    monkeypatch.setattr(rate_limit_config, "rpc", 2)
    client = TestClient(create_app())
    request = {
        "jsonrpc": "2.0",
        "method": "aria2.getVersion",
        "params": [f"token:{secret}"],
        "id": 1,
    }
    success = client.post("/aria2/jsonrpc", json=request, headers={"Origin": origin})
    assert success.json()["result"]["version"] == "aria2deck-proxy"
    request["params"] = ["token:wrong-secret"]
    invalid = client.post("/aria2/jsonrpc", json=request, headers={"Origin": origin})
    assert invalid.json()["error"]["code"] == 1
    blocked = client.post("/aria2/jsonrpc", json=request, headers={"Origin": origin})
    assert blocked.json()["error"]["code"] == -32000
    for response in (success, invalid, blocked):
        assert response.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in response.headers


@pytest.mark.parametrize("enabled", [False, True])
def test_fixed_origins_still_work_for_normal_api(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    monkeypatch.setattr(settings, "rpc_allow_all_origins", enabled)
    monkeypatch.setattr(settings, "cors_origins", ORIGIN)
    client = TestClient(create_app())
    for path in ("/api/auth/me", "/aria2/jsonrpc"):
        response = client.options(path, headers=PREFLIGHT)
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == (
            "*" if enabled and path == "/aria2/jsonrpc" else ORIGIN
        )


def test_rpc_get_remains_rejected_and_no_origin_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "rpc_allow_all_origins", True)
    client = TestClient(create_app())
    response = client.get("/aria2/jsonrpc", headers={"Origin": ORIGIN})
    assert response.status_code == 405
    assert response.headers["access-control-allow-origin"] == "*"
    response = client.post("/aria2/jsonrpc", content="not-json")
    assert response.json()["error"]["code"] == -32700
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "scope", [{"type": "lifespan"}, {"type": "websocket", "path": "/aria2/jsonrpc"}]
)
async def test_non_http_scopes_passthrough(scope: Scope) -> None:
    from app.http.rpc_cors import NonRpcCORSMiddleware, RpcCORSMiddleware

    downstream = AsyncMock()
    receive = AsyncMock()
    send = AsyncMock()
    middleware = RpcCORSMiddleware(NonRpcCORSMiddleware(downstream))
    await middleware(scope, receive, send)
    downstream.assert_awaited_once_with(scope, receive, send)


@pytest.mark.parametrize(
    "method,headers", [("DELETE", "content-type"), ("POST", "x-custom-header")]
)
def test_rpc_preflight_rejects_unsupported_requests(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    headers: str,
) -> None:
    monkeypatch.setattr(settings, "rpc_allow_all_origins", True)
    response = TestClient(create_app()).options(
        "/aria2/jsonrpc",
        headers={
            **PREFLIGHT,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-credentials" not in response.headers
