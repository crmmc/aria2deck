from __future__ import annotations

import asyncio
import base64
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from aria2deck.application.downloads.models import DownloadObservation, ObservedFile
from aria2deck.bootstrap import create_app
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork
from tests.api.conftest import create_admin, login
from tests.download_fakes import FakeSubmissionBackend

WRONG_SECRET = "错误令牌"  # noqa: S105  # 测试夹具令牌，非凭据


class SimpleCors:
    def __init__(self, *, allow_all: bool, allowed_origins: frozenset[str]) -> None:
        self.allow_all = allow_all
        self.allowed_origins = allowed_origins


_CURRENT_SECRET: dict[str, str] = {}


def _enable_rpc(client: TestClient) -> None:
    """登录后开启 RPC 访问并取回原文 Secret（仅显示一次）。"""
    response = client.put("/api/v2/credentials/rpc", json={"enabled": True})
    assert response.status_code == 200, response.text
    secret = response.json()["secret"]
    assert secret
    _CURRENT_SECRET["value"] = secret


def _secret() -> str:
    return _CURRENT_SECRET["value"]


def _rpc(
    client: TestClient, method: str, params: list[object] | None = None, *, token: str | None = None
) -> object:
    effective = _secret() if token is None else token
    payload = {"jsonrpc": "2.0", "id": "1", "method": method, "params": [f"token:{effective}", *(params or [])]}
    return client.post("/aria2/jsonrpc", json=payload)


def _seed_task(settings, user_id: int, *, status: str = "active", name: str = "demo.bin") -> tuple[int, str]:
    now_ms = time.time_ns() // 1_000_000  # 当前时间基准：1970 时间戳会被历史保留 worker 过期（已知坑）。

    async def write() -> tuple[int, str]:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                repository = DownloadRepository(uow.connection)
                source_id = await repository.create_source(
                    kind="http",
                    resource_key=f"http:rpc-{user_id}-{name}",
                    payload_json="{}",
                    options_json="{}",
                    selected_file_indexes_json=None,
                    display_name=name,
                    display_uri="https://example.com/展示链接",
                    now_ms=now_ms,
                )
                attempt_id = await repository.create_attempt(
                    source_id=source_id,
                    resource_key=f"http:rpc-{user_id}-{name}",
                    gid="feedface0123456789abcdef",
                    now_ms=now_ms,
                )
                task_id = await repository.create_subscription(
                    attempt_id=attempt_id,
                    user_id=user_id,
                    reserved_bytes=0,
                    display_name=name,
                    display_uri=None,
                    now_ms=now_ms,
                )
                await uow.connection.execute(
                    text("UPDATE task_subscriptions SET status = :status WHERE id = :id"),
                    {"status": status, "id": task_id},
                )
                return task_id, "feedface0123456789abcdef"
        finally:
            await database.close()

    return asyncio.run(write())


def _seed_completed_content(
    settings,
    user_id: int,
    *,
    name: str = "completed.bin",
    size: int = 577,
    kind: str = "file",
    entries: tuple[tuple[str, str, int], ...] = (),
) -> tuple[int, str]:
    now_ms = time.time_ns() // 1_000_000

    async def write() -> tuple[int, str]:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                downloads = DownloadRepository(uow.connection)
                content = ContentRepository(uow.connection)
                content_id = await content.create_object(
                    kind=kind,
                    digest=("ab" if kind == "file" else "cd") * 32,
                    size=size,
                    storage_path=f"objects/completed-rpc-{kind}",
                    now_ms=now_ms,
                )
                for path, entry_kind, entry_size in entries:
                    parent_path, _, entry_name = path.rpartition("/")
                    await content.add_entry(
                        content_id=content_id,
                        parent_path=parent_path,
                        name=entry_name,
                        entry_kind=entry_kind,
                        size=entry_size,
                        now_ms=now_ms,
                    )
                source_id = await downloads.create_source(
                    kind="http",
                    resource_key=f"http:rpc-completed-{user_id}-{name}",
                    payload_json="{}",
                    options_json="{}",
                    selected_file_indexes_json=None,
                    display_name=name,
                    display_uri="https://example.com/completed.bin",
                    now_ms=now_ms,
                )
                gid = "feedface0123456789abcdef"
                attempt_id = await downloads.create_attempt(
                    source_id=source_id,
                    resource_key=f"http:rpc-completed-{user_id}-{name}",
                    gid=gid,
                    now_ms=now_ms,
                )
                task_id = await downloads.create_subscription(
                    attempt_id=attempt_id,
                    user_id=user_id,
                    reserved_bytes=0,
                    display_name=name,
                    display_uri="https://example.com/completed.bin",
                    now_ms=now_ms,
                )
                await downloads.begin_completion(
                    attempt_id,
                    expected_gid=gid,
                    content_id=content_id,
                    now_ms=now_ms + 1,
                )
                assert await downloads.finalize_completion(
                    attempt_id, expected_gid=gid, now_ms=now_ms + 2
                )
                return task_id, gid
        finally:
            await database.close()

    return asyncio.run(write())


@pytest.fixture
def rpc_client(client: TestClient) -> TestClient:
    _CURRENT_SECRET.clear()
    create_admin(client)
    login(client)
    _enable_rpc(client)
    # 真实 aria2 不在测试环境运行：换假提交后端，业务链路保持真实执行。
    backend = FakeSubmissionBackend()
    backend.mark_confirmed_on_submit = True
    client.app.state.download_commands.backend = backend
    client.app.state.download_recovery.backend = backend
    return client


