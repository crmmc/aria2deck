"""Coverage tests for aria2_rpc router helpers and dead-auth branches."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.routers import aria2_rpc
from app.routers.aria2_rpc import (
    _multicall_cost,
    _rpc_rate_limit_cost,
    extract_secret_for_multicall,
    process_single_request,
)
from app.services.rpc import RpcError, RpcErrorCode


def test_multicall_cost_variants() -> None:
    assert _multicall_cost(None) == 1
    assert _multicall_cost([]) == 1
    assert _multicall_cost(["nope"]) == 1
    assert _multicall_cost([[{"methodName": "a"}] * 21]) == 1
    assert _multicall_cost([[{"methodName": "a"}, {"methodName": "b"}, "junk"]]) == 2


def test_rpc_rate_limit_cost_variants() -> None:
    assert _rpc_rate_limit_cost("string") == 1
    assert _rpc_rate_limit_cost({"method": "aria2.tellActive"}) == 1
    assert _rpc_rate_limit_cost(
        [
            {"method": "system.multicall", "params": [[{"methodName": "a"}, {"methodName": "b"}]]},
            {"method": "aria2.tellActive"},
        ]
    ) == 3


def test_extract_secret_for_multicall_variants() -> None:
    assert extract_secret_for_multicall([]) == (None, [])
    assert extract_secret_for_multicall(["flat"]) == (None, ["flat"])
    assert extract_secret_for_multicall([["nope"]]) == (None, [["nope"]])
    assert extract_secret_for_multicall([[{"params": "bad"}]]) == (None, [[{"params": "bad"}]])
    assert extract_secret_for_multicall([[{"params": []}]]) == (None, [[{"params": []}]])
    assert extract_secret_for_multicall([[{"params": ["plain"]}]]) == (
        None,
        [[{"params": ["plain"]}]],
    )
    secret, params = extract_secret_for_multicall([[{"params": ["token:abc", 1]}]])
    assert secret == "abc"
    assert params == [[{"params": ["token:abc", 1]}]]


def test_log_rpc_method_response_internal_error() -> None:
    response = {
        "error": {"code": RpcErrorCode.INTERNAL_ERROR, "message": "boom"}
    }
    aria2_rpc._log_rpc_method_response("m", 1, 1, "req", response)


def test_log_rpc_method_response_success() -> None:
    aria2_rpc._log_rpc_method_response("m", 1, 1, "req", {"result": "ok"})
    aria2_rpc._log_rpc_method_response("m", 1, 1, "req", {"error": "bad-shape"})


@pytest.mark.asyncio
async def test_process_single_request_invalid_params_type() -> None:
    handler = AsyncMock()
    response = await process_single_request(
        {"jsonrpc": "2.0", "method": "aria2.tellActive", "params": {"a": 1}, "id": 1},
        handler,
    )
    assert response["error"]["code"] == RpcErrorCode.INVALID_PARAMS
    handler.handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_single_request_handler_crash() -> None:
    handler = AsyncMock()
    handler.handle.side_effect = RuntimeError("boom")
    response = await process_single_request(
        {"jsonrpc": "2.0", "method": "aria2.tellActive", "params": [], "id": 1},
        handler,
    )
    assert response["error"]["code"] == RpcErrorCode.INTERNAL_ERROR


@pytest.mark.asyncio
async def test_process_single_request_rpc_error() -> None:
    handler = AsyncMock()
    handler.handle.side_effect = RpcError(RpcErrorCode.INVALID_PARAMS, "bad", None)
    response = await process_single_request(
        {"jsonrpc": "2.0", "method": "aria2.tellActive", "params": [], "id": 1},
        handler,
    )
    assert response["error"]["code"] == RpcErrorCode.INVALID_PARAMS


def test_batch_request_too_large(client: TestClient, temp_db: str) -> None:
    item = {"jsonrpc": "2.0", "method": "aria2.getVersion", "params": ["token:x"], "id": 1}
    response = client.post("/aria2/jsonrpc", json=[item] * 21)
    assert response.status_code == 200
    assert "max 20" in response.json()["error"]["message"]


def test_authenticated_dead_branch_single_request(
    client: TestClient, temp_db: str
) -> None:
    async def broken_auth(*args, **kwargs):
        return None, None, None

    with patch.object(aria2_rpc, "_authenticate_from_params", broken_auth):
        response = client.post(
            "/aria2/jsonrpc",
            json={
                "jsonrpc": "2.0",
                "method": "aria2.getVersion",
                "params": ["token:x"],
                "id": 1,
            },
        )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == RpcErrorCode.INTERNAL_ERROR


def test_authenticated_dead_branch_batch_request(
    client: TestClient, temp_db: str
) -> None:
    async def broken_auth(*args, **kwargs):
        return None, None, None

    item = {"jsonrpc": "2.0", "method": "aria2.getVersion", "params": ["token:x"], "id": 1}
    with patch.object(aria2_rpc, "_authenticate_from_params", broken_auth):
        response = client.post("/aria2/jsonrpc", json=[item])
    assert response.status_code == 200
    assert response.json()[0]["error"]["code"] == RpcErrorCode.INTERNAL_ERROR
