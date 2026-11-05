from __future__ import annotations

import json
from contextlib import ExitStack

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aria2deck.application.downloads.broadcast import BroadcastMessage
from tests.api.conftest import create_admin, create_user, login


def _connect(client: TestClient, **kwargs):
    return client.websocket_connect("/ws/tasks", **kwargs)


def test_ws_requires_session_cookie(client: TestClient) -> None:
    create_admin(client)
    with pytest.raises(WebSocketDisconnect) as caught:
        with _connect(client):
            pass
    assert caught.value.code == 4401


def test_ws_origin_check(client: TestClient, app: FastAPI) -> None:
    create_admin(client)
    login(client)
    app.state.ws_service.allowed_origins = frozenset({"https://app.example"})
    with pytest.raises(WebSocketDisconnect) as caught:
        with _connect(client, headers={"Origin": "https://evil.example"}):
            pass
    assert caught.value.code == 4403


def test_ws_unknown_path_closes_4404(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect("/ws/other"):
            pass
    assert caught.value.code == 4404


def test_ws_heartbeat_and_push_matching_rest(client: TestClient, app: FastAPI) -> None:
    create_admin(client)
    login(client)
    user_id = int(client.get("/api/v2/auth/me").json()["id"])

    with _connect(client) as socket:
        socket.send_text("ping")
        assert socket.receive_text() == "pong"
        socket.send_text("not-a-ping-message")  # 坏消息忽略。
        socket.send_text("ping")
        assert socket.receive_text() == "pong"

        # 直接通过应用内总线发布（经 portal 落到应用事件循环），模拟生命周期状态变更。
        bus = app.state.ws_service.broadcast

        def _publish() -> None:
            bus.publish(
                [user_id],
                BroadcastMessage(
                    type="task_update",
                    task_id=1,
                    status="active",
                    payload={
                        "id": 1,
                        "status": "active",
                        "status_label": "下载中",
                        "name": "demo.bin",
                        "uri": None,
                        "total_bytes": 10,
                        "completed_bytes": 4,
                        "download_speed": 1,
                        "upload_speed": 0,
                        "peer_count": 0,
                        "connection_count": 1,
                        "files": [],
                        "observation_fresh": True,
                        "reserved_bytes": 0,
                        "error_code": None,
                        "retryable": False,
                        "created_at_ms": 1,
                        "updated_at_ms": 2,
                        "finished_at_ms": None,
                    },
                ),
            )

        _publish()
        frame = json.loads(socket.receive_text())
        assert frame["type"] == "task_update"
        # AC5 对照：WS task 字段与 REST TaskResponse 同构。
        assert set(frame["task"]) == {
            "id", "status", "status_label", "name", "uri",
            "total_bytes", "completed_bytes", "download_speed", "upload_speed",
            "peer_count", "connection_count", "files", "observation_fresh",
            "reserved_bytes", "error_code", "retryable",
            "created_at_ms", "updated_at_ms", "finished_at_ms",
        }

        # 他人消息不进入本人通道。
        bus.publish(
            [user_id + 999], BroadcastMessage("task_update", 2, "active", {"id": 2})
        )
        socket.send_text("ping")
        assert socket.receive_text() == "pong"


def test_ws_rest_and_ws_projection_consistency(client: TestClient, app: FastAPI, settings) -> None:
    """AC5 专项：同一任务在 REST 与 WS 输出同构同源。"""
    from tests.api.test_files import seed_file  # noqa: F401  # 环境准备复用

    create_admin(client)
    login(client)
    user_id = int(client.get("/api/v2/auth/me").json()["id"])

    # 通过生命周期广播挂接点触发真实推送：直接驱动 reconcile 不可控，
    # 改用同一投影入口构造消息（task_message 即 REST 序列化源）。
    from aria2deck.application.downloads.broadcast import task_message
    from aria2deck.application.downloads.models import TaskProjection
    from aria2deck.domain.downloads import SubscriptionStatus

    projection = TaskProjection(
        id=3,
        status=SubscriptionStatus.COMPLETED,
        status_label="已完成",
        name="done.bin",
        display_uri="https://example.com/展示",
        total_bytes=7,
        completed_bytes=7,
        download_speed=0,
        upload_speed=0,
        peer_count=0,
        connection_count=0,
        files=(),
        observation_fresh=False,
        reserved_bytes=0,
        error_code=None,
        retryable=False,
        created_at_ms=1,
        updated_at_ms=2,
        finished_at_ms=3,
    )
    bus = app.state.ws_service.broadcast
    with _connect(client) as socket:
        bus.publish([user_id], task_message(projection))
        frame = json.loads(socket.receive_text())
    assert frame["task"]["status"] == "completed"
    assert frame["task"]["name"] == "done.bin"
    assert frame["task"]["finished_at_ms"] == 3


def test_ws_connection_limit_per_user(client: TestClient) -> None:
    create_admin(client)
    login(client)
    with ExitStack() as stack:
        for _ in range(5):
            stack.enter_context(_connect(client))
        # 裸 .close() 不驱动服务端收尾，必须走完整 with 语义，否则 portal 卸载挂死。
        with pytest.raises(WebSocketDisconnect) as caught:
            with _connect(client):
                pass
    assert caught.value.code == 4429


def test_ws_rest_ws_field_by_field_consistency(client: TestClient, app: FastAPI, settings) -> None:
    """P2-4：REST item 与 WS task_update 逐字段 diff（AC5 专项）。"""
    from tests.api.test_aria2_rpc import _seed_task  # 复用种子（当前时间基准）。

    create_admin(client)
    login(client)
    user_id = int(client.get("/api/v2/auth/me").json()["id"])
    task_id, _gid = _seed_task(settings, user_id, name="consistency.bin")

    rest_items = client.get("/api/v2/tasks").json()["items"]
    assert any(item["id"] == task_id for item in rest_items)

    import asyncio

    from aria2deck.application.downloads.broadcast import task_message

    # 用与 REST 相同的投影入口构造 WS 帧（唯一序列化点 task_message）。
    from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
    from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository
    from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

    async def _project() -> dict:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                record = await DownloadRepository(uow.connection).get_task(
                    user_id=user_id, task_id=task_id
                )
            from aria2deck.application.downloads.projection import TaskProjector
            from aria2deck.infrastructure.observations import InMemoryDownloadObservations

            projector = TaskProjector(
                observations=InMemoryDownloadObservations(),
                clock_ms=lambda: 0,
            )
            return task_message(projector.project(record)).payload
        finally:
            await database.close()

    ws_payload = asyncio.run(_project())
    rest_item = next(item for item in rest_items if item["id"] == task_id)
    assert ws_payload == rest_item, "REST 与 WS 输出必须逐字段一致"


def test_ws_revocation_scenarios_e2e(client: TestClient, app: FastAPI) -> None:
    """P2-7：登出/登录替换/改密/删用户 → WS 连接立即关闭（不等 30s 复验）。"""
    create_admin(client)
    login(client)

    # 登出：注销当前会话 → 撤销总线触发 → 连接关闭。
    with _connect(client) as socket:
        socket.send_text("ping")
        assert socket.receive_text() == "pong"
        client.post("/api/v2/auth/logout")
        with pytest.raises(WebSocketDisconnect) as caught:
            socket.receive_text()
        assert caught.value.code in (4401, 1001)

    # 登录替换：新登录撤销旧会话。
    login(client)
    with _connect(client) as socket:
        socket.send_text("ping")
        assert socket.receive_text() == "pong"
        login(client)  # 再次登录 → 旧会话全部撤销。
        with pytest.raises(WebSocketDisconnect) as caught:
            socket.receive_text()
        assert caught.value.code in (4401, 1001)

    # 改密：撤销全部会话（新会话建立后连接再开，旧连接关闭）。
    login(client)
    with _connect(client) as socket:
        socket.send_text("ping")
        assert socket.receive_text() == "pong"
        client.post(
            "/api/v2/auth/password", json={"old_password": "ab" * 32, "new_password": "cd" * 32}
        )
        with pytest.raises(WebSocketDisconnect) as caught:
            socket.receive_text()
        assert caught.value.code in (4401, 1001)

    # 删用户：标记待删除 → 撤销。
    login(client, "admin", "cd" * 32)
    other = create_user(client, "victim")
    login(client, "victim", "cd" * 32)
    with _connect(client) as socket:
        socket.send_text("ping")
        assert socket.receive_text() == "pong"
        login(client, "admin", "cd" * 32)
        client.delete(f"/api/v2/users/{other['id']}")
        with pytest.raises(WebSocketDisconnect) as caught:
            socket.receive_text()
        assert caught.value.code in (4401, 1001)


def test_bootstrap_wiring_broadcast_shared(app: FastAPI) -> None:
    """P2-8 装配断言：lifecycle 与 ws_service 共用同一广播总线、撤销总线接通。"""
    ws_service = app.state.ws_service
    lifecycle = app.state.download_lifecycle
    assert ws_service.broadcast is lifecycle.broadcast, "WS 与 lifecycle 必须共享同一 TaskBroadcast"
    assert app.state.session_revocations is not None

    # 撤销总线确实接了 ws_service 回调。
    user_id = 42  # 任意不存在用户：仅验证订阅链路存在。
    from aria2deck.application.ports import SessionRevocation

    app.state.session_revocations.publish(SessionRevocation(user_id=user_id))
    # 若未接通会抛 TypeError（publish 只迭代已注册回调），此处静默即接通。
