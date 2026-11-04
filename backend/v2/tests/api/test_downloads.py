from __future__ import annotations

import asyncio
import time
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update

from aria2deck.api.downloads import list_tasks as list_tasks_handler
from aria2deck.application.accounts.models import Principal
from aria2deck.application.downloads.models import (
    DownloadObservation,
    ObservedFile,
    TaskPage,
    TaskProjection,
)
from aria2deck.application.downloads.queries import DownloadQueryService
from aria2deck.domain.accounts import CredentialKind, User
from aria2deck.domain.downloads import AttemptStatus, SubscriptionStatus
from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.downloads import DownloadRepository
from aria2deck.infrastructure.sqlite.schema import (
    download_attempts,
    quota_accounts,
    task_subscriptions,
)
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork
from tests.api.conftest import OTHER_PASSWORD, create_admin, create_user, login


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def seed_task(
    settings: Settings,
    *,
    user_id: int,
    name: str,
    subscription_status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    attempt_status: AttemptStatus = AttemptStatus.ACTIVE,
    display_uri: str | None = "https://example.com/public-name",
    total_bytes: int | None = 1000,
    completed_bytes: int = 100,
    reserved_bytes: int = 0,
    attempt_id: int | None = None,
    created_at_ms: int = 1,
) -> tuple[int, int]:
    async def write() -> tuple[int, int]:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                repository = DownloadRepository(uow.connection)
                current_attempt_id = attempt_id
                if current_attempt_id is None:
                    gid = uuid.uuid4().hex[:16]
                    resource_key = f"http:{gid}"
                    source_id = await repository.create_source(
                        kind="http",
                        resource_key=resource_key,
                        payload_json='{"uri":"https://private.example/file?token=hidden"}',
                        options_json="{}",
                        selected_file_indexes_json=None,
                        display_name=name,
                        display_uri=display_uri,
                        now_ms=created_at_ms,
                    )
                    current_attempt_id = await repository.create_attempt(
                        source_id=source_id,
                        resource_key=resource_key,
                        gid=gid,
                        status=attempt_status,
                        total_length=total_bytes,
                        now_ms=created_at_ms,
                    )
                    await uow.connection.execute(
                        update(download_attempts)
                        .where(download_attempts.c.id == current_attempt_id)
                        .values(completed_length=completed_bytes)
                    )
                subscription_id = await repository.create_subscription(
                    attempt_id=current_attempt_id,
                    user_id=user_id,
                    status=subscription_status,
                    reserved_bytes=reserved_bytes,
                    display_name=name,
                    display_uri=display_uri,
                    now_ms=created_at_ms,
                )
                return current_attempt_id, subscription_id
        finally:
            await database.close()

    return asyncio.run(write())


def observe(
    app: FastAPI,
    settings: Settings,
    *,
    user_id: int,
    name: str,
    observed_at_ms: int | None = None,
    status: str = "active",
    download_speed: int = 120,
    upload_speed: int = 12,
) -> int:
    _, task_id = seed_task(settings, user_id=user_id, name=name)
    attempt = asyncio.run(_attempt_gid(settings, user_id=user_id, task_id=task_id))
    app.state.download_observations.put(
        DownloadObservation(
            gid=attempt,
            observed_at_ms=_now_ms() if observed_at_ms is None else observed_at_ms,
            status=status,
            download_speed=download_speed,
            upload_speed=upload_speed,
            peer_count=3,
            connection_count=2,
            files=(
                ObservedFile(
                    name="public-file.iso",
                    total_bytes=1000,
                    completed_bytes=100,
                    selected=True,
                ),
            ),
        )
    )
    return task_id


async def _attempt_gid(settings: Settings, *, user_id: int, task_id: int) -> str:
    database = SQLiteDatabase(settings.database_path)
    try:
        async with SQLiteUnitOfWork(database) as uow:
            record = await DownloadRepository(uow.connection).get_task(
                user_id=user_id, task_id=task_id
            )
            return record.gid
    finally:
        await database.close()


def authenticated_admin(client: TestClient) -> dict[str, object]:
    admin = create_admin(client)
    login(client)
    return admin