def test_completed_single_file_is_projected_after_observation_is_empty(
    rpc_client: TestClient, settings
) -> None:
    user_id = int(rpc_client.get("/api/v2/auth/me").json()["id"])
    task_id, _ = _seed_completed_content(settings, user_id)
    stopped = _rpc(rpc_client, "aria2.tellStopped", [0, 10]).json()["result"]
    gid = next(item["gid"] for item in stopped if item["status"] == "complete")
    expected = {
        "index": "1",
        "path": "completed.bin",
        "length": "577",
        "completedLength": "577",
        "selected": "true",
        "uris": [],
    }

    status = _rpc(rpc_client, "aria2.tellStatus", [gid])
    assert status.json()["result"]["files"] == [expected]
    files = _rpc(rpc_client, "aria2.getFiles", [gid])
    assert files.json()["result"] == [expected]
    rest = rpc_client.get(f"/api/v2/tasks/{task_id}")
    assert rest.json()["files"] == [
        {
            "name": "completed.bin",
            "total_bytes": 577,
            "completed_bytes": 577,
            "selected": True,
        }
    ]
    projection = asyncio.run(
        rpc_client.app.state.download_queries.get_task(user_id=user_id, task_id=task_id)
    )
    assert projection.files == (
        ObservedFile(
            name="completed.bin",
            total_bytes=577,
            completed_bytes=577,
            selected=True,
        ),
    )


def test_completed_directory_projects_sorted_real_files_and_safe_rpc_basenames(
    rpc_client: TestClient, settings
) -> None:
    user_id = int(rpc_client.get("/api/v2/auth/me").json()["id"])
    task_id, gid = _seed_completed_content(
        settings,
        user_id,
        name="bundle",
        size=11,
        kind="directory",
        entries=(
            ("z-last.bin", "file", 5),
            ("folder", "directory", 0),
            ("folder/nested/b.bin", "file", 2),
            ("folder/nested", "directory", 0),
            ("folder/a-first.bin", "file", 4),
        ),
    )
    expected = [
        {
            "index": "1",
            "path": "a-first.bin",
            "length": "4",
            "completedLength": "4",
            "selected": "true",
            "uris": [],
        },
        {
            "index": "2",
            "path": "b.bin",
            "length": "2",
            "completedLength": "2",
            "selected": "true",
            "uris": [],
        },
        {
            "index": "3",
            "path": "z-last.bin",
            "length": "5",
            "completedLength": "5",
            "selected": "true",
            "uris": [],
        },
    ]

    files = _rpc(rpc_client, "aria2.getFiles", [gid])
    assert files.json()["result"] == expected
    assert "folder/" not in files.text
    rest = rpc_client.get(f"/api/v2/tasks/{task_id}").json()["files"]
    assert rest == [
        {
            "name": path,
            "total_bytes": size,
            "completed_bytes": size,
            "selected": True,
        }
        for path, size in (
            ("folder/a-first.bin", 4),
            ("folder/nested/b.bin", 2),
            ("z-last.bin", 5),
        )
    ]
    projection = asyncio.run(
        rpc_client.app.state.download_queries.get_task(user_id=user_id, task_id=task_id)
    )
    assert projection.files == (
        ObservedFile("folder/a-first.bin", 4, 4, True),
        ObservedFile("folder/nested/b.bin", 2, 2, True),
        ObservedFile("z-last.bin", 5, 5, True),
    )


def test_completed_files_prefer_nonempty_live_observation(
    rpc_client: TestClient, settings
) -> None:
    user_id = int(rpc_client.get("/api/v2/auth/me").json()["id"])
    task_id, gid = _seed_completed_content(settings, user_id)
    rpc_client.app.state.download_observations.put(
        DownloadObservation(
            gid=gid,
            observed_at_ms=time.time_ns() // 1_000_000,
            status="complete",
            download_speed=0,
            upload_speed=0,
            peer_count=0,
            connection_count=0,
            files=(
                ObservedFile(
                    name="live/current.bin",
                    total_bytes=900,
                    completed_bytes=300,
                    selected=False,
                ),
            ),
        )
    )

    status = _rpc(rpc_client, "aria2.tellStatus", [gid]).json()["result"]
    assert status["files"] == [
        {
            "index": "1",
            "path": "current.bin",
            "length": "900",
            "completedLength": "300",
            "selected": "false",
            "uris": [],
        }
    ]
    rest = rpc_client.get(f"/api/v2/tasks/{task_id}").json()["files"]
    assert rest == [
        {
            "name": "live/current.bin",
            "total_bytes": 900,
            "completed_bytes": 300,
            "selected": False,
        }
    ]


def test_completed_files_remain_user_scoped(rpc_client: TestClient, settings) -> None:
    from tests.api.conftest import create_user

    owner_id = int(rpc_client.get("/api/v2/auth/me").json()["id"])
    task_id, gid = _seed_completed_content(settings, owner_id)
    create_user(rpc_client, "intruder")
    login(rpc_client, "intruder", "cd" * 32)
    _enable_rpc(rpc_client)

    for method in ("aria2.tellStatus", "aria2.getFiles"):
        response = _rpc(rpc_client, method, [gid])
        assert response.json()["error"]["code"] == 1
        assert "completed.bin" not in response.text
    assert rpc_client.get(f"/api/v2/tasks/{task_id}").status_code == 404


