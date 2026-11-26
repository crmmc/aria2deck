from __future__ import annotations

import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork
from tests.api.conftest import OTHER_PASSWORD, create_admin, create_user, login

ORPHANS = "/api/v2/storage/disk-orphans"
QUEUE = f"{ORPHANS}/queue"


def write_orphan(settings: Settings, payload: bytes) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    target = settings.download_directory / "objects" / f"file-{digest}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return f"objects/file-{digest}"


async def register_content(settings: Settings, storage_path: str, digest: str, size: int) -> int:
    database = SQLiteDatabase(settings.database_path)
    try:
        async with SQLiteUnitOfWork(database) as uow:
            return await ContentRepository(uow.connection).create_object(
                kind="file", digest=digest, size=size, storage_path=storage_path, now_ms=1
            )
    finally:
        await database.close()


async def deletion_state(settings: Settings, content_id: int) -> str:
    database = SQLiteDatabase(settings.database_path)
    try:
        async with SQLiteUnitOfWork(database) as uow:
            record = await ContentRepository(uow.connection).get_object(content_id)
        return record.deletion_state.value
    finally:
        await database.close()


def setup_admin(client: TestClient) -> None:
    create_admin(client)
    login(client)


def stop_background_workers(client: TestClient, app: FastAPI) -> None:
    """停掉随应用启动的后台任务，让用例自己逐步驱动 worker（避免与轮询抢条目）。"""
    client.portal.call(app.state.supervisor.stop)


