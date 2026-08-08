"""M3 T12: RPC 写路径切换到 Task Core register/unref。

验证 ``Aria2RpcHandler`` 的写方法：

- ``aria2.addUri`` 不再走 ``download_service.create_user_download``，
  而是走 ``task_service.register_and_submit``（register + submit_tid）。
- ``aria2.addTorrent`` 同样走 ``register_and_submit``（resource_kind=torrent，
  source_uri 为 ``base64:`` 约定）。
- ``aria2.remove`` / ``aria2.forceRemove`` 不再走 ``download_service.cancel_user_task``，
  而是走 ``task_service.cancel_task``（unref）。
- 错误映射保持 RPC 语义（TASK_EXISTS / QUOTA_EXCEEDED）。
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest

from app.domain.errors import ConflictError, ForbiddenError
from app.modules.task_core.register import ResourceSpec
from app.services.aria2_rpc_handler import Aria2RpcHandler, RpcError, RpcErrorCode
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


def _bencode_bytes(value: bytes) -> bytes:
    return str(len(value)).encode("ascii") + b":" + value


def _valid_torrent_b64() -> str:
    info = b"d6:lengthi4e4:name4:teste"
    raw = b"d" + _bencode_bytes(b"info") + info + b"e"
    return base64.b64encode(raw).decode("ascii")


def _install_spy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: dict,
) -> AsyncMock:
    spy = AsyncMock(return_value=payload)
    monkeypatch.setattr(
        "app.services.aria2_rpc_handler.task_service.register_and_submit", spy
    )
    return spy


async def test_add_uri_uses_register_and_submit(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="t12_add_uri")
    spy = _install_spy(monkeypatch, payload={"id": 321})
    handler = Aria2RpcHandler(user["id"])

    result = await handler.handle(
        "aria2.addUri",
        [["https://example.com/file.bin"], {"out": "file.bin"}],
    )

    assert result == "task-321"
    spy.assert_awaited_once()
    call = spy.await_args.kwargs
    assert call["user_id"] == user["id"]
    assert call["quota_bytes"] == user["quota_bytes"]
    assert call["options"] == {"out": "file.bin"}
    resource = call["resource"]
    assert isinstance(resource, ResourceSpec)
    assert resource.source_uri == "https://example.com/file.bin"
    assert resource.resource_kind == "http"
    assert resource.display_name == "file.bin"
    assert resource.size_known is False


async def test_add_uri_magnet_uses_register_and_submit(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="t12_add_magnet")
    spy = _install_spy(monkeypatch, payload={"id": 654})
    handler = Aria2RpcHandler(user["id"])
    info_hash = "0123456789abcdef0123456789abcdef01234567"

    result = await handler.handle(
        "aria2.addUri",
        [[f"magnet:?xt=urn:btih:{info_hash.upper()}&tr=https://tracker.example/x"]],
    )

    assert result == "task-654"
    spy.assert_awaited_once()
    resource = spy.await_args.kwargs["resource"]
    assert resource.resource_kind == "magnet"
    assert resource.source_uri == f"magnet:?xt=urn:btih:{info_hash}"
    assert resource.resource_key == info_hash


async def test_add_torrent_uses_register_and_submit(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="t12_add_torrent")
    spy = _install_spy(monkeypatch, payload={"id": 987})
    handler = Aria2RpcHandler(user["id"])
    torrent_data = _valid_torrent_b64()

    result = await handler.handle(
        "aria2.addTorrent",
        [torrent_data, [], {"out": "seed.bin"}],
    )

    assert result == "task-987"
    spy.assert_awaited_once()
    call = spy.await_args.kwargs
    assert call["user_id"] == user["id"]
    assert call["options"] == {"out": "seed.bin"}
    resource = call["resource"]
    assert resource.resource_kind == "torrent"
    assert resource.source_uri == f"base64:{torrent_data}"
    assert resource.display_name == "test"
    assert resource.size_known is True
    assert resource.size_bytes == 4


async def test_add_uri_duplicate_maps_to_task_exists(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="t12_dup")
    spy = AsyncMock(side_effect=ConflictError("任务已存在"))
    monkeypatch.setattr(
        "app.services.aria2_rpc_handler.task_service.register_and_submit", spy
    )
    handler = Aria2RpcHandler(user["id"])

    with pytest.raises(RpcError) as exc_info:
        await handler.handle("aria2.addUri", [["https://example.com/dup.bin"]])

    assert exc_info.value.code == RpcErrorCode.TASK_EXISTS
    assert exc_info.value.message == "任务已存在"


async def test_add_uri_quota_maps_to_quota_exceeded(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="t12_quota")
    spy = AsyncMock(side_effect=ForbiddenError("用户配额不足，无法创建任务"))
    monkeypatch.setattr(
        "app.services.aria2_rpc_handler.task_service.register_and_submit", spy
    )
    handler = Aria2RpcHandler(user["id"])

    with pytest.raises(RpcError) as exc_info:
        await handler.handle("aria2.addUri", [["https://example.com/quota.bin"]])

    assert exc_info.value.code == RpcErrorCode.QUOTA_EXCEEDED


async def test_remove_uses_cancel_task(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="t12_remove")
    gd = await create_global_download_v0(
        resource_key="http:t12-remove",
        source_uri="https://example.com/remove.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid-t12-remove",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status="active",
    )
    client = make_aria2_client()
    spy = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(
        "app.services.aria2_rpc_handler.task_service.cancel_task", spy
    )
    handler = Aria2RpcHandler(user["id"])

    result = await handler.handle("aria2.remove", [f"task-{task['id']}"])

    assert result == f"task-{task['id']}"
    spy.assert_awaited_once_with(
        user_id=user["id"],
        user_task_id=int(task["id"]),
        quota_bytes=user["quota_bytes"],
    )
    # 旧路径的 force_remove 不再被调用（unref/claim cleanup 内部才控制 aria2）。
    client.force_remove.assert_not_awaited()


async def test_force_remove_uses_cancel_task(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="t12_force_remove")
    gd = await create_global_download_v0(
        resource_key="http:t12-force-remove",
        source_uri="https://example.com/force.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid-t12-force",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status="active",
    )
    spy = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(
        "app.services.aria2_rpc_handler.task_service.cancel_task", spy
    )
    handler = Aria2RpcHandler(user["id"])

    result = await handler.handle("aria2.forceRemove", [f"task-{task['id']}"])

    assert result == f"task-{task['id']}"
    spy.assert_awaited_once()


async def test_remove_terminal_task_maps_to_not_found(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="t12_remove_terminal")
    gd = await create_global_download_v0(
        resource_key="http:t12-terminal",
        source_uri="https://example.com/done.bin",
        resource_kind="http",
        status="completed",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status="completed",
    )
    spy = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(
        "app.services.aria2_rpc_handler.task_service.cancel_task", spy
    )
    handler = Aria2RpcHandler(user["id"])

    with pytest.raises(RpcError) as exc_info:
        await handler.handle("aria2.remove", [f"task-{task['id']}"])

    assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND
    spy.assert_not_awaited()


def test_handler_no_longer_imports_legacy_write_path() -> None:
    """aria2_rpc_handler 不再依赖 download_service 的创建/取消入口。"""
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "services" / "aria2_rpc_handler.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names.add(alias.name)
    assert "create_user_download" not in imported_names
    assert "create_user_torrent_download" not in imported_names
    assert "cancel_user_task" not in imported_names
    assert "task_service" in imported_names