def test_completed_files_survive_restart_with_empty_observations(settings) -> None:
    from tests.application.test_download_lifecycle import LifecycleBackend

    first_app = create_app(settings=settings)
    first_backend = LifecycleBackend()
    first_app.state.download_commands.backend = first_backend
    first_app.state.download_recovery.backend = first_backend
    first_app.state.download_lifecycle.backend = first_backend
    with TestClient(first_app) as first:
        create_admin(first)
        login(first)
        _enable_rpc(first)
        secret = _secret()
        user_id = int(first.get("/api/v2/auth/me").json()["id"])
        _, gid = _seed_completed_content(settings, user_id)

    restarted_app = create_app(settings=settings)
    restarted_backend = LifecycleBackend()
    restarted_app.state.download_commands.backend = restarted_backend
    restarted_app.state.download_recovery.backend = restarted_backend
    restarted_app.state.download_lifecycle.backend = restarted_backend
    with TestClient(restarted_app) as restarted:
        login(restarted)
        assert restarted.app.state.download_observations.get(gid) is None
        status = _rpc(restarted, "aria2.tellStatus", [gid], token=secret)
        assert status.json()["result"]["files"] == [
            {
                "index": "1",
                "path": "completed.bin",
                "length": "577",
                "completedLength": "577",
                "selected": "true",
                "uris": [],
            }
        ]


def test_bootstrap_reuses_content_repository_for_completed_files(app: FastAPI) -> None:
    assert (
        app.state.download_queries.content_repository_factory
        is app.state.library_queries.repository_factory
    )


def test_get_method_is_rejected(rpc_client: TestClient) -> None:
    assert rpc_client.get("/aria2/jsonrpc").status_code == 405


def test_parse_error_and_invalid_envelope(rpc_client: TestClient) -> None:
    raw = rpc_client.post("/aria2/jsonrpc", content=b"{not json", headers={"Content-Type": "application/json"})
    assert raw.status_code == 200
    assert raw.json()["error"]["code"] == -32700

    bad_envelope = rpc_client.post(
        "/aria2/jsonrpc", json={"jsonrpc": "1.0", "id": 1, "method": "aria2.getVersion"}
    )
    assert bad_envelope.json()["error"]["code"] == -32600
    missing_method = rpc_client.post(
        "/aria2/jsonrpc", json={"jsonrpc": "2.0", "id": 1}
    )
    assert missing_method.json()["error"]["code"] == -32600


def test_unknown_method_and_internal_error_shape(rpc_client: TestClient) -> None:
    response = _rpc(rpc_client, "aria2.changeOption", ["g", {}])
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32601
    assert "不存在" in response.json()["error"]["message"]


def test_token_three_states(rpc_client: TestClient) -> None:
    ok = _rpc(rpc_client, "system.listMethods")
    assert ok.status_code == 200
    assert len(ok.json()["result"]) == 30

    wrong = _rpc(rpc_client, "system.listMethods", token=WRONG_SECRET)
    assert wrong.status_code == 200
    assert wrong.json()["error"]["code"] == 1

    missing = rpc_client.post(
        "/aria2/jsonrpc",
        json={"jsonrpc": "2.0", "id": 7, "method": "system.listMethods", "params": []},
    )
    assert missing.status_code == 200
    assert missing.json()["error"]["code"] == 1
    assert missing.json()["id"] == 7


def test_notification_without_id_returns_204(rpc_client: TestClient) -> None:
    notification = rpc_client.post(
        "/aria2/jsonrpc",
        json={"jsonrpc": "2.0", "method": "system.listMethods", "params": [f"token:{_secret()}"]},
    )
    assert notification.status_code == 204

    # 缺 token 的通知同样 204；带 id 的错误请求才有响应体。
    broken_notification = rpc_client.post(
        "/aria2/jsonrpc", json={"jsonrpc": "2.0", "method": "system.listMethods"}
    )
    assert broken_notification.status_code == 204


def test_rate_limited_single_notification_returns_204(rpc_client: TestClient) -> None:
    rpc_client.patch("/api/v2/settings", json={"rpc_rate_limit": 1})
    token = f"token:{_secret()}"
    payload = {"jsonrpc": "2.0", "method": "system.listMethods", "params": [token]}

    first = rpc_client.post("/aria2/jsonrpc", json=payload)
    assert first.status_code == 204  # 未限流：通知执行后无响应

    limited = rpc_client.post("/aria2/jsonrpc", json=payload)
    assert limited.status_code == 204  # 修复前此处为 200 + -32000 错误对象
    assert limited.content == b""


def test_rate_limited_pure_notification_batch_returns_204(rpc_client: TestClient) -> None:
    rpc_client.patch("/api/v2/settings", json={"rpc_rate_limit": 1})
    token = f"token:{_secret()}"

    consume = rpc_client.post(
        "/aria2/jsonrpc",
        json={"jsonrpc": "2.0", "id": "consume", "method": "system.listMethods", "params": [token]},
    )
    assert consume.status_code == 200  # 用带 id 请求消耗本窗口唯一配额

    limited_batch = rpc_client.post(
        "/aria2/jsonrpc",
        json=[
            {"jsonrpc": "2.0", "method": "system.listMethods", "params": [token]},
            {"jsonrpc": "2.0", "method": "aria2.tellActive", "params": [token]},
        ],
    )
    assert limited_batch.status_code == 204  # 修复前此处为 200 + -32000 错误对象
    assert limited_batch.content == b""