def test_task_list_is_user_scoped_paginated_searchable_and_filterable(
    app: FastAPI, client: TestClient, settings: Settings
) -> None:
    admin = authenticated_admin(client)
    other = create_user(client)
    admin_id = int(admin["id"])
    other_id = int(other["id"])
    alpha_id = observe(app, settings, user_id=admin_id, name="Alpha ISO")
    seed_task(
        settings,
        user_id=admin_id,
        name="Paused backup",
        display_uri="https://example.com/ARCHIVE",
        subscription_status=SubscriptionStatus.PAUSED,
        attempt_status=AttemptStatus.PAUSED,
        created_at_ms=2,
    )
    seed_task(settings, user_id=other_id, name="Other user secret", created_at_ms=3)

    first = client.get("/api/v2/tasks", params={"page": 1, "page_size": 1})
    assert first.status_code == 200
    assert first.json()["total"] == 2
    assert first.json()["page"] == 1
    assert first.json()["page_size"] == 1
    assert len(first.json()["items"]) == 1

    beyond = client.get("/api/v2/tasks", params={"page": 999, "page_size": 100})
    assert beyond.status_code == 200
    assert beyond.json() == {"items": [], "total": 2, "page": 999, "page_size": 100}

    by_name = client.get("/api/v2/tasks", params={"q": "  alpha  "})
    assert [item["id"] for item in by_name.json()["items"]] == [alpha_id]
    by_uri = client.get("/api/v2/tasks", params={"q": "archive"})
    assert [item["name"] for item in by_uri.json()["items"]] == ["Paused backup"]
    assert client.get("/api/v2/tasks", params={"q": "missing"}).json()["total"] == 0

    paused = client.get("/api/v2/tasks", params={"status": "paused"})
    assert paused.json()["total"] == 1
    assert paused.json()["items"][0]["status_label"] == "已暂停"
    assert client.get("/api/v2/tasks").text.find("Other user secret") == -1


def test_projection_uses_database_state_and_only_fresh_observation_details(
    app: FastAPI, client: TestClient, settings: Settings
) -> None:
    admin = authenticated_admin(client)
    user_id = int(admin["id"])
    task_id = observe(
        app,
        settings,
        user_id=user_id,
        name="Finishing",
        status="complete",
        download_speed=500,
        upload_speed=50,
    )

    response = client.get(f"/api/v2/tasks/{task_id}")

    assert response.status_code == 200
    task = response.json()
    assert task["status"] == "active"
    assert task["status_label"] == "入库中"
    assert task["total_bytes"] == 1000
    assert task["completed_bytes"] == 100
    assert task["download_speed"] == 500
    assert task["upload_speed"] == 50
    assert task["peer_count"] == 3
    assert task["connection_count"] == 2
    assert task["observation_fresh"] is True
    assert task["files"] == [
        {
            "name": "public-file.iso",
            "total_bytes": 1000,
            "completed_bytes": 100,
            "selected": True,
        }
    ]
    assert task["retryable"] is False
    assert "gid" not in task
    assert "attempt_id" not in task
    assert "private.example" not in response.text


@pytest.mark.parametrize("observed_at", [None, 0])
def test_missing_or_expired_observation_never_changes_authoritative_task(
    app: FastAPI,
    client: TestClient,
    settings: Settings,
    observed_at: int | None,
) -> None:
    admin = authenticated_admin(client)
    user_id = int(admin["id"])
    if observed_at is None:
        _, task_id = seed_task(settings, user_id=user_id, name="Offline")
    else:
        task_id = observe(
            app,
            settings,
            user_id=user_id,
            name="Stale",
            observed_at_ms=observed_at,
            status="complete",
        )

    task = client.get(f"/api/v2/tasks/{task_id}").json()
    assert task["status"] == "active"
    assert task["status_label"] == "下载中"
    assert task["download_speed"] == 0
    assert task["upload_speed"] == 0
    assert task["peer_count"] == 0
    assert task["connection_count"] == 0
    assert task["files"] == []
    assert task["observation_fresh"] is False


def test_future_observation_is_not_fresh_and_paused_tasks_have_zero_speed(
    app: FastAPI, client: TestClient, settings: Settings
) -> None:
    admin = authenticated_admin(client)
    user_id = int(admin["id"])
    future_id = observe(
        app,
        settings,
        user_id=user_id,
        name="Future",
        observed_at_ms=_now_ms() + 60_000,
    )
    paused_id, _ = seed_task(
        settings,
        user_id=user_id,
        name="Paused",
        subscription_status=SubscriptionStatus.PAUSED,
        attempt_status=AttemptStatus.PAUSED,
    )
    paused_gid = asyncio.run(_attempt_gid(settings, user_id=user_id, task_id=paused_id))
    app.state.download_observations.put(
        DownloadObservation(
            gid=paused_gid,
            observed_at_ms=_now_ms(),
            status="paused",
            download_speed=900,
            upload_speed=90,
            peer_count=1,
            connection_count=1,
            files=(),
        )
    )

    future = client.get(f"/api/v2/tasks/{future_id}").json()
    paused = client.get(f"/api/v2/tasks/{paused_id}").json()
    assert future["observation_fresh"] is False
    assert future["download_speed"] == 0
    assert paused["observation_fresh"] is True
    assert paused["download_speed"] == 0
    assert paused["upload_speed"] == 0
    assert paused["peer_count"] == 1


