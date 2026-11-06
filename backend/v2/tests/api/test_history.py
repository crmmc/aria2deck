from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from aria2deck.domain.downloads import AttemptStatus, SubscriptionStatus
from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.schema import download_sources, task_subscriptions
from tests.api.conftest import OTHER_PASSWORD, create_admin, create_user, login
from tests.api.test_download_writes import install_backend
from tests.api.test_downloads import _now_ms, seed_task


def test_history_filters_paginates_and_deletes_only_terminal_tasks(
    client: TestClient, settings: Settings
) -> None:
    admin = create_admin(client)
    login(client)
    install_backend(client)
    user_id = int(admin["id"])
    base = _now_ms()
    _, completed = seed_task(settings, user_id=user_id, name="Alpha complete", subscription_status=SubscriptionStatus.COMPLETED, attempt_status=AttemptStatus.COMPLETED, created_at_ms=base)
    _, failed = seed_task(settings, user_id=user_id, name="Beta failed", subscription_status=SubscriptionStatus.FAILED, attempt_status=AttemptStatus.FAILED, created_at_ms=base + 1)
    _, active = seed_task(settings, user_id=user_id, name="Live", created_at_ms=base + 2)

    response = client.get("/api/v2/history", params={"page_size": 1})
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert response.json()["items"][0]["id"] == failed
    second = client.get("/api/v2/history", params={"page": 2, "page_size": 1})
    assert second.json()["items"][0]["id"] == completed
    assert client.get("/api/v2/history", params={"status": "failed"}).json()["total"] == 1
    assert client.get("/api/v2/history", params={"q": " alpha "}).json()["total"] == 1
    assert client.get("/api/v2/history", params={"status": "active"}).status_code == 422

    deleted = client.request("DELETE", "/api/v2/history", json={"task_ids": [completed, active, 999]})
    assert deleted.status_code == 200
    assert [item["accepted"] for item in deleted.json()["items"]] == [True, False, False]
    assert client.get("/api/v2/history").json()["total"] == 1
    assert client.get("/api/v2/tasks", params={"status": "active"}).json()["total"] == 1
    retry = client.post(f"/api/v2/tasks/{failed}/retry")
    assert retry.status_code == 200, retry.text


def test_retention_purge_expires_shell_detaches_payload_and_blocks_retry(
    client: TestClient, settings: Settings
) -> None:
    admin = create_admin(client)
    login(client)
    base = _now_ms()
    _, failed = seed_task(settings, user_id=int(admin["id"]), name="Old failure", subscription_status=SubscriptionStatus.FAILED, attempt_status=AttemptStatus.FAILED, created_at_ms=base)

    purged = client.post("/api/v2/history/purge", json={"before_ms": base})
    assert purged.status_code == 200
    assert purged.json()["expired_tasks"] == 1
    assert purged.json()["detached_sources"] == 1
    history = client.get("/api/v2/history", params={"status": "expired"}).json()
    assert history["items"][0]["status"] == "expired"
    assert client.post(f"/api/v2/tasks/{failed}/retry").status_code == 409

    async def check() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with database.connection() as connection:
                source = (await connection.execute(select(download_sources))).one()
                assert source.payload_json == "{}"
                assert source.display_uri is None
                assert source.detached_at_ms is not None
                subscription = (await connection.execute(select(task_subscriptions))).one()
                assert subscription.status == SubscriptionStatus.EXPIRED
                assert subscription.expired_at_ms is not None
        finally:
            await database.close()

    asyncio.run(check())
    repeated = client.post("/api/v2/history/purge", json={})
    assert repeated.json()["expired_tasks"] == 0


def test_history_purge_requires_admin_session_and_valid_condition(client: TestClient) -> None:
    create_admin(client)
    login(client)
    create_user(client)
    login(client, "user", OTHER_PASSWORD)
    assert client.post("/api/v2/history/purge", json={}).status_code == 403
    login(client)
    both = client.post("/api/v2/history/purge", json={"before_ms": 1, "older_than_days": 2})
    assert both.status_code == 422
    assert client.post("/api/v2/history/purge", json={"older_than_days": 1}).status_code == 200