def test_rate_limited_mixed_batch_keeps_batch_error(rpc_client: TestClient) -> None:
    rpc_client.patch("/api/v2/settings", json={"rpc_rate_limit": 1})
    token = f"token:{_secret()}"

    consume = rpc_client.post(
        "/aria2/jsonrpc",
        json={"jsonrpc": "2.0", "id": "consume", "method": "system.listMethods", "params": [token]},
    )
    assert consume.status_code == 200

    limited_mixed = rpc_client.post(
        "/aria2/jsonrpc",
        json=[
            {"jsonrpc": "2.0", "id": "m1", "method": "system.listMethods", "params": [token]},
            {"jsonrpc": "2.0", "method": "aria2.tellActive", "params": [token]},
        ],
    )
    # 带 id 请求的批次维持批次级 -32000 错误结构（与修复前一致）。
    assert limited_mixed.status_code == 200
    assert limited_mixed.json()["error"]["code"] == -32000
    assert "Retry-After" in limited_mixed.headers


def test_rate_limited_invalid_envelope_without_id_keeps_error(rpc_client: TestClient) -> None:
    rpc_client.patch("/api/v2/settings", json={"rpc_rate_limit": 1})
    token = f"token:{_secret()}"

    consume = rpc_client.post(
        "/aria2/jsonrpc",
        json={"jsonrpc": "2.0", "id": "consume", "method": "system.listMethods", "params": [token]},
    )
    assert consume.status_code == 200

    limited_invalid = rpc_client.post("/aria2/jsonrpc", json={"jsonrpc": "2.0"})
    # 非法信封（无 method）不是合法通知：维持明确的 -32000 协议错误。
    assert limited_invalid.status_code == 200
    assert limited_invalid.json()["error"]["code"] == -32000


def _consume_only_rpc_quota(rpc_client: TestClient) -> None:
    """用带 id 请求消耗 rpc_rate_limit=1 的本窗口唯一配额。"""
    consume = rpc_client.post(
        "/aria2/jsonrpc",
        json={
            "jsonrpc": "2.0",
            "id": "consume",
            "method": "system.listMethods",
            "params": [f"token:{_secret()}"],
        },
    )
    assert consume.status_code == 200


def test_rate_limited_multicall_notification_returns_204(rpc_client: TestClient) -> None:
    rpc_client.patch("/api/v2/settings", json={"rpc_rate_limit": 1})
    _consume_only_rpc_quota(rpc_client)

    limited_multicall = rpc_client.post(
        "/aria2/jsonrpc",
        json={
            "jsonrpc": "2.0",
            "method": "system.multicall",
            "params": [[{"methodName": "system.listMethods", "params": []}]],
        },
    )
    assert limited_multicall.status_code == 204
    assert limited_multicall.content == b""


def test_rate_limited_oversized_notification_batch_keeps_error(rpc_client: TestClient) -> None:
    rpc_client.patch("/api/v2/settings", json={"rpc_rate_limit": 1})
    _consume_only_rpc_quota(rpc_client)
    token = f"token:{_secret()}"

    oversized_notifications = [
        {"jsonrpc": "2.0", "method": "system.listMethods", "params": [token]} for _ in range(21)
    ]
    limited_oversized = rpc_client.post("/aria2/jsonrpc", json=oversized_notifications)
    # 超 20 项的批次维持限流错误：边界检查的既有优先级高于通知语义。
    assert limited_oversized.status_code == 200
    assert limited_oversized.json()["error"]["code"] == -32000


def test_rate_limited_batch_with_invalid_item_keeps_error(rpc_client: TestClient) -> None:
    rpc_client.patch("/api/v2/settings", json={"rpc_rate_limit": 1})
    _consume_only_rpc_quota(rpc_client)
    token = f"token:{_secret()}"

    limited_batch = rpc_client.post(
        "/aria2/jsonrpc",
        json=[
            {"jsonrpc": "2.0"},  # 非法信封项：批次不算纯合法通知
            {"jsonrpc": "2.0", "method": "system.listMethods", "params": [token]},
        ],
    )
    assert limited_batch.status_code == 200
    assert limited_batch.json()["error"]["code"] == -32000


def test_rate_limited_empty_batch_keeps_error(rpc_client: TestClient) -> None:
    rpc_client.patch("/api/v2/settings", json={"rpc_rate_limit": 1})
    _consume_only_rpc_quota(rpc_client)

    limited_empty = rpc_client.post("/aria2/jsonrpc", json=[])
    # 空批不是合法通知：维持限流错误（非限流路径仍由既有 -32600 处理）。
    assert limited_empty.status_code == 200
    assert limited_empty.json()["error"]["code"] == -32000


def test_rate_limited_non_object_body_keeps_error(rpc_client: TestClient) -> None:
    rpc_client.patch("/api/v2/settings", json={"rpc_rate_limit": 1})
    _consume_only_rpc_quota(rpc_client)

    limited_scalar = rpc_client.post("/aria2/jsonrpc", json=42)
    assert limited_scalar.status_code == 200
    assert limited_scalar.json()["error"]["code"] == -32000


