from __future__ import annotations

import sqlite3
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria2deck.infrastructure.config import Settings
from tests.api.conftest import OTHER_PASSWORD, create_admin, create_user, login
from tests.api.test_files import seed_file


def _wait_pack_status(
    client: TestClient, pack_id: int, statuses: tuple[str, ...], timeout: float = 30.0
) -> dict[str, object]:
    """后台 pack-dispatcher 每 1s 推进一轮：轮询等待真实 worker 收敛。"""
    deadline = time.monotonic() + timeout
    last = "unknown"
    while time.monotonic() < deadline:
        view = client.get(f"/api/v2/packs/{pack_id}")
        if view.status_code == 200:
            last = str(view.json()["status"])
            if last in statuses:
                return dict(view.json())
        time.sleep(0.02)
    raise AssertionError(f"pack {pack_id} 未在 {timeout}s 内进入 {statuses}（当前 {last}）")


def _delay_pack_claim(settings: Settings, pack_id: int, *, delay_ms: int = 5000) -> None:
    """推迟领取：消除「创建→改名」与调度器 1s 轮次之间在全量负载下的竞态窗口。"""
    with sqlite3.connect(settings.database_path) as conn:
        conn.execute(
            "UPDATE pack_jobs SET next_attempt_at_ms = ? WHERE id = ?",
            (int(time.time() * 1000) + delay_ms, pack_id),
        )


def _setup_two_users(client: TestClient) -> tuple[int, int]:
    admin = create_admin(client)
    login(client)
    user = create_user(client)
    login(client, "user", OTHER_PASSWORD)
    return int(admin["id"]), int(user["id"])


def test_precompute_contract(client: TestClient, settings) -> None:
    _, user_id = _setup_two_users(client)
    first = seed_file(settings, user_id=user_id, index=1, payload=b"a" * 10)
    second = seed_file(settings, user_id=user_id, index=2, payload=b"b" * 5)

    response = client.post("/api/v2/packs/precompute", json={"items": [first[1], first[1], 999, second[1]]})
    assert response.status_code == 200
    body = response.json()
    assert body["total_size"] == 15
    accepted = [item for item in body["items"] if item["accepted"]]
    rejected = {item["user_file_id"]: item for item in body["items"] if not item["accepted"]}
    assert len(accepted) == 2
    assert rejected[999]["error"]["code"] == "not_found"
    assert 0 < body["available_bytes"]

    assert client.post("/api/v2/packs/precompute", json={"items": []}).status_code == 422
    assert client.post("/api/v2/packs/precompute", json={"items": [1] * 1001}).status_code == 422


def test_create_contract_success_and_failures(client: TestClient, settings) -> None:
    _, user_id = _setup_two_users(client)
    first = seed_file(settings, user_id=user_id, index=1, payload=b"a" * 10)
    second = seed_file(settings, user_id=user_id, index=2, payload=b"b" * 5)

    created = client.post(
        "/api/v2/packs",
        json={"items": [first[1], second[1]], "output_name": "bundle.zip", "delete_source": False},
    )
    assert created.status_code == 201
    view = created.json()
    assert view["status"] == "pending"
    assert view["delete_source"] is False
    assert view["output"] is None

    assert client.post("/api/v2/packs", json={"items": [999], "output_name": "x.zip", "delete_source": False}).status_code == 404
    assert client.post("/api/v2/packs", json={"items": [first[1]], "output_name": "a/b", "delete_source": False}).status_code == 422
    assert client.post("/api/v2/packs", json={"items": [first[1]] * 101, "output_name": "many.zip", "delete_source": False}).status_code == 422
    assert client.post("/api/v2/packs", json={"items": [], "output_name": "none.zip", "delete_source": False}).status_code == 422


def test_pack_execution_progress_and_output(client: TestClient, app: FastAPI, settings) -> None:
    _, user_id = _setup_two_users(client)
    payload_a, payload_b = b"a" * 10, b"b" * 25
    first = seed_file(settings, user_id=user_id, index=1, payload=payload_a)
    second = seed_file(settings, user_id=user_id, index=2, payload=payload_b)
    pack_id = client.post(
        "/api/v2/packs",
        json={"items": [first[1], second[1]], "output_name": "bundle.zip", "delete_source": False},
    ).json()["id"]

    view = _wait_pack_status(client, pack_id, ("done",))
    assert view["output"] is not None
    assert view["step"] is None and view["step_progress"] == 100
    assert view["output"] is not None and view["output"]["display_name"] == "bundle.zip"
    assert client.get("/api/v2/packs/999").status_code == 404

    import zipfile

    archives = [
        candidate
        for candidate in (settings.download_directory / "objects").glob("file-*")
        if zipfile.is_zipfile(candidate)
    ]
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as archive:
        assert archive.read("file-1") == payload_a
        assert archive.read("file-2") == payload_b