def test_terminal_user_state_wins_without_mutating_shared_attempt(
    client: TestClient, settings: Settings
) -> None:
    admin = authenticated_admin(client)
    other = create_user(client)
    admin_id = int(admin["id"])
    other_id = int(other["id"])
    attempt_id, admin_task = seed_task(settings, user_id=admin_id, name="Shared")
    _, other_task = seed_task(
        settings,
        user_id=other_id,
        name="Shared by other",
        subscription_status=SubscriptionStatus.CANCELLED,
        attempt_id=attempt_id,
    )

    assert client.get(f"/api/v2/tasks/{other_task}").status_code == 404
    login(client, "user", OTHER_PASSWORD)
    cancelled = client.get(f"/api/v2/tasks/{other_task}").json()
    assert cancelled["status"] == "cancelled"
    assert cancelled["status_label"] == "已取消"
    assert cancelled["retryable"] is True

    login(client)
    active = client.get(f"/api/v2/tasks/{admin_task}").json()
    assert active["status"] == "active"
    assert active["status_label"] == "下载中"


def test_attempt_terminal_state_is_projected_for_a_live_subscription(
    client: TestClient, settings: Settings
) -> None:
    admin = authenticated_admin(client)
    _, task_id = seed_task(
        settings,
        user_id=int(admin["id"]),
        name="Failed attempt",
        subscription_status=SubscriptionStatus.ACTIVE,
        attempt_status=AttemptStatus.FAILED,
    )

    task = client.get(f"/api/v2/tasks/{task_id}").json()
    assert task["status"] == "failed"
    assert task["status_label"] == "已失败"
    assert task["retryable"] is True
    filtered = client.get("/api/v2/tasks", params={"status": "failed"}).json()
    assert [item["id"] for item in filtered["items"]] == [task_id]


def test_stats_share_projection_and_expose_quota(
    client: TestClient, settings: Settings
) -> None:
    admin = authenticated_admin(client)
    other = create_user(client)
    user_id = int(admin["id"])
    observe(
        app=client.app,
        settings=settings,
        user_id=int(other["id"]),
        name="Other user",
        download_speed=999,
        upload_speed=99,
    )
    observe(app=client.app, settings=settings, user_id=user_id, name="Active", download_speed=100, upload_speed=10)
    observe(app=client.app, settings=settings, user_id=user_id, name="Waiting", download_speed=900, upload_speed=90)
    waiting_id = client.get("/api/v2/tasks", params={"q": "Waiting"}).json()["items"][0]["id"]
    asyncio.run(_set_waiting(settings, user_id=user_id, task_id=waiting_id))
    seed_task(
        settings,
        user_id=user_id,
        name="Done",
        subscription_status=SubscriptionStatus.COMPLETED,
        attempt_status=AttemptStatus.COMPLETED,
    )

    async def set_quota() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                await uow.connection.execute(
                    update(quota_accounts)
                    .where(quota_accounts.c.user_id == user_id)
                    .values(used_bytes=300, reserved_bytes=150)
                )
        finally:
            await database.close()

    asyncio.run(set_quota())
    stats = client.get("/api/v2/tasks/stats").json()
    assert stats["download_speed"] == 100
    assert stats["upload_speed"] == 10
    assert stats["counts"]["active"] == 1
    assert stats["counts"]["waiting"] == 1
    assert stats["counts"]["completed"] == 1
    assert stats["counts"]["failed"] == 0
    assert stats["total"] == 3
    assert stats["quota"]["used_bytes"] == 300
    assert stats["quota"]["reserved_bytes"] == 150
    assert stats["quota"]["available_bytes"] == stats["quota"]["quota_bytes"] - 450


async def _set_waiting(settings: Settings, *, user_id: int, task_id: int) -> None:
    database = SQLiteDatabase(settings.database_path)
    try:
        async with SQLiteUnitOfWork(database) as uow:
            record = await DownloadRepository(uow.connection).get_task(user_id=user_id, task_id=task_id)
            assert record.subscription_status is SubscriptionStatus.ACTIVE
            await uow.connection.execute(
                update(task_subscriptions)
                .where(task_subscriptions.c.id == task_id)
                .values(status=SubscriptionStatus.WAITING)
            )
    finally:
        await database.close()