def test_batch_semantics(rpc_client: TestClient) -> None:
    token = f"token:{_secret()}"
    batch = rpc_client.post(
        "/aria2/jsonrpc",
        json=[
            {"jsonrpc": "2.0", "id": 1, "method": "system.listMethods", "params": [token]},
            {"jsonrpc": "2.0", "method": "aria2.tellActive", "params": [token]},  # 通知被过滤
            {"jsonrpc": "2.0", "id": 2, "method": "nope.method", "params": [token]},
            {"jsonrpc": "2.0", "id": 3, "method": "aria2.getVersion"},  # 缺 token：code 1
        ],
    )
    assert batch.status_code == 200
    body = batch.json()
    assert [item["id"] for item in body] == [1, 2, 3]
    assert len(body[0]["result"]) == 30
    assert body[1]["error"]["code"] == -32601
    assert body[2]["error"]["code"] == 1

    empty = rpc_client.post("/aria2/jsonrpc", json=[])
    assert empty.json()["error"]["code"] == -32600

    oversized = [
        {"jsonrpc": "2.0", "id": index, "method": "system.listMethods", "params": [token]}
        for index in range(21)
    ]
    assert rpc_client.post("/aria2/jsonrpc", json=oversized).json()["error"]["code"] == -32600

    all_notifications = rpc_client.post(
        "/aria2/jsonrpc",
        json=[{"jsonrpc": "2.0", "method": "aria2.tellActive", "params": [token]}] * 3,
    )
    assert all_notifications.status_code == 204


def test_multicall_wrapper_and_nesting_rejection(rpc_client: TestClient) -> None:
    token = f"token:{_secret()}"
    response = rpc_client.post(
        "/aria2/jsonrpc",
        json={
            "jsonrpc": "2.0",
            "id": 9,
            "method": "system.multicall",
            "params": [
                token,
                [
                    {"methodName": "aria2.getVersion", "params": []},
                    {"methodName": "aria2.pause", "params": ["g"]},
                ],
            ],
        },
    )
    assert response.status_code == 200
    results = response.json()["result"]
    assert isinstance(results[0], list) and results[0][0]["version"]
    assert results[1]["faultCode"] == 1

    nested = rpc_client.post(
        "/aria2/jsonrpc",
        json={
            "jsonrpc": "2.0",
            "id": 10,
            "method": "system.multicall",
            "params": [token, [{"methodName": "system.multicall", "params": []}]],
        },
    )
    body = nested.json()["result"][0]
    assert body["faultCode"] == -32602

    oversized = rpc_client.post(
        "/aria2/jsonrpc",
        json={
            "jsonrpc": "2.0",
            "id": 11,
            "method": "system.multicall",
            "params": [token, [{"methodName": "aria2.getVersion"}] * 21],
        },
    )
    assert oversized.json()["error"]["code"] == -32602


def test_rate_limit_returns_200_with_error_and_retry_after(client: TestClient) -> None:
    create_admin(client)
    login(client)
    response = client.patch("/api/v2/settings", json={"rpc_rate_limit": 1})
    assert response.status_code == 200
    first = client.post(
        "/aria2/jsonrpc", json={"jsonrpc": "2.0", "id": 1, "method": "system.listMethods"}
    )
    assert first.status_code == 200
    assert first.json()["error"]["code"] in {1, -32601} or "result" in first.json()
    second = client.post(
        "/aria2/jsonrpc", json={"jsonrpc": "2.0", "id": 2, "method": "system.listMethods"}
    )
    assert second.status_code == 200
    assert second.json()["error"]["code"] == -32000
    assert "Retry-After" in second.headers


def test_cors_whitelist_and_reject(client: TestClient, app: FastAPI) -> None:
    app.state.rpc_cors = SimpleCors(allow_all=False, allowed_origins=frozenset({"https://ariang.example"}))
    preflight = client.options(
        "/aria2/jsonrpc", headers={"Origin": "https://ariang.example"}
    )
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == "https://ariang.example"
    assert preflight.headers["access-control-allow-credentials"] == "true"

    foreign = client.options("/aria2/jsonrpc", headers={"Origin": "https://evil.example"})
    assert foreign.status_code == 403

    unknown = client.options("/aria2/jsonrpc")
    assert unknown.status_code == 403


def test_cors_allow_all_mode_has_no_credentials(client: TestClient, app: FastAPI) -> None:
    app.state.rpc_cors = SimpleCors(allow_all=True, allowed_origins=frozenset())
    preflight = client.options("/aria2/jsonrpc", headers={"Origin": "https://anywhere.example"})
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in preflight.headers
    post = client.post(
        "/aria2/jsonrpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "system.listMethods"},
        headers={"Origin": "https://anywhere.example"},
    )
    assert post.headers.get("access-control-allow-origin") == "*"