def test_pack_failure_progress_and_lifecycle(client: TestClient, app: FastAPI, settings) -> None:
    _, user_id = _setup_two_users(client)
    first = seed_file(settings, user_id=user_id, index=1, payload=b"a" * 10)
    # 确定性失败：领取前改名使快照失配。
    pack_id = client.post(
        "/api/v2/packs",
        json={"items": [first[1]], "output_name": "renamed.zip", "delete_source": False},
    ).json()["id"]
    _delay_pack_claim(settings, pack_id)
    client.patch(f"/api/v2/files/{first[1]}", json={"name": "moved.bin"})
    view = _wait_pack_status(client, pack_id, ("failed",))
    assert view["error_code"] == "invalid_state"

    # 取消语义：pending → cancelled；重复取消 409；终态删除成功。
    other = seed_file(settings, user_id=user_id, index=3, payload=b"c" * 4)
    pending = client.post(
        "/api/v2/packs",
        json={"items": [other[1]], "output_name": "cancelme.zip", "delete_source": False},
    ).json()["id"]
    assert client.post(f"/api/v2/packs/{pending}/cancel").status_code == 200
    assert client.post(f"/api/v2/packs/{pending}/cancel").status_code == 409
    assert client.delete(f"/api/v2/packs/{pending}").status_code == 200
    assert client.get(f"/api/v2/packs/{pending}").status_code == 404
    assert client.delete(f"/api/v2/packs/{pending}").status_code == 404

    # 清记录语义：不删源的完成任务立即可清；删源任务等待来源清理收敛后可清
    # （409 门语义在 tests/application/test_pack_cleanup.py 有确定性证据）。
    third = seed_file(settings, user_id=user_id, index=4, payload=b"d" * 6)
    done = client.post(
        "/api/v2/packs",
        json={"items": [third[1]], "output_name": "cleanup.zip", "delete_source": True},
    ).json()["id"]
    _wait_pack_status(client, done, ("done",))
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if client.delete(f"/api/v2/packs/{done}/record").status_code == 200:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("来源清理后清记录未收敛")
    assert client.get(f"/api/v2/packs/{done}").status_code == 404


def test_create_rate_limit(client: TestClient, settings) -> None:
    _, user_id = _setup_two_users(client)
    login(client)
    client.patch("/api/v2/settings", json={"create_pack_rate_limit": 1})
    login(client, "user", OTHER_PASSWORD)
    first = seed_file(settings, user_id=user_id, index=1, payload=b"a")
    assert client.post("/api/v2/packs", json={"items": [first[1]], "output_name": "one.zip", "delete_source": False}).status_code == 201
    limited = client.post("/api/v2/packs", json={"items": [first[1]], "output_name": "two.zip", "delete_source": False})
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers


def test_openapi_contains_pack_and_share_paths(app: FastAPI) -> None:
    schema = app.openapi()
    paths = schema["paths"]
    for expected in (
        "/api/v2/packs/precompute",
        "/api/v2/packs",
        "/api/v2/packs/{pack_id}",
        "/api/v2/packs/{pack_id}/cancel",
        "/api/v2/packs/{pack_id}/record",
        "/api/v2/shares",
        "/api/v2/shares/revoke",
        "/api/v2/shares/all",
        "/api/s/{code}/info",
        "/api/s/{code}/verify",
        "/api/s/{code}/entries",
        "/api/s/{code}/download",
    ):
        assert expected in paths, expected
    assert not any(path.startswith("/api/packs") or path.startswith("/api/shares") for path in paths)
    verify_responses = paths["/api/s/{code}/verify"]["post"]["responses"]
    assert "410" in verify_responses and "403" in verify_responses


def test_list_recent_packs_scopes_to_owner_and_orders_newest_first(
    client: TestClient, settings
) -> None:
    _, user_id = _setup_two_users(client)
    first = seed_file(settings, user_id=user_id, index=1, payload=b"a" * 10)
    second = seed_file(settings, user_id=user_id, index=2, payload=b"b" * 5)

    created_a = client.post(
        "/api/v2/packs",
        json={"items": [first[1]], "output_name": "a.zip", "delete_source": False},
    ).json()["id"]
    created_b = client.post(
        "/api/v2/packs",
        json={"items": [second[1]], "output_name": "b.zip", "delete_source": False},
    ).json()["id"]

    listing = client.get("/api/v2/packs")
    assert listing.status_code == 200
    body = listing.json()
    ids = [item["id"] for item in body["items"]]
    assert ids == [created_b, created_a]  # 新任务在前。
    assert body["items"][0]["output_name"] == "b.zip"
    assert {item["id"] for item in body["items"]} <= {created_a, created_b}

    # limit 生效。
    assert [item["id"] for item in client.get("/api/v2/packs", params={"limit": 1}).json()["items"]] == [
        created_b
    ]
    assert client.get("/api/v2/packs", params={"limit": 0}).status_code == 422
    assert client.get("/api/v2/packs", params={"limit": 51}).status_code == 422

    # 管理员视角看不到他人任务。
    login(client)
    assert client.get("/api/v2/packs").json()["items"] == []
    # 未登录不可见。
    client.cookies.clear()
    assert client.get("/api/v2/packs").status_code == 401