def test_scan_contract_returns_only_unregistered_canonical_entries(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    setup_admin(client)
    empty = client.get(ORPHANS)
    assert empty.status_code == 200
    assert empty.json() == {
        "items": [],
        "total": 0,
        "page": 1,
        "page_size": 20,
        "unrecognized_count": 0,
    }

    payload = b"orphan payload"
    storage_path = write_orphan(settings, payload)
    registered = write_orphan(settings, b"already registered")
    client.portal.call(
        register_content,
        settings,
        registered,
        registered.rpartition("-")[2],
        len(b"already registered"),
    )
    objects = settings.download_directory / "objects"
    (objects / "not-canonical.txt").write_bytes(b"x")

    listing = client.get(ORPHANS, params={"page": 1, "page_size": 100})
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    assert body["unrecognized_count"] == 1
    item = body["items"][0]
    assert item["storage_path"] == storage_path
    assert (item["kind"], item["size"]) == ("file", len(payload))
    assert item["digest"] == storage_path.rpartition("-")[2]
    assert item["modified_at_ms"] > 0

    filtered = client.get(ORPHANS, params={"search": "NOT-FOUND-TERM"})
    assert filtered.json()["total"] == 0
    assert client.get(ORPHANS, params={"search": "OBJECTS/FILE-"}).json()["total"] == 1

    for params in ({"page_size": 101}, {"page_size": 0}, {"page": 0}):
        invalid = client.get(ORPHANS, params=params)
        assert invalid.status_code == 422, params
        assert invalid.json()["error"]["code"] == "invalid_input"


def test_queue_enqueue_is_per_item_and_idempotent(client: TestClient, settings: Settings) -> None:
    setup_admin(client)
    storage_path = write_orphan(settings, b"payload")

    mixed = client.post(
        QUEUE,
        json={"storage_paths": ["../etc/passwd", storage_path, f"objects/file-{'a' * 64}"]},
    )
    assert mixed.status_code == 200
    results = mixed.json()
    assert [(item["storage_path"], item["accepted"]) for item in results] == [
        ("../etc/passwd", False),
        (storage_path, True),
        (f"objects/file-{'a' * 64}", False),
    ]
    assert results[0]["error"]["code"] == "invalid_input"
    assert results[0]["error"]["message"] == "存储路径格式无效"
    assert results[1]["state"] == "queued"
    assert results[1]["error"] is None
    assert results[2]["error"]["code"] == "not_found"

    again = client.post(QUEUE, json={"storage_paths": [storage_path]})
    assert again.status_code == 200
    assert [(item["accepted"], item["state"]) for item in again.json()] == [(True, "queued")]

    for body in ({"storage_paths": []}, {"storage_paths": ["objects/x"] * 1001}):
        invalid = client.post(QUEUE, json=body)
        assert invalid.status_code == 422
    forbidden_field = client.post(
        QUEUE, json={"storage_paths": [storage_path], "wait": True}
    )
    assert forbidden_field.status_code == 422


def test_queue_snapshot_reports_counts_and_start_stop(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    setup_admin(client)
    stop_background_workers(client, app)
    storage_path = write_orphan(settings, b"payload")
    client.post(QUEUE, json={"storage_paths": [storage_path]})

    snapshot = client.get(QUEUE)
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert (body["running"], body["queued_count"], body["pending_bytes"]) == (False, 1, 7)
    assert (body["running_count"], body["done_count"], body["failed_count"]) == (0, 0, 0)
    item = body["items"][0]
    assert (item["storage_path"], item["state"], item["kind"], item["size"]) == (
        storage_path,
        "queued",
        "file",
        7,
    )
    assert (item["started_at_ms"], item["finished_at_ms"], item["content_id"]) == (
        None,
        None,
        None,
    )

    started = client.post(f"{QUEUE}/start")
    assert started.status_code == 200
    assert started.json()["running"] is True
    stopped = client.post(f"{QUEUE}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["running"] is False


def test_queue_processes_and_defers_deletion_of_an_admitted_entry(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    setup_admin(client)
    stop_background_workers(client, app)
    payload = b"payload"
    storage_path = write_orphan(settings, payload)
    client.post(QUEUE, json={"storage_paths": [storage_path]})
    client.post(f"{QUEUE}/start")

    processed = client.portal.call(app.state.disk_admission.step_once)
    assert processed is True

    snapshot = client.get(QUEUE).json()
    item = snapshot["items"][0]
    assert (item["state"], snapshot["done_count"]) == ("done", 1)
    assert item["content_id"] is not None
    assert client.get(ORPHANS).json()["total"] == 0, "入库后从磁盘清单消失"

    deleted = client.request("DELETE", QUEUE, json={"storage_paths": [storage_path]})
    assert deleted.status_code == 200
    result = deleted.json()[0]
    assert (result["accepted"], result["mode"], result["freed_bytes"]) == (True, "deferred", 0)
    assert client.get(QUEUE).json()["items"] == []
    assert client.portal.call(deletion_state, settings, item["content_id"]) == "pending_delete"
    assert (settings.download_directory / storage_path).exists(), "物理删除由既有后台任务完成"

    client.post(f"{QUEUE}/stop")


def test_delete_handles_queued_missing_and_running_entries(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    setup_admin(client)
    stop_background_workers(client, app)
    queued = write_orphan(settings, b"queued")
    client.post(QUEUE, json={"storage_paths": [queued]})

    immediate = client.request(
        "DELETE", QUEUE, json={"storage_paths": [queued, "../etc/passwd"]}
    )
    assert immediate.status_code == 200
    first, second = immediate.json()
    assert (first["accepted"], first["mode"], first["freed_bytes"]) == (True, "immediate", 6)
    assert not (settings.download_directory / queued).exists()
    assert (second["accepted"], second["error"]["code"]) == (False, "invalid_input")

    running = write_orphan(settings, b"running")
    client.post(QUEUE, json={"storage_paths": [running]})
    queue = app.state.disk_orphans.queue
    queue.start()
    assert queue.claim_next(now_ms=1) is not None
    rejected = client.request("DELETE", QUEUE, json={"storage_paths": [running]})
    assert rejected.status_code == 200
    body = rejected.json()[0]
    assert (body["accepted"], body["error"]["code"]) == (False, "content_busy")
    assert body["error"]["message"] == "正在校验中，请先停止处理"
    assert (settings.download_directory / running).exists()
    queue.stop()


def test_disk_orphan_endpoints_are_admin_only(client: TestClient, app: FastAPI) -> None:
    create_admin(client)
    login(client)
    create_user(client)
    login(client, "user", OTHER_PASSWORD)

    for method, path, body in (
        ("get", ORPHANS, None),
        ("get", QUEUE, None),
        ("post", QUEUE, {"storage_paths": ["objects/file-" + "a" * 64]}),
        ("delete", QUEUE, {"storage_paths": ["objects/file-" + "a" * 64]}),
        ("post", f"{QUEUE}/start", None),
        ("post", f"{QUEUE}/stop", None),
    ):
        refused = client.request(method.upper(), path, json=body)
        assert refused.status_code == 403, (method, path)
        assert refused.json()["error"]["code"] == "permission_denied"
        denied = TestClient(app).request(method.upper(), path, json=body)
        assert denied.status_code == 401, (method, path)


def test_disk_admission_worker_is_registered_with_the_supervisor(app: FastAPI) -> None:
    assert "disk-admission" in [worker.name for worker in app.state.supervisor.snapshot()]