def test_projection_safety_full_scan(rpc_client: TestClient, settings) -> None:
    user_id = int(rpc_client.get("/api/v2/auth/me").json()["id"])
    _seed_task(settings, user_id)

    payloads: list[str] = []
    for method, params in [
        ("aria2.tellActive", []),
        ("aria2.tellWaiting", [0, 10]),
        ("aria2.tellStopped", [0, 10]),
        ("aria2.getGlobalStat", []),
        ("aria2.getOption", ["0000000000000000"]),
        ("aria2.getGlobalOption", []),
    ]:
        response = _rpc(rpc_client, method, params)
        payloads.append(response.text)
    status = _rpc(rpc_client, "aria2.tellStatus", ["0000000000000000"])
    payloads.append(status.text)
    blob = "\n".join(payloads)

    assert "feedface" not in blob, "真实 aria2 GID 不得出现"
    assert "objects/" not in blob and "/downloads" not in blob, "内部路径不得出现"
    assert "capability" not in blob.lower(), "签名 capability 不得出现"
    assert "X-Mjiz" not in blob, "capability 头名不得出现"
    # 展示 URI 是用户提交的公开形态，允许出现；内部签名 URI 绝不出现。


def test_raw_gid_query_echoes_request_string(rpc_client: TestClient, settings) -> None:
    user_id = int(rpc_client.get("/api/v2/auth/me").json()["id"])
    _seed_task(settings, user_id)
    response = _rpc(rpc_client, "aria2.tellStatus", ["feedface0123456789abcdef"])
    assert response.status_code == 200
    body = response.json()
    assert "result" in body
    assert body["result"]["gid"] == "feedface0123456789abcdef"


def test_adduri_rejects_ftp_and_path_out(rpc_client: TestClient) -> None:
    ftp = _rpc(rpc_client, "aria2.addUri", [["ftp://example.com/file.bin"]])
    assert ftp.json()["error"]["code"] == 1
    out = _rpc(
        rpc_client, "aria2.addUri", [["https://example.com/file.bin"], {"out": "/etc/passwd"}]
    )
    assert out.json()["error"]["code"] == 1
    tracker = _rpc(
        rpc_client,
        "aria2.addUri",
        [["https://example.com/file.bin"], {"bt-tracker": "http://t/announce"}],
    )
    assert tracker.json()["error"]["code"] == 1


def test_adduri_creates_and_is_idempotent(rpc_client: TestClient, settings) -> None:
    created = _rpc(rpc_client, "aria2.addUri", [["https://example.com/rpc-create.bin"]])
    assert "result" in created.json(), created.text
    gid = created.json()["result"]
    assert len(gid) == 16
    duplicate = _rpc(rpc_client, "aria2.addUri", [["https://example.com/rpc-create.bin"]])
    assert duplicate.json()["result"] == gid
    status = _rpc(rpc_client, "aria2.tellStatus", [gid])
    assert status.json()["result"]["gid"] == gid


def test_remove_cancels_and_rejects_terminal(rpc_client: TestClient, settings) -> None:
    user_id = int(rpc_client.get("/api/v2/auth/me").json()["id"])
    _seed_task(settings, user_id, status="active", name="cancel-me.bin")
    listed = _rpc(rpc_client, "aria2.tellActive", [])
    gid = listed.json()["result"][0]["gid"]
    removed = _rpc(rpc_client, "aria2.remove", [gid])
    assert removed.json()["result"] == gid
    after = _rpc(rpc_client, "aria2.tellStopped", [0, 10])
    statuses = {item["status"] for item in after.json()["result"]}
    assert statuses <= {"complete", "error", "removed"}

    terminal = _rpc(rpc_client, "aria2.remove", [gid])
    assert terminal.json()["error"]["code"] == 1
    unknown = _rpc(rpc_client, "aria2.remove", ["aaaaaaaaaaaaaaaa"])
    assert unknown.json()["error"]["code"] == 1


@pytest.mark.parametrize("mode", ["batch", "multicall"])
def test_mixed_remove_results_keep_per_item_errors(
    rpc_client: TestClient, mode: str
) -> None:
    confirmed = _rpc(
        rpc_client, "aria2.addUri", [["https://example.com/rpc-confirmed.bin"]]
    )
    unconfirmed = _rpc(
        rpc_client, "aria2.addUri", [["https://example.com/rpc-unconfirmed.bin"]]
    )
    assert "result" in confirmed.json(), confirmed.text
    assert "result" in unconfirmed.json(), unconfirmed.text
    confirmed_gid = confirmed.json()["result"]
    unconfirmed_gid = unconfirmed.json()["result"]

    backend = rpc_client.app.state.download_commands.backend
    assert isinstance(backend, FakeSubmissionBackend)
    confirmed_raw_gid = backend.submit_calls[0][0].gid
    unconfirmed_raw_gid = backend.submit_calls[1][0].gid

    async def stop(*, gid: str) -> None:
        backend.stop_calls.append(gid)
        if gid == unconfirmed_raw_gid:
            raise DomainError(ErrorCode.RPC_UNAVAILABLE, "停止超时")

    backend.stop = stop
    token = f"token:{_secret()}"
    if mode == "batch":
        response = rpc_client.post(
            "/aria2/jsonrpc",
            json=[
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "aria2.remove",
                    "params": [token, confirmed_gid],
                },
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "aria2.forceRemove",
                    "params": [token, unconfirmed_gid],
                },
            ],
        )
        body = response.json()
        assert body[0]["result"] == confirmed_gid
        assert "error" in body[1]
        assert body[1]["error"]["code"] == 1
        assert "停止结果无法确认" in body[1]["error"]["message"]
    else:
        response = rpc_client.post(
            "/aria2/jsonrpc",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "system.multicall",
                "params": [
                    token,
                    [
                        {"methodName": "aria2.remove", "params": [confirmed_gid]},
                        {
                            "methodName": "aria2.forceRemove",
                            "params": [unconfirmed_gid],
                        },
                    ],
                ],
            },
        )
        results = response.json()["result"]
        assert results[0] == [confirmed_gid]
        assert "faultCode" in results[1]
        assert results[1]["faultCode"] == 1
        assert "停止结果无法确认" in results[1]["faultString"]

    assert set(backend.stop_calls) == {confirmed_raw_gid, unconfirmed_raw_gid}