@pytest.mark.parametrize(
    "path,params",
    [
        ("/api/v2/tasks", {"status": "unknown"}),
        ("/api/v2/tasks", {"page": 0}),
        ("/api/v2/tasks", {"page_size": 0}),
        ("/api/v2/tasks", {"page_size": 101}),
        ("/api/v2/tasks", {"q": "x" * 201}),
        ("/api/v2/tasks/0", {}),
        ("/api/v2/tasks/not-an-id", {}),
    ],
)
def test_invalid_task_read_parameters_have_stable_validation_errors(
    client: TestClient, path: str, params: dict[str, object]
) -> None:
    authenticated_admin(client)
    response = client.get(path, params=params)
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "invalid_input", "message": "请求参数无效"}
    }


def test_task_reads_require_authentication_and_hide_missing_tasks(client: TestClient) -> None:
    assert client.get("/api/v2/tasks").status_code == 401
    assert client.get("/api/v2/tasks/stats").status_code == 401
    authenticated_admin(client)
    missing = client.get("/api/v2/tasks/999")
    assert missing.status_code == 404
    assert missing.json() == {"error": {"code": "not_found", "message": "任务不存在"}}


def test_download_read_openapi_matches_response_and_query_contract(app: FastAPI) -> None:
    schema = app.openapi()
    paths = schema["paths"]
    for path in ("/api/v2/tasks", "/api/v2/tasks/stats", "/api/v2/tasks/{task_id}"):
        assert paths[path]["get"]["security"] == [{"ApiToken": []}, {"SessionCookie": []}]

    list_operation = paths["/api/v2/tasks"]["get"]
    assert set(list_operation["responses"]) == {"200", "401", "422", "429", "500"}
    assert list_operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/TaskListResponse"
    )
    parameters = {parameter["name"]: parameter for parameter in list_operation["parameters"]}
    assert parameters["page"]["schema"]["minimum"] == 1
    assert parameters["page_size"]["schema"]["maximum"] == 100
    assert parameters["q"]["schema"]["maxLength"] == 200
    status_ref = parameters["status"]["schema"]["anyOf"][0]["$ref"]
    status_schema = schema["components"]["schemas"][status_ref.rsplit("/", 1)[-1]]
    assert set(status_schema["enum"]) == {
        "queued",
        "active",
        "waiting",
        "paused",
        "completed",
        "failed",
        "cancelled",
        "expired",
    }
    detail_operation = paths["/api/v2/tasks/{task_id}"]["get"]
    assert detail_operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/TaskResponse"
    )
    assert "404" in detail_operation["responses"]
    stats_operation = paths["/api/v2/tasks/stats"]["get"]
    assert stats_operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/TaskStatsResponse"
    )


async def test_list_api_adapter_maps_query_arguments_and_typed_response() -> None:
    projection = TaskProjection(
        id=9,
        status=SubscriptionStatus.ACTIVE,
        status_label="下载中",
        name="Alpha",
        display_uri="https://example.com/alpha",
        total_bytes=100,
        completed_bytes=25,
        download_speed=10,
        upload_speed=1,
        peer_count=0,
        connection_count=1,
        files=(),
        observation_fresh=True,
        reserved_bytes=75,
        error_code=None,
        retryable=False,
        created_at_ms=1,
        updated_at_ms=2,
        finished_at_ms=None,
    )
    service = AsyncMock(spec=DownloadQueryService)
    service.list_tasks.return_value = TaskPage(
        items=[projection], total=20, page=3, page_size=1
    )
    principal = Principal(
        User(
            id=7,
            username="user",
            username_key="user",
            password_hash="ab" * 32,
            is_admin=False,
            quota_bytes=1000,
            is_initial_password=False,
            pending_delete=False,
            credential_generation=1,
            created_at_ms=1,
            updated_at_ms=1,
        ),
        CredentialKind.SESSION,
    )

    response = await list_tasks_handler(
        page=3,
        page_size=1,
        status=SubscriptionStatus.ACTIVE,
        q="alpha",
        principal=principal,
        service=service,
    )

    service.list_tasks.assert_awaited_once_with(
        user_id=7,
        status=SubscriptionStatus.ACTIVE,
        search="alpha",
        page=3,
        page_size=1,
    )
    assert response.total == 20
    assert response.page == 3
    assert response.items[0].id == 9
    assert response.items[0].uri == "https://example.com/alpha"
    assert response.items[0].completed_bytes == 25
