"""M3 T14: system.multicall 覆盖 Task Core 新路径。

验证 ``_handle_system_multicall`` 的每个子调用都通过 ``self.handle``
分发，因此自动复用 T11（读路径投影）/ T12（写路径 register/unref）：

- 混合批量 ``addUri`` + ``tellStatus`` + ``remove``：
  - ``addUri`` 走 ``task_service.register_and_submit``（register + submit）。
  - ``remove`` 走 ``task_service.cancel_task``（unref），且 aria2 只读 client
    完全不参与。
  - ``tellStatus`` 在 aria2 不可达时仍从 DB 投影返回。
- 嵌套 multicall 被拒（INVALID_REQUEST）。
- 超过 20 个子调用被拒（INVALID_REQUEST）。
- 单个子调用失败不影响其他子调用（faultCode 结构，非 RpcError 异常
  也安全落为 INTERNAL_ERROR 并脱敏）。
"""

from __future__ import annotations
from unittest.mock import AsyncMock

import pytest

from app.modules.task_core.sync import record_observed_snapshot
from app.services.rpc import (
    SAFE_INTERNAL_ERROR_MESSAGE,
    Aria2RpcHandler,
    RpcError,
    RpcErrorCode,
)
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


async def _make_active_task(
    username: str,
    resource_key: str,
    *,
    display_name: str = "mix.bin",
    completed_bytes: int = 400,
) -> tuple[dict, dict, dict]:
    user = await create_user_v0(username=username)
    gd = await create_global_download_v0(
        resource_key=resource_key,
        source_uri=f"http://example.com/{display_name}",
        resource_kind="http",
        status="active",
        aria2_gid=f"gid-{resource_key[-6:]}",
        total_bytes=1000,
        completed_bytes=completed_bytes,
        display_name=display_name,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status="active",
        display_name=display_name,
    )
    return user, gd, task


async def _write_snapshot(gd: dict, raw: dict) -> None:
    await record_observed_snapshot(tid=int(gd["id"]), observed_status=raw)


def _broken_read_client():
    """aria2 client whose every read method raises (projection must serve reads)."""
    error = RuntimeError("aria2 unavailable")
    return make_aria2_client(
        tell_status=error,
        tell_active=error,
        tell_waiting=error,
        tell_stopped=error,
        get_files=error,
        get_uris=error,
        get_peers=error,
        get_servers=error,
        get_version=error,
        get_global_stat=error,
    )


