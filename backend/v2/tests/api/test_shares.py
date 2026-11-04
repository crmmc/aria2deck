from __future__ import annotations

from fastapi.testclient import TestClient

from tests.api.conftest import OTHER_PASSWORD, THIRD_PASSWORD, create_admin, create_user, login
from tests.api.test_files import seed_file

SHARE_PASSWORD = "测试口令"  # noqa: S105  # 测试夹具口令，非凭据


def _setup(client: TestClient) -> tuple[int, int]:
    admin = create_admin(client)
    login(client)
    user = create_user(client)
    login(client, "user", OTHER_PASSWORD)
    return int(admin["id"]), int(user["id"])


def _user_id_by_name(client: TestClient) -> int:
    return int(client.get("/api/v2/auth/me").json()["id"])


def _create(client: TestClient, file_id: int, **overrides: object) -> object:
    payload: dict[str, object] = {"user_file_id": file_id}
    payload.update(overrides)
    return client.post("/api/v2/shares", json=payload)


def test_share_create_contract(client: TestClient, settings) -> None:
    admin_id, user_id = _setup(client)
    _, file_id = seed_file(settings, user_id=user_id, index=1, payload=b"data")

    created = _create(client, file_id, expires_in_seconds=3600, max_downloads=5, password=SHARE_PASSWORD)
    assert created.status_code == 201
    body = created.json()
    assert body["code"] and body["status"] == "active"
    assert body["has_password"] is True
    assert body["file_name"] == "file-1"
    assert body["download_count"] == 0

    assert _create(client, 999).status_code == 404
    foreign = seed_file(settings, user_id=admin_id, index=2, payload=b"x")
    assert _create(client, foreign[1]).status_code == 404
    assert _create(client, file_id, expires_in_seconds=0).status_code == 422
    assert _create(client, file_id, max_downloads=10001).status_code == 422
    assert _create(client, file_id, password="").status_code == 422


def test_share_capacity_and_rate_limit(client: TestClient, settings) -> None:
    _, user_id = _setup(client)
    _, file_id = seed_file(settings, user_id=user_id, index=1, payload=b"data")
    for _ in range(10):
        assert _create(client, file_id, expires_in_seconds=3600).status_code == 201
    overflow = _create(client, file_id, expires_in_seconds=3600)
    assert overflow.status_code == 409
    assert overflow.json()["error"]["code"] == "share_limit_exceeded"

    login(client)
    create_user(client, "searcher", password=THIRD_PASSWORD)
    client.patch("/api/v2/settings", json={"create_share_rate_limit": 1})
    login(client, "searcher", THIRD_PASSWORD)  # 新用户使用干净的创建限流桶。
    searcher_file = seed_file(settings, user_id=_user_id_by_name(client), index=3, payload=b"more")[1]
    assert _create(client, searcher_file).status_code == 201
    limited = _create(client, searcher_file)
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers


def test_share_list_filter_and_isolation(client: TestClient, settings) -> None:
    admin_id, user_id = _setup(client)
    _, file_id = seed_file(settings, user_id=user_id, index=1, payload=b"data")
    _, foreign_file = seed_file(settings, user_id=admin_id, index=2, payload=b" theirs")
    active = _create(client, file_id, expires_in_seconds=3600).json()
    short = _create(client, file_id, expires_in_seconds=3600).json()

    listing = client.get("/api/v2/shares", params={"page": 1, "page_size": 10})
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 2
    assert {item["id"] for item in body["items"]} == {active["id"], short["id"]}
    assert all(item["status"] == "active" for item in body["items"])

    revoked = client.post("/api/v2/shares/revoke", json={"share_ids": [short["id"]]})
    assert revoked.status_code == 200
    assert revoked.json()["results"] == [
        {"share_id": short["id"], "ok": True, "already_revoked": False, "error": None}
    ]
    filtered = client.get("/api/v2/shares", params={"status": "revoked"})
    assert [item["id"] for item in filtered.json()["items"]] == [short["id"]]
    assert client.get("/api/v2/shares", params={"status": "active"}).json()["total"] == 1


def test_share_revoke_delete_and_revoke_all(client: TestClient, settings) -> None:
    _setup(client)
    login(client)
    create_user(client, "searcher", password=THIRD_PASSWORD)
    login(client, "user", OTHER_PASSWORD)
    user_id = _user_id_by_name(client)
    first = seed_file(settings, user_id=user_id, index=1, payload=b"a")[1]
    second = seed_file(settings, user_id=user_id, index=2, payload=b"bb")[1]
    one = _create(client, first).json()
    two = _create(client, second).json()

    repeat = client.post("/api/v2/shares/revoke", json={"share_ids": [one["id"], one["id"], 999]})
    assert repeat.status_code == 200
    results = repeat.json()["results"]
    assert [(r["ok"], r["already_revoked"], r["error"]["code"] if r["error"] else None) for r in results] == [
        (True, False, None),
        (True, True, None),
        (False, False, "not_found"),
    ]

    deleted = client.request(
        "DELETE", "/api/v2/shares", json={"share_ids": [one["id"], one["id"]]}
    )
    assert deleted.status_code == 200
    assert [r["ok"] for r in deleted.json()["results"]] == [True, False]
    assert client.get(f"/api/s/{one['code']}/info").status_code == 404

    all_revoked = client.delete("/api/v2/shares/all")
    assert all_revoked.status_code == 200
    assert all_revoked.json()["revoked_count"] == 1  # two 仍在，one 行已删。
    assert client.get(f"/api/s/{two['code']}/info").status_code == 410

    assert client.post("/api/v2/shares/revoke", json={"share_ids": []}).status_code == 422
    assert client.request("DELETE", "/api/v2/shares", json={"share_ids": [1] * 1001}).status_code == 422
    login(client, "searcher", THIRD_PASSWORD)  # searcher 由管理员在本测试开头创建。
    foreign_view = client.get("/api/v2/shares")
    assert foreign_view.json()["total"] == 0
