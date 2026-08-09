"""M3 T13: routers/aria2_rpc.py 断 aria2 依赖。

- AST 检查：``app/routers/aria2_rpc.py`` 不再 import ``get_aria2_client``
- AST 检查：``Aria2RpcHandler.__init__`` 签名不含 ``aria2_client``
- 集成：POST /aria2/jsonrpc 单条与批量请求正常
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.security import credential_digest, credential_prefix
from app.repositories import auth as auth_repo
from tests.helpers_v0 import create_user_v0, now_ms

ROUTER_PATH = Path(__file__).resolve().parents[1] / "app" / "routers" / "aria2_rpc.py"
HANDLER_PATH = Path(__file__).resolve().parents[1] / "app" / "services" / "rpc" / "system.py"


@pytest.fixture
def rpc_user(temp_db: str) -> dict:
    async def create() -> dict:
        user = await create_user_v0(username="t13_rpc_user")
        secret = "t13_rpc_secret"
        await auth_repo.set_rpc_secret(
            user["id"],
            credential_digest("rpc-secret", secret),
            credential_prefix(secret),
            now_ms(),
        )
        return {**user, "rpc_secret": secret}

    return asyncio.run(create())


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[-1])
    return names


def test_router_does_not_import_get_aria2_client() -> None:
    source = ROUTER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "get_aria2_client" not in _imported_names(tree)


def test_handler_init_has_no_aria2_client_param() -> None:
    source = HANDLER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Aria2RpcHandler":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    args = [a.arg for a in item.args.args]
                    assert "aria2_client" not in args
                    assert args == ["self", "user_id"]
                    return
            pytest.fail("Aria2RpcHandler.__init__ not found")
    pytest.fail("Aria2RpcHandler class not found")


def test_handler_has_no_client_attribute() -> None:
    source = HANDLER_PATH.read_text(encoding="utf-8")
    assert "self.client" not in source


def test_rpc_single_request(client: TestClient, rpc_user: dict) -> None:
    from app.routers.aria2_rpc import rpc_limiter

    rpc_limiter._requests.clear()
    response = client.post(
        "/aria2/jsonrpc",
        json={
            "jsonrpc": "2.0",
            "method": "aria2.getVersion",
            "params": [f"token:{rpc_user['rpc_secret']}"],
            "id": "1",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "1"
    assert payload["result"]["version"] == "aria2deck-proxy"


def test_rpc_batch_request(client: TestClient, rpc_user: dict) -> None:
    from app.routers.aria2_rpc import rpc_limiter

    rpc_limiter._requests.clear()
    token = f"token:{rpc_user['rpc_secret']}"
    response = client.post(
        "/aria2/jsonrpc",
        json=[
            {
                "jsonrpc": "2.0",
                "method": "aria2.getVersion",
                "params": [token],
                "id": "1",
            },
            {
                "jsonrpc": "2.0",
                "method": "aria2.getGlobalStat",
                "params": [token],
                "id": "2",
            },
        ],
    )
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert len(payload) == 2
    by_id = {item["id"]: item for item in payload}
    assert by_id["1"]["result"]["version"] == "aria2deck-proxy"
    stat = by_id["2"]["result"]
    assert "downloadSpeed" in stat
    assert "numActive" in stat