@pytest.mark.asyncio
async def test_multicall_mixed_add_uri_tell_status_remove_uses_new_paths(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, gd, task = await _make_active_task(
        "t14_mix", "http://example.com/mix.bin", completed_bytes=400
    )
    await _write_snapshot(
        gd,
        {
            "gid": gd["aria2_gid"],
            "status": "active",
            "totalLength": "1000",
            "completedLength": "700",
            "downloadSpeed": "123",
        },
    )
    client = _broken_read_client()
    register_spy = AsyncMock(return_value={"id": 777})
    cancel_spy = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(
        "app.services.rpc.write.task_service.register_and_submit",
        register_spy,
    )
    monkeypatch.setattr(
        "app.services.rpc.write.task_service.cancel_task",
        cancel_spy,
    )
    handler = Aria2RpcHandler(user["id"])

    result = await handler.handle(
        "system.multicall",
        [
            [
                {
                    "methodName": "aria2.addUri",
                    "params": [["https://example.com/new.bin"], {"out": "new.bin"}],
                },
                {
                    "methodName": "aria2.tellStatus",
                    "params": [f"task-{task['id']}", ["gid", "completedLength"]],
                },
                {
                    "methodName": "aria2.remove",
                    "params": [f"task-{task['id']}"],
                },
            ]
        ],
    )

    assert result[0] == ["task-777"]
    assert result[1] == [
        {"gid": f"task-{task['id']}", "completedLength": "700"}
    ]
    assert result[2] == [f"task-{task['id']}"]

    register_spy.assert_awaited_once()
    register_kwargs = register_spy.await_args.kwargs
    assert register_kwargs["user_id"] == user["id"]
    assert register_kwargs["resource"].source_uri == "https://example.com/new.bin"

    cancel_spy.assert_awaited_once_with(
        user_id=user["id"],
        user_task_id=int(task["id"]),
        quota_bytes=user["quota_bytes"],
    )

    client.tell_status.assert_not_awaited()
    client.force_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_multicall_rejects_nested_multicall(temp_db: str) -> None:
    user = await create_user_v0(username="t14_nested")
    handler = Aria2RpcHandler(user["id"])

    result = await handler.handle(
        "system.multicall",
        [
            [
                {
                    "methodName": "system.multicall",
                    "params": [[{"methodName": "aria2.getVersion", "params": []}]],
                }
            ]
        ],
    )

    assert result == [
        {
            "faultCode": RpcErrorCode.INVALID_REQUEST,
            "faultString": "Nested multicall is not allowed",
        }
    ]


@pytest.mark.asyncio
async def test_multicall_rejects_more_than_20_methods(temp_db: str) -> None:
    user = await create_user_v0(username="t14_oversize")
    handler = Aria2RpcHandler(user["id"])
    calls = [{"methodName": "aria2.getVersion", "params": []} for _ in range(21)]

    with pytest.raises(RpcError) as exc_info:
        await handler.handle("system.multicall", [calls])

    assert exc_info.value.code == RpcErrorCode.INVALID_REQUEST
    assert exc_info.value.message == "Too many methods in multicall, max 20"


@pytest.mark.asyncio
async def test_multicall_single_failure_does_not_block_others(temp_db: str) -> None:
    user, _gd, task = await _make_active_task(
        "t14_fail", "http://example.com/fail.bin"
    )
    handler = Aria2RpcHandler(user["id"])

    result = await handler.handle(
        "system.multicall",
        [
            [
                {"methodName": "aria2.tellStatus", "params": ["task-99999999"]},
                {"methodName": "aria2.tellStatus", "params": [f"task-{task['id']}", ["gid"]]},
                {"methodName": "aria2.getVersion", "params": []},
            ]
        ],
    )

    assert result[0] == {
        "faultCode": RpcErrorCode.TASK_NOT_FOUND,
        "faultString": "Task not found: task-99999999",
    }
    assert result[1] == [{"gid": f"task-{task['id']}"}]
    assert result[2] == [{"version": "aria2deck-proxy", "enabledFeatures": []}]


@pytest.mark.asyncio
async def test_multicall_unexpected_exception_is_sanitized(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await create_user_v0(username="t14_sanitize")
    handler = Aria2RpcHandler(user["id"])
    monkeypatch.setattr(
        handler,
        "_handle_get_version",
        AsyncMock(side_effect=RuntimeError("t14-internal-secret")),
    )

    result = await handler.handle(
        "system.multicall",
        [
            [
                {"methodName": "aria2.getVersion", "params": []},
                {"methodName": "system.listMethods", "params": []},
            ]
        ],
    )

    assert result[0] == {
        "faultCode": RpcErrorCode.INTERNAL_ERROR,
        "faultString": SAFE_INTERNAL_ERROR_MESSAGE,
    }
    assert "t14-internal-secret" not in repr(result)
    assert result[1] == [handler.SUPPORTED_METHODS]


@pytest.mark.asyncio
async def test_multicall_invalid_subcall_shape_gets_fault_entry(temp_db: str) -> None:
    user = await create_user_v0(username="t14_shape")
    handler = Aria2RpcHandler(user["id"])

    result = await handler.handle(
        "system.multicall",
        [["not-a-dict", {"methodName": "aria2.getVersion", "params": []}]],
    )

    assert result[0] == {
        "faultCode": RpcErrorCode.INVALID_PARAMS,
        "faultString": "Invalid method call",
    }
    assert result[1] == [{"version": "aria2deck-proxy", "enabledFeatures": []}]


@pytest.mark.asyncio
async def test_multicall_unknown_method_fault_does_not_block_others(
    temp_db: str,
) -> None:
    user = await create_user_v0(username="t14_unknown")
    handler = Aria2RpcHandler(user["id"])

    result = await handler.handle(
        "system.multicall",
        [
            [
                {"methodName": "aria2.nonExistent", "params": []},
                {"methodName": "aria2.getVersion", "params": []},
            ]
        ],
    )

    assert result[0]["faultCode"] == RpcErrorCode.METHOD_NOT_FOUND
    assert result[1] == [{"version": "aria2deck-proxy", "enabledFeatures": []}]