@pytest.mark.parametrize("detached_marker", [True, False], ids=["purged", "empty-payload"])
def test_recreating_empty_source_rebuilds_payload_and_supports_new_retry(
    client: TestClient, settings: Settings, detached_marker: bool
) -> None:
    create_admin(client)
    login(client)
    backend = install_backend(client)
    uri = "https://example.com/file"
    mirror = "https://mirror.example/file"
    options = {"headers": {"X-Test": "one"}, "mirrors": [mirror]}
    created = client.post(
        "/api/v2/tasks", json={"tasks": [{"uri": uri, "options": options}]}
    ).json()["items"][0]
    old_task_id = created["task"]["id"]
    assert client.post(
        "/api/v2/tasks/cancel", json={"task_ids": [old_task_id]}
    ).json()["items"][0]["accepted"] is True
    assert client.post(
        "/api/v2/history/purge", json={"before_ms": 2**63 - 1}
    ).json()["detached_sources"] == 1

    if not detached_marker:
        async def clear_marker() -> None:
            database = SQLiteDatabase(settings.database_path)
            try:
                async with database.connection() as connection:
                    await connection.execute(update(download_sources).values(detached_at_ms=None))
                    await connection.commit()
            finally:
                await database.close()

        asyncio.run(clear_marker())

    recreated = client.post(
        "/api/v2/tasks", json={"tasks": [{"uri": uri, "options": options}]}
    )
    assert recreated.status_code == 200, recreated.text
    new_task = recreated.json()["items"][0]
    assert new_task["accepted"] is True
    new_task_id = new_task["task"]["id"]
    expected_payload = {"uris": [uri, mirror]}
    expected_options = {"headers": {"x-test": "one"}, "mirrors": [mirror]}
    assert backend.submit_calls[-1][0].source.payload == expected_payload
    assert backend.submit_calls[-1][0].source.options == expected_options
    assert backend.submit_calls[-1][0].source.detached_at_ms is None
    assert client.post(f"/api/v2/tasks/{old_task_id}/retry").status_code == 409

    assert client.post(
        "/api/v2/tasks/cancel", json={"task_ids": [new_task_id]}
    ).json()["items"][0]["accepted"] is True
    retried = client.post(f"/api/v2/tasks/{new_task_id}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["outcome"] == "created"
    assert backend.submit_calls[-1][0].source.payload == expected_payload

    history = client.get("/api/v2/history", params={"status": "expired"}).json()["items"]
    assert [(item["id"], item["status"]) for item in history] == [
        (old_task_id, "expired")
    ]

    async def check() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with database.connection() as connection:
                source = (await connection.execute(select(download_sources))).one()
                assert source.payload_json != "{}"
                assert source.detached_at_ms is None
                old_subscription = (
                    await connection.execute(
                        select(task_subscriptions).where(task_subscriptions.c.id == old_task_id)
                    )
                ).one()
                assert old_subscription.status == SubscriptionStatus.EXPIRED
                assert old_subscription.display_name == "file"
        finally:
            await database.close()

    asyncio.run(check())


def test_clear_all_history_removes_only_terminal_tasks(
    client: TestClient, settings: Settings
) -> None:
    """A2：一键清空本人全部终态任务（含有效终态），进行中任务保留。"""
    admin = create_admin(client)
    login(client)
    install_backend(client)
    user_id = int(admin["id"])
    base = _now_ms()
    _, completed = seed_task(settings, user_id=user_id, name="Alpha complete", subscription_status=SubscriptionStatus.COMPLETED, attempt_status=AttemptStatus.COMPLETED, created_at_ms=base)
    _, failed = seed_task(settings, user_id=user_id, name="Beta failed", subscription_status=SubscriptionStatus.FAILED, attempt_status=AttemptStatus.FAILED, created_at_ms=base + 1)
    _, live = seed_task(settings, user_id=user_id, name="Live", created_at_ms=base + 2)
    # 订阅仍 live、尝试已终态：有效状态同样属于终态，应被清空。
    _, lagging = seed_task(settings, user_id=user_id, name="Lagging", subscription_status=SubscriptionStatus.ACTIVE, attempt_status=AttemptStatus.COMPLETED, created_at_ms=base + 3)
    # 他人任务不受影响。
    other_row = create_user(client, "other", password=OTHER_PASSWORD)
    other_id = int(other_row["id"])
    login(client, "other", OTHER_PASSWORD)
    _, other_terminal = seed_task(settings, user_id=other_id, name="Other done", subscription_status=SubscriptionStatus.COMPLETED, attempt_status=AttemptStatus.COMPLETED, created_at_ms=base)
    login(client)

    response = client.delete("/api/v2/history/all")

    assert response.status_code == 200
    assert response.json() == {"cleared_count": 3}
    # 进行中任务保留，历史清空。
    tasks = client.get("/api/v2/tasks").json()
    remaining_ids = {item["id"] for item in tasks["items"]}
    assert live in remaining_ids
    assert completed not in remaining_ids and failed not in remaining_ids
    assert lagging not in remaining_ids
    assert client.get("/api/v2/history").json()["total"] == 0
    # 重复清空返回 0。
    assert client.delete("/api/v2/history/all").json() == {"cleared_count": 0}

    # 他人历史仍在。
    login(client, "other", OTHER_PASSWORD)
    other_history = client.get("/api/v2/history").json()
    assert other_history["total"] == 1
    assert other_history["items"][0]["id"] == other_terminal
