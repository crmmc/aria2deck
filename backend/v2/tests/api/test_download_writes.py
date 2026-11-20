from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from aria2deck.application.downloads.models import BackendSubmissionResult
from aria2deck.domain.downloads import AttemptStatus
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository
from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository
from aria2deck.infrastructure.sqlite.schema import (
    download_attempts,
    download_sources,
    user_files,
)
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork
from tests.api.conftest import OTHER_PASSWORD, create_admin, create_user, login
from tests.download_fakes import FakeSubmissionBackend, make_torrent


def install_backend(client: TestClient) -> FakeSubmissionBackend:
    backend = FakeSubmissionBackend()
    client.app.state.download_commands.backend = backend
    client.app.state.download_recovery.backend = backend
    return backend


def test_batch_create_deduplicates_http_and_magnet_but_rejects_ftp_per_item(
    client: TestClient, settings: Settings
) -> None:
    create_admin(client)
    login(client)
    backend = install_backend(client)

    response = client.post(
        "/api/v2/tasks",
        json={
            "tasks": [
                {"uri": " https://example.com/file.iso?token=private "},
                {"uri": "https://example.com/file.iso?token=private"},
                {"uri": "magnet:?xt=urn:btih:" + "ab" * 20},
                {"uri": "ftp://example.com/not-supported"},
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["accepted_count"], body["rejected_count"]) == (3, 1)
    assert [item["outcome"] for item in body["items"]] == [
        "created",
        "duplicate",
        "created",
        None,
    ]
    assert body["items"][0]["task"]["id"] == body["items"][1]["task"]["id"]
    assert body["items"][0]["task"]["uri"] == "https://example.com/file.iso"
    assert "private" not in response.text
    assert body["items"][3]["error"]["code"] == "invalid_input"
    assert len(backend.submit_calls) == 1
    assert len(backend.submit_calls[0]) == 2
    assert len(backend.confirm_calls) == 2

    async def check() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                states = (
                    await uow.connection.execute(select(download_attempts.c.submission_state))
                ).scalars().all()
                assert states == ["submitted", "submitted"]
        finally:
            await database.close()

    asyncio.run(check())


def test_http_identity_separates_credentials_and_mirrors(client: TestClient) -> None:
    create_admin(client)
    login(client)
    backend = install_backend(client)
    response = client.post(
        "/api/v2/tasks",
        json={
            "tasks": [
                {"uri": "https://example.com/a", "options": {"headers": {"X-Auth": "one"}}},
                {"uri": "https://example.com/a", "options": {"headers": {"X-Auth": "two"}}},
                {"uri": "https://example.com/a", "options": {"mirrors": ["https://mirror.example/a"]}},
            ]
        },
    )
    assert response.status_code == 200
    assert [item["outcome"] for item in response.json()["items"]] == ["created"] * 3
    assert len(backend.submit_calls[0]) == 3


def test_aria2_definite_rejection_is_partial_not_whole_batch_failure(client: TestClient) -> None:
    create_admin(client)
    login(client)
    backend = install_backend(client)
    backend.reject_indexes = {1}

    response = client.post(
        "/api/v2/tasks",
        json={"tasks": [{"uri": "https://example.com/a"}, {"uri": "https://example.com/b"}]},
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["accepted_count"], body["rejected_count"]) == (1, 1)
    assert body["items"][1]["task"]["status"] == "failed"
    assert body["items"][1]["error"] == {
        "code": "aria2_1",
        "message": "aria2 拒绝了下载提交",
    }


@pytest.mark.parametrize("failure", ["submit", "confirm"])
def test_uncertain_submission_returns_502_and_keeps_recovery_state(
    client: TestClient, settings: Settings, failure: str
) -> None:
    create_admin(client)
    login(client)
    backend = install_backend(client)
    if failure == "submit":
        backend.submit_error = DomainError(ErrorCode.SUBMISSION_UNCERTAIN, "network detail")
    else:
        backend.confirm_default = False

    response = client.post(
        "/api/v2/tasks", json={"tasks": [{"uri": "https://example.com/a"}]}
    )

    assert response.status_code == 502
    assert response.json() == {
        "error": {
            "code": "submission_uncertain",
            "message": "下载结果暂无法确认，将在恢复时核对",
        }
    }
    assert "network detail" not in response.text

    async def check() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                attempt = (
                    await uow.connection.execute(select(download_attempts))
                ).one()
                assert attempt.status == AttemptStatus.QUEUED
                assert attempt.submission_state == "uncertain"
        finally:
            await database.close()

    asyncio.run(check())


def test_torrent_preview_selection_create_and_definite_rejection_release_quota(
    client: TestClient, settings: Settings
) -> None:
    admin = create_admin(client)
    login(client)
    backend = install_backend(client)
    payload = make_torrent([("a.bin", 3), ("folder/b.bin", 5)])

    preview = client.post("/api/v2/tasks/torrent/preview", json={"torrent": payload})
    assert preview.status_code == 200
    assert preview.json()["total_size"] == 8
    assert preview.json()["default_selection"] == [1, 2]

    created = client.post(
        "/api/v2/tasks/torrent",
        json={"torrent": payload, "selected_file_indexes": [2]},
    )
    assert created.status_code == 200
    result = created.json()
    assert result["accepted"] is True
    assert result["task"]["reserved_bytes"] == 5
    assert result["task"]["total_bytes"] == 5
    assert backend.submit_calls[0][0].source.selected_file_indexes == (2,)

    failed_backend = FakeSubmissionBackend()
    failed_backend.replies = [BackendSubmissionResult(False, error_code="aria2_2")]
    client.app.state.download_commands.backend = failed_backend
    rejected = client.post(
        "/api/v2/tasks/torrent",
        json={"torrent": make_torrent([("other.bin", 4)], name="other")},
    )
    assert rejected.json()["accepted"] is False

    async def quota() -> int:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                return (await QuotaRepository(uow.connection).get(int(admin["id"]))).reserved_bytes
        finally:
            await database.close()

    assert asyncio.run(quota()) == 5


def test_multi_user_join_and_cancel_only_stops_after_last_subscription(
    client: TestClient,
) -> None:
    create_admin(client)
    login(client)
    create_user(client)
    backend = install_backend(client)
    first = client.post(
        "/api/v2/tasks", json={"tasks": [{"uri": "https://example.com/shared"}]}
    ).json()["items"][0]
    first_id = first["task"]["id"]

    login(client, "user", OTHER_PASSWORD)
    joined = client.post(
        "/api/v2/tasks", json={"tasks": [{"uri": "https://example.com/shared"}]}
    ).json()["items"][0]
    assert joined["outcome"] == "joined_live"
    assert joined["task"]["id"] != first_id
    cancelled_other = client.post("/api/v2/tasks/cancel", json={"task_ids": [joined["task"]["id"]]})
    assert cancelled_other.json()["items"][0]["accepted"] is True
    assert backend.stop_calls == []

    login(client)
    cancelled_admin = client.post("/api/v2/tasks/cancel", json={"task_ids": [first_id]})
    assert cancelled_admin.json()["items"][0]["stop_confirmed"] is True
    assert len(backend.stop_calls) == 1
    repeated = client.post("/api/v2/tasks/cancel", json={"task_ids": [first_id]})
    assert repeated.json()["items"][0]["accepted"] is False

    retry = client.post(f"/api/v2/tasks/{first_id}/retry")
    assert retry.status_code == 200
    assert retry.json()["outcome"] == "created"
    assert retry.json()["task"]["id"] != first_id


def test_cancel_stop_failure_is_reported_unconfirmed_and_recoverable(client: TestClient) -> None:
    create_admin(client)
    login(client)
    backend = install_backend(client)
    task_id = client.post(
        "/api/v2/tasks", json={"tasks": [{"uri": "https://example.com/a"}]}
    ).json()["items"][0]["task"]["id"]
    backend.stop_error = DomainError(ErrorCode.SUBMISSION_UNCERTAIN, "offline")

    result = client.post("/api/v2/tasks/cancel", json={"task_ids": [task_id]}).json()["items"][0]
    assert result["accepted"] is True
    assert result["stop_confirmed"] is False
    assert result["task"]["status"] == "cancelled"


def test_completed_content_attaches_to_another_user_and_same_user_is_idempotent(
    client: TestClient, settings: Settings
) -> None:
    admin = create_admin(client)
    login(client)
    other = create_user(client)
    install_backend(client)
    created = client.post(
        "/api/v2/tasks", json={"tasks": [{"uri": "https://example.com/file.iso"}]}
    ).json()["items"][0]
    attempt_id = created["task"]["id"]

    async def complete() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                downloads = DownloadRepository(uow.connection)
                record = await downloads.get_task(user_id=int(admin["id"]), task_id=attempt_id)
                # 当前时间基准：1970 时间戳会被历史保留 worker 立即软过期，与断言竞态（已知坑）。
                now_ms = time.time_ns() // 1_000_000
                content_id = await ContentRepository(uow.connection).create_object(
                    kind="file", digest="ab" * 32, size=10, storage_path="objects/file", now_ms=now_ms
                )
                await downloads.complete_attempt(record.attempt_id, content_id=content_id, now_ms=now_ms)
        finally:
            await database.close()

    asyncio.run(complete())
    login(client, "user", OTHER_PASSWORD)
    attached = client.post(
        "/api/v2/tasks", json={"tasks": [{"uri": "https://example.com/file.iso"}]}
    ).json()["items"][0]
    assert attached["outcome"] == "attached_completed"
    assert attached["task"]["status"] == "completed"

    login(client)
    duplicate = client.post(
        "/api/v2/tasks", json={"tasks": [{"uri": "https://example.com/file.iso"}]}
    ).json()["items"][0]
    assert duplicate["outcome"] == "duplicate"
    assert duplicate["task"]["id"] == attempt_id

    async def check() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                assert (
                    await QuotaRepository(uow.connection).get(int(other["id"]))
                ).used_bytes == 10
                assert (
                    await uow.connection.execute(
                        select(user_files).where(user_files.c.user_id == int(other["id"]))
                    )
                ).one()
        finally:
            await database.close()

    asyncio.run(check())


def test_concurrent_torrent_admission_never_exceeds_quota(app: FastAPI) -> None:
    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            backend = FakeSubmissionBackend()
            app.state.download_commands.backend = backend
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                created = await client.post(
                    "/api/v2/users",
                    json={"username": "admin", "password": "ab" * 32, "is_admin": True, "quota_bytes": 4},
                )
                user_id = created.json()["id"]
                await client.post(
                    "/api/v2/auth/login", json={"username": "admin", "password": "ab" * 32}
                )
                responses = await asyncio.gather(
                    client.post(
                        "/api/v2/tasks/torrent",
                        json={"torrent": make_torrent([("a", 3)], name="a")},
                    ),
                    client.post(
                        "/api/v2/tasks/torrent",
                        json={"torrent": make_torrent([("b", 3)], name="b")},
                    ),
                )
                assert sorted(response.status_code for response in responses) == [200, 409]
                async with app.state.download_commands.uow_factory() as uow:
                    quota = await QuotaRepository(uow.connection).get(user_id)
                    assert quota.reserved_bytes == 3

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "payload",
    [
        {"tasks": []},
        {"tasks": [{"uri": "https://example.com", "extra": True}]},
        {"tasks": [{"uri": "https://example.com", "options": {"unknown": 1}}]},
    ],
)
def test_invalid_write_requests_use_uniform_validation(client: TestClient, payload: object) -> None:
    create_admin(client)
    login(client)
    response = client.post("/api/v2/tasks", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"


def test_write_openapi_includes_partial_and_whole_failure_contracts(app: FastAPI) -> None:
    paths = app.openapi()["paths"]
    assert paths["/api/v2/tasks"]["post"]["responses"]["502"]["content"]["application/json"]["schema"]["$ref"].endswith("ErrorResponse")
    assert paths["/api/v2/tasks/torrent/preview"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("TorrentPreviewResponse")
    assert paths["/api/v2/tasks/cancel"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("TaskBatchResponse")
    assert "/api/v2/internal/fetch/{attempt_id}/{source_index}" not in paths


def test_create_task_accepts_new_option_whitelist_without_echoing_credentials(
    client: TestClient, settings: Settings
) -> None:
    """A9：REST 侧接受 aria2 键名选项；凭据不回显到任何响应。"""
    create_admin(client)
    login(client)
    backend = install_backend(client)

    response = client.post(
        "/api/v2/tasks",
        json={
            "tasks": [
                {
                    "uri": "https://example.com/authed.iso",
                    "options": {
                        "out": "renamed.iso",
                        "http-user": "alice",
                        "http-passwd": "s3cret-passwd",
                        "max-connection-per-server": 4,
                    },
                },
                # 路径形 out 拒绝（防逃逸），逐项失败不阻断其余。
                {
                    "uri": "https://example.com/escape.iso",
                    "options": {"out": "../escape.iso"},
                },
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["accepted_count"], body["rejected_count"]) == (1, 1)
    rejected = body["items"][1]
    assert rejected["accepted"] is False
    assert rejected["error"]["code"] == "invalid_input"
    assert "out" in rejected["error"]["message"]
    accepted_task = body["items"][0]["task"]
    assert accepted_task["name"] == "renamed.iso"
    # 凭据零回显：全文不得出现用户名/密码字面量。
    assert "alice" not in response.text
    assert "s3cret-passwd" not in response.text

    detail = client.get(f"/api/v2/tasks/{accepted_task['id']}")
    assert "alice" not in detail.text and "s3cret-passwd" not in detail.text

    # aria2 提交载荷不含凭据（HTTP 走内部网关，凭据只留在来源数据里）。
    assert backend.submit_calls
    submission = backend.submit_calls[-1][-1]
    call = submission  # Submission 数据类
    assert call.source.options["http-user"] == "alice"  # 来源侧保留用于网关


# --- 回归（D1/D7）：批内同名分配用户级名称，共享 source 不保存最终名与策略 ---
def test_same_name_batch_allocates_user_names_without_polluting_sources(
    client: TestClient, settings: Settings
) -> None:
    import json as json_module

    create_admin(client)
    login(client)
    backend = install_backend(client)

    response = client.post(
        "/api/v2/tasks",
        json={
            "tasks": [
                {"uri": "https://example.com/one.bin", "options": {"out": "same.bin"}},
                {
                    "uri": "https://example.com/two.bin",
                    "options": {"out": "same.bin", "allow_rename": True},
                },
            ]
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["accepted_count"], body["rejected_count"]) == (2, 0)
    assert [item["task"]["name"] for item in body["items"]] == ["same.bin", "same (2).bin"]
    assert len(backend.submit_calls[0]) == 2

    async def check_sources() -> list[dict[str, object]]:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                rows = (
                    await uow.connection.execute(select(download_sources.c.options_json))
                ).scalars().all()
        finally:
            await database.close()
        return [json_module.loads(row) for row in rows]

    options_rows = asyncio.run(check_sources())
    assert len(options_rows) == 2
    for options in options_rows:
        assert options["out"] == "same.bin"  # source 保留资源派生名/请求名
        assert "allow_rename" not in options  # 策略不进入共享 source（D1）


# --- 回归（D7）：REST 严格冲突逐项 duplicate_resource，且不提交 aria2 ---
def test_strict_allow_rename_false_rejects_occupied_name_per_item(client: TestClient) -> None:
    create_admin(client)
    login(client)
    backend = install_backend(client)

    first = client.post(
        "/api/v2/tasks",
        json={"tasks": [{"uri": "https://example.com/a.bin", "options": {"out": "busy.bin"}}]},
    )
    assert first.status_code == 200

    strict = client.post(
        "/api/v2/tasks",
        json={
            "tasks": [
                {
                    "uri": "https://example.com/b.bin",
                    "options": {"out": "busy.bin", "allow_rename": False},
                }
            ]
        },
    )
    assert strict.status_code == 200
    item = strict.json()["items"][0]
    assert item["accepted"] is False
    assert item["error"]["code"] == "duplicate_resource"
    assert len(backend.submit_calls) == 1  # 严格冲突不提交 aria2

    auto = client.post(
        "/api/v2/tasks",
        json={"tasks": [{"uri": "https://example.com/b.bin", "options": {"out": "busy.bin"}}]},
    )
    auto_item = auto.json()["items"][0]
    assert auto_item["accepted"] is True
    assert auto_item["task"]["name"] == "busy (2).bin"


# --- 回归（D7）：单任务重试携带本次 allow_rename，严格冲突 409 ---
def test_retry_carries_allow_rename_policy_with_strict_409(
    client: TestClient, settings: Settings
) -> None:
    admin = create_admin(client)
    login(client)
    install_backend(client)
    created = client.post(
        "/api/v2/tasks",
        json={"tasks": [{"uri": "https://example.com/retry.bin", "options": {"out": "busy.bin"}}]},
    ).json()["items"][0]
    task_id = created["task"]["id"]
    cancelled = client.post("/api/v2/tasks/cancel", json={"task_ids": [task_id]})
    assert cancelled.json()["items"][0]["accepted"] is True

    async def seed_user_file() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                content = ContentRepository(uow.connection)
                content_id = await content.create_object(
                    kind="file", digest="cd" * 32, size=7, storage_path="objects/retry-keep", now_ms=1
                )
                await content.add_user_file(
                    user_id=int(admin["id"]),
                    content_id=content_id,
                    display_name="busy.bin",
                    now_ms=1,
                )
        finally:
            await database.close()

    asyncio.run(seed_user_file())

    strict = client.post(f"/api/v2/tasks/{task_id}/retry", json={"allow_rename": False})
    assert strict.status_code == 409
    assert strict.json()["error"]["code"] == "duplicate_resource"

    auto = client.post(f"/api/v2/tasks/{task_id}/retry")
    assert auto.status_code == 200
    assert auto.json()["task"]["name"] == "busy (2).bin"


# --- 回归（D7）：Torrent 顶层 allow_rename 严格冲突 409 且不提交 aria2 ---
def test_torrent_strict_conflict_returns_409_without_submission(client: TestClient) -> None:
    create_admin(client)
    login(client)
    backend = install_backend(client)
    occupied = client.post(
        "/api/v2/tasks",
        json={"tasks": [{"uri": "https://example.com/x.bin", "options": {"out": "busy.bin"}}]},
    )
    assert occupied.status_code == 200

    strict = client.post(
        "/api/v2/tasks/torrent",
        json={"torrent": make_torrent([("busy.bin", 3)], name="busy.bin"), "allow_rename": False},
    )
    assert strict.status_code == 409
    assert strict.json()["error"]["code"] == "duplicate_resource"
    assert len(backend.submit_calls) == 1  # 只有占用名的 HTTP 任务提交过，种子未提交

    auto = client.post(
        "/api/v2/tasks/torrent",
        json={"torrent": make_torrent([("busy.bin", 3)], name="busy.bin"), "allow_rename": True},
    )
    assert auto.status_code == 200
    assert auto.json()["task"]["name"] == "busy (2).bin"


def test_full_quota_user_is_blocked_by_known_size_and_keeps_read_access(
    app: FastAPI,
) -> None:
    """AC7：用量已达上限的用户无法新建任务，已存在文件仍可浏览。

    已知大小的任务（种子）在建任务时按声明大小预留 → 立刻被 quota_exceeded 拒绝；
    未知大小的任务（HTTP URI）可以入队，但在观测到大小后由准入规则判失败，
    不会真的占用超出配额的磁盘。
    """
    from sqlalchemy import update

    from aria2deck.infrastructure.sqlite.schema import quota_accounts

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            backend = FakeSubmissionBackend()
            app.state.download_commands.backend = backend
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                created = await client.post(
                    "/api/v2/users",
                    json={
                        "username": "admin",
                        "password": "ab" * 32,
                        "is_admin": True,
                        "quota_bytes": 10,
                    },
                )
                user_id = int(created.json()["id"])
                await client.post(
                    "/api/v2/auth/login", json={"username": "admin", "password": "ab" * 32}
                )
                async with app.state.download_commands.uow_factory() as uow:
                    await uow.connection.execute(
                        update(quota_accounts)
                        .where(quota_accounts.c.user_id == user_id)
                        .values(used_bytes=10)
                    )
                # 已知大小：创建阶段即被拒
                torrent = await client.post(
                    "/api/v2/tasks/torrent",
                    json={"torrent": make_torrent([("a.iso", 3)], name="a")},
                )
                assert torrent.status_code == 409
                assert torrent.json()["error"]["code"] == "quota_exceeded"
                assert torrent.json()["error"]["message"] == "用户配额不足"

                # 已存在文件与统计仍可读取（读路径不受配额影响）
                assert (await client.get("/api/v2/files")).status_code == 200
                stats = await client.get("/api/v2/tasks/stats")
                assert stats.status_code == 200
                assert stats.json()["quota"]["available_bytes"] == 0

    asyncio.run(exercise())