def test_addtorrent_rejects_bad_options(rpc_client: TestClient) -> None:
    payload = base64.b64encode(b"not-a-torrent").decode()
    webseed = _rpc(
        rpc_client,
        "aria2.addTorrent",
        [payload, {"follow-web-seed": "http://mirror/"}],
    )
    assert webseed.json()["error"]["code"] == 1
    tracker = _rpc(rpc_client, "aria2.addTorrent", [payload, {"bt-tracker": "http://t/a"}])
    assert tracker.json()["error"]["code"] == 1
    invalid = _rpc(rpc_client, "aria2.addTorrent", [payload])
    assert invalid.json()["error"]["code"] == 1


def test_removed_projection_for_cancelled_tasks(rpc_client: TestClient, settings) -> None:
    user_id = int(rpc_client.get("/api/v2/auth/me").json()["id"])
    _seed_task(settings, user_id, status="cancelled", name="removed-check.bin")
    stopped = _rpc(rpc_client, "aria2.tellStopped", [0, 50])
    names = {item["status"] for item in stopped.json()["result"]}
    assert "removed" in names


def test_multicall_token_in_subcall_ariang_form(rpc_client: TestClient) -> None:
    """P1-1：AriaNg 标准形态——外层无 token，token 在每个子调用的 params[0]。"""
    secret = _secret()
    response = rpc_client.post(
        "/aria2/jsonrpc",
        json={
            "jsonrpc": "2.0",
            "id": "mc1",
            "method": "system.multicall",
            "params": [
                [
                    {
                        "methodName": "aria2.getVersion",
                        "params": [f"token:{secret}"],
                    },
                    {
                        "methodName": "system.listMethods",
                        "params": [f"token:{secret}"],
                    },
                ]
            ],
        },
    )
    assert response.status_code == 200
    results = response.json()["result"]
    assert isinstance(results[0], list) and results[0][0]["version"]
    assert len(results[1][0]) == 30

    # 多子调用 token 不一致：以第一个为准（版本查询成功，同用户执行）。
    mixed = rpc_client.post(
        "/aria2/jsonrpc",
        json={
            "jsonrpc": "2.0",
            "id": "mc2",
            "method": "system.multicall",
            "params": [
                [
                    {"methodName": "aria2.getVersion", "params": [f"token:{secret}"]},
                    {"methodName": "aria2.getVersion", "params": ["token:wrong-secret"]},
                ]
            ],
        },
    )
    assert mixed.status_code == 200
    assert isinstance(mixed.json()["result"][0], list)


def test_rate_limit_charges_per_execution_count(client: TestClient) -> None:
    """P2-6：multicall 按子调用数计费。"""
    create_admin(client)
    login(client)
    client.patch("/api/v2/settings", json={"rpc_rate_limit": 20})
    secret_response = client.put("/api/v2/credentials/rpc", json={"enabled": True})
    secret = secret_response.json()["secret"]

    calls = [{"methodName": "aria2.getVersion", "params": [f"token:{secret}"]} for _ in range(20)]
    burst = client.post(
        "/aria2/jsonrpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "system.multicall", "params": [calls]},
    )
    assert burst.status_code == 200
    assert "result" in burst.json()

    throttled = client.post(
        "/aria2/jsonrpc",
        json={"jsonrpc": "2.0", "id": 2, "method": "aria2.getVersion", "params": [f"token:{secret}"]},
    )
    assert throttled.json()["error"]["code"] == -32000
    assert "Retry-After" in throttled.headers


def test_internal_error_returns_32603_in_single_and_batch(client: TestClient, app: FastAPI) -> None:
    """P2-5：非 DomainError 异常 → -32603 中文且不泄露原文（单请求与 batch 内两处）。"""
    create_admin(client)
    login(client)
    secret = client.put("/api/v2/credentials/rpc", json={"enabled": True}).json()["secret"]

    class ExplodingService:
        async def dispatch(self, method: str, params: list, *, user_id: int) -> object:
            raise RuntimeError("数据库连接池耗尽 /d/internal/secret/path")

    original = app.state.aria2_rpc_service
    app.state.aria2_rpc_service = ExplodingService()
    try:
        single = client.post(
            "/aria2/jsonrpc",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "aria2.getVersion",
                "params": [f"token:{secret}"],
            },
        )
        assert single.status_code == 200
        error = single.json()["error"]
        assert error["code"] == -32603
        assert error["message"] == "服务器内部错误"
        assert "数据库" not in error["message"] and "/d/" not in single.text

        batch = client.post(
            "/aria2/jsonrpc",
            json=[
                {
                    "jsonrpc": "2.0", "id": 1, "method": "aria2.getVersion",
                    "params": [f"token:{secret}"],
                },
                {"jsonrpc": "2.0", "method": "aria2.getVersion", "params": [f"token:{secret}"]},
            ],
        )
        assert batch.status_code == 200
        body = batch.json()
        assert len(body) == 1  # 通知不产生响应。
        assert body[0]["error"]["code"] == -32603
        assert "RuntimeError" not in batch.text
    finally:
        app.state.aria2_rpc_service = original


def test_addtorrent_canonical_param_positions(rpc_client: TestClient) -> None:
    """P2-3：规范形态 [b64, uris[], options{}]；非空 uris（webseed）拒绝。"""
    payload = base64.b64encode(b"not-a-torrent").decode()
    webseed = _rpc(
        rpc_client,
        "aria2.addTorrent",
        [payload, ["http://mirror/file.bin"], {"select-file": "1"}],
    )
    assert webseed.json()["error"]["code"] == 1
    assert "webseed" in webseed.json()["error"]["message"]

    # 空 uris 列表 + options 在 params[2] 是合法形态（种子本身无效但参数解析通过）。
    empty_uris = _rpc(rpc_client, "aria2.addTorrent", [payload, [], {"select-file": "1"}])
    assert "不支持通过种子 URI 列表提供 webseed" not in empty_uris.text
    assert empty_uris.json()["error"]["code"] == 1  # 种子无效，code 1。


def test_get_405_has_allow_header(client: TestClient) -> None:
    response = client.get("/aria2/jsonrpc")
    assert response.status_code == 405
    assert response.headers.get("allow") == "POST, OPTIONS"


def test_rpc_isolation_from_other_users(rpc_client: TestClient, settings) -> None:
    """P3 #14：他人任务在任何响应中零出现（双用户数据专项）。"""
    from tests.api.conftest import OTHER_PASSWORD, create_user

    owner_id = int(rpc_client.get("/api/v2/auth/me").json()["id"])
    _seed_task(settings, owner_id, name="owner-secret-file.bin")

    create_user(rpc_client, "intruder")
    login(rpc_client, "intruder", OTHER_PASSWORD)
    secret = rpc_client.put("/api/v2/credentials/rpc", json={"enabled": True}).json()["secret"]

    blob_parts = []
    for method, params in [
        ("aria2.tellActive", []),
        ("aria2.tellWaiting", [0, 50]),
        ("aria2.tellStopped", [0, 50]),
        ("aria2.getGlobalStat", []),
    ]:
        blob_parts.append(
            rpc_client.post(
                "/aria2/jsonrpc",
                json={
                    "jsonrpc": "2.0", "id": 1, "method": method,
                    "params": [f"token:{secret}", *params],
                },
            ).text
        )
    blob_parts.append(
        rpc_client.post(
            "/aria2/jsonrpc",
            json={
                "jsonrpc": "2.0", "id": 2, "method": "aria2.tellStatus",
                "params": [f"token:{secret}", "feedface0123456789abcdef"],
            },
        ).text
    )
    blob = "\n".join(blob_parts)
    assert "owner-secret-file" not in blob, "他人任务名不得出现"
    assert blob_parts[-1].find('"error"') != -1, "他人 GID 查询必须报错"


def test_rpc_full_scan_includes_getfiles_and_geturis(rpc_client: TestClient, settings) -> None:
    """P3 #15：getFiles/getUris 纳入 API 级全文扫描。"""
    user_id = int(rpc_client.get("/api/v2/auth/me").json()["id"])
    _seed_task(settings, user_id)
    listed = _rpc(rpc_client, "aria2.tellActive", [])
    gid = listed.json()["result"][0]["gid"]
    files = _rpc(rpc_client, "aria2.getFiles", [gid])
    uris = _rpc(rpc_client, "aria2.getUris", [gid])
    blob = files.text + uris.text
    assert "feedface" not in blob
    assert "capability" not in blob.lower() and "X-Mjiz" not in blob
    assert "objects/" not in blob


# --- 回归（D7）：RPC allow_rename 按 aria2 习惯解析字符串布尔并按本次调用生效 ---
def test_adduri_allow_rename_policy_string_booleans(rpc_client: TestClient, settings) -> None:
    user_id = int(rpc_client.get("/api/v2/auth/me").json()["id"])
    _seed_task(settings, user_id, name="occupied.bin")

    strict = _rpc(
        rpc_client,
        "aria2.addUri",
        [["https://example.com/rpc-strict.bin"], {"out": "occupied.bin", "allow_rename": "false"}],
    )
    body = strict.json()
    assert "error" in body, body
    assert "同名" in body["error"]["message"], body  # 修复前：错误原因是“下载选项包含未知字段”

    auto = _rpc(
        rpc_client,
        "aria2.addUri",
        [["https://example.com/rpc-auto.bin"], {"out": "occupied.bin", "allow_rename": "true"}],
    )
    assert "result" in auto.json(), auto.text

    async def allocated_name() -> str:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                return (
                    await uow.connection.execute(
                        text(
                            "SELECT display_name FROM task_subscriptions"
                            " WHERE display_name LIKE 'occupied%' ORDER BY id DESC LIMIT 1"
                        )
                    )
                ).scalar_one()
        finally:
            await database.close()

    assert asyncio.run(allocated_name()) == "occupied (2).bin"
