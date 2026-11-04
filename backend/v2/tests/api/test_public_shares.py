from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork
from tests.api.conftest import OTHER_PASSWORD, create_admin, create_user, login
from tests.api.test_files import seed_file

PASSWORD = "公开口令"  # noqa: S105  # 测试夹具口令，非凭据


def _setup(client: TestClient) -> int:
    create_admin(client)
    login(client)
    user = create_user(client)
    login(client, "user", OTHER_PASSWORD)
    return int(user["id"])


def _seed_directory(settings, user_id: int, index: int) -> int:
    root = settings.download_directory / "objects" / f"directory-{index}"
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "child.bin").write_bytes(b"abcd")

    async def write() -> int:
        from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository

        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                repository = ContentRepository(uow.connection)
                content_id = await repository.create_object(
                    kind="directory",
                    digest=f"{index:064x}",
                    size=4,
                    storage_path=f"objects/directory-{index}",
                    now_ms=1,
                )
                await repository.add_entry(
                    content_id=content_id, parent_path="", name="docs",
                    entry_kind="directory", size=0, now_ms=1,
                )
                await repository.add_entry(
                    content_id=content_id, parent_path="docs", name="child.bin",
                    entry_kind="file", size=4, now_ms=1,
                )
                return await repository.add_user_file(
                    user_id=user_id, content_id=content_id, display_name=f"dir-{index}", now_ms=1
                )
        finally:
            await database.close()

    return asyncio.run(write())


def _make_share(client: TestClient, file_id: int, **overrides: object) -> str:
    payload: dict[str, object] = {"user_file_id": file_id}
    payload.update(overrides)
    response = client.post("/api/v2/shares", json=payload)
    assert response.status_code == 201, response.text
    return str(response.json()["code"])


def test_public_info_no_auth_and_states(client: TestClient, settings) -> None:
    user_id = _setup(client)
    file_id = seed_file(settings, user_id=user_id, index=1, payload=b"payload")[1]
    code = _make_share(client, file_id, expires_in_seconds=3600, max_downloads=3, password=PASSWORD)

    anon = TestClient(client.app)
    info = anon.get(f"/api/s/{code}/info")
    assert info.status_code == 200
    body = info.json()
    assert (body["file_name"], body["file_size"], body["file_kind"]) == ("file-1", 7, "file")
    assert body["has_password"] is True
    assert "password_hash" not in body and PASSWORD not in info.text
    assert anon.get("/api/s/missing/info").status_code == 404

    share_id = client.get("/api/v2/shares").json()["items"][0]["id"]
    client.post("/api/v2/shares/revoke", json={"share_ids": [share_id]})
    assert anon.get(f"/api/s/{code}/info").status_code == 410
    assert anon.get(f"/api/s/{code}/info").json()["error"]["code"] == "share_revoked"


def test_verify_semantics_and_rate_limit(client: TestClient, settings) -> None:
    user_id = _setup(client)
    plain = seed_file(settings, user_id=user_id, index=1, payload=b"p")[1]
    guarded = seed_file(settings, user_id=user_id, index=2, payload=b"g")[1]
    plain_code = _make_share(client, plain)
    guarded_code = _make_share(client, guarded, expires_in_seconds=3600, password=PASSWORD)
    anon = TestClient(client.app)

    no_password = anon.post(f"/api/s/{plain_code}/verify", json={"password": "x"})
    assert no_password.status_code == 422
    assert no_password.json()["error"]["code"] == "invalid_input"
    wrong = anon.post(f"/api/s/{guarded_code}/verify", json={"password": "不对"})
    assert wrong.status_code == 403
    assert wrong.json()["error"]["code"] == "password_invalid"
    ok = anon.post(f"/api/s/{guarded_code}/verify", json={"password": PASSWORD})
    assert ok.status_code == 200
    assert ok.json()["expires_in"] == 1800 and ok.json()["access_token"]

    # 失效分享验密 410：直接把过期时间写到过去。
    async def expire() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with database.connection() as conn:
                await conn.execute(
                    text("UPDATE share_links SET expires_at_ms = 1 WHERE code = :c"),
                    {"c": guarded_code},
                )
                await conn.commit()
        finally:
            await database.close()

    asyncio.run(expire())
    assert anon.post(f"/api/s/{guarded_code}/verify", json={"password": PASSWORD}).status_code == 410

    fresh = _make_share(client, guarded, expires_in_seconds=3600, password=PASSWORD)
    login(client)
    client.patch("/api/v2/settings", json={"share_access_rate_limit": 1})
    login(client, "user", OTHER_PASSWORD)
    limited_client = TestClient(client.app, client=("203.0.113.9", 1234))  # 独立 IP 限流桶。
    assert limited_client.post(f"/api/s/{fresh}/verify", json={"password": PASSWORD}).status_code == 200
    second = limited_client.post(f"/api/s/{fresh}/verify", json={"password": PASSWORD})
    assert second.status_code == 429
    assert "Retry-After" in second.headers


def test_browse_and_query_token_ignored(client: TestClient, settings) -> None:
    user_id = _setup(client)
    dir_file = _seed_directory(settings, user_id, 2)
    file_id = seed_file(settings, user_id=user_id, index=1, payload=b"z")[1]
    dir_code = _make_share(client, dir_file)
    file_code = _make_share(client, file_id)
    anon = TestClient(client.app)

    # 无密码分享走不了验密接口：测试用应用内 token 存储直接签发。
    issued = asyncio.run(_issue_token(client.app, dir_code))
    headers = {"Authorization": f"Bearer {issued}"}

    entries = anon.get(f"/api/s/{dir_code}/entries", headers=headers)
    assert entries.status_code == 200
    body = entries.json()
    assert body["total"] == 1
    assert [(item["name"], item["kind"]) for item in body["items"]] == [("docs", "directory")]
    assert [c["name"] for c in body["breadcrumbs"]] == ["dir-2"]
    nested = anon.get(f"/api/s/{dir_code}/entries", params={"path": "docs"}, headers=headers)
    assert nested.json()["items"][0]["path"] == "docs/child.bin"

    assert anon.get(f"/api/s/{dir_code}/entries").status_code == 401
    assert anon.get(f"/api/s/{dir_code}/entries?token={issued}").status_code == 401, "query token 必须被忽略"
    assert anon.get(f"/api/s/{file_code}/entries", headers=headers).status_code == 401, "令牌绑定单一分享码"
    file_token = asyncio.run(_issue_token(client.app, file_code))
    assert anon.get(f"/api/s/{file_code}/entries", headers={"Authorization": f"Bearer {file_token}"}).status_code == 422


async def _issue_token(app: FastAPI, code: str) -> str:
    return app.state.public_shares.tokens.issue(code)[0]


def test_download_matrix_headers_and_form_channel(client: TestClient, settings) -> None:
    user_id = _setup(client)
    payload = b"0123456789"
    file_id = seed_file(settings, user_id=user_id, index=1, payload=payload)[1]
    dir_file = _seed_directory(settings, user_id, 3)
    code = _make_share(client, file_id, max_downloads=5)  # 非 bytes 回退也是完整实体，计入计数。
    dir_code = _make_share(client, dir_file)
    anon = TestClient(client.app)
    token = asyncio.run(_issue_token(client.app, code))
    headers = {"Authorization": f"Bearer {token}"}

    full = anon.get(f"/api/s/{code}/download", headers=headers)
    assert full.status_code == 200
    assert full.content == payload
    assert full.headers["content-length"] == "10"
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-disposition"].startswith("attachment;")

    partial = anon.get(f"/api/s/{code}/download", headers={**headers, "Range": "bytes=2-4"})
    assert partial.status_code == 206
    assert partial.content == payload[2:5]
    assert partial.headers["content-range"] == "bytes 2-4/10"

    suffix = anon.get(f"/api/s/{code}/download", headers={**headers, "Range": "bytes=-3"})
    assert suffix.status_code == 206 and suffix.content == payload[-3:]

    fallback = anon.get(f"/api/s/{code}/download", headers={**headers, "Range": "items=0-1"})
    assert fallback.status_code == 200 and fallback.content == payload

    invalid = anon.get(f"/api/s/{code}/download", headers={**headers, "Range": "bytes=99-"})
    assert invalid.status_code == 416
    assert invalid.json()["error"]["code"] == "range_not_satisfiable"

    # query token 一律不读：无 Bearer 头即未携带。
    assert anon.get(f"/api/s/{code}/download", params={"token": token}).status_code == 401

    # 表单通道（x-www-form-urlencoded）。
    form = anon.post(f"/api/s/{code}/download", data={"token": token})
    assert form.status_code == 200 and form.content == payload
    assert anon.post(f"/api/s/{code}/download", data={"token": "wrong"}).status_code == 401
    assert anon.post(f"/api/s/{code}/download", data={}).status_code == 422, "缺 Content-Type 按非表单拒绝"

    # 目录内条目下载 + 目录整体拒绝。
    dir_token = asyncio.run(_issue_token(client.app, dir_code))
    entry = anon.get(
        f"/api/s/{dir_code}/download",
        params={"path": "docs/child.bin"},
        headers={"Authorization": f"Bearer {dir_token}", "Range": "bytes=1-2"},
    )
    assert entry.status_code == 206 and entry.content == b"bc"
    assert (
        anon.get(f"/api/s/{dir_code}/download", headers={"Authorization": f"Bearer {dir_token}"}).status_code
        == 422
    )

    # 计数：两次完整（200 + 全文件 206）计 2，第三完整达上限，之后 410。
    whole = anon.get(f"/api/s/{code}/download", headers={**headers, "Range": "bytes=0-"})
    assert whole.status_code == 206 and whole.content == payload
    third = anon.get(f"/api/s/{code}/download", headers=headers)
    assert third.status_code == 200
    exhausted = anon.get(f"/api/s/{code}/download", headers=headers)
    assert exhausted.status_code == 410
    assert exhausted.json()["error"]["code"] == "share_exhausted"


def test_form_channel_content_type_size_and_rate_consumption(
    client: TestClient, settings
) -> None:
    user_id = _setup(client)
    file_id = seed_file(settings, user_id=user_id, index=1, payload=b"form")[1]
    code = _make_share(client, file_id, max_downloads=10)
    token = asyncio.run(_issue_token(client.app, code))

    wrong_type = TestClient(client.app).post(
        f"/api/s/{code}/download", json={"token": token}
    )
    assert wrong_type.status_code == 422
    assert wrong_type.json()["error"]["code"] == "invalid_input"

    oversized = TestClient(client.app).post(
        f"/api/s/{code}/download", data={"token": token, "pad": "x" * (64 * 1024 + 1)}
    )
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "task_too_large"

    login(client)
    client.patch("/api/v2/settings", json={"share_access_rate_limit": 1})
    login(client, "user", OTHER_PASSWORD)
    limited = TestClient(client.app, client=("198.51.100.7", 2222))
    # 无 token 的请求也消耗限流额度：第一次 401，第二次 429。
    assert limited.post(f"/api/s/{code}/download", data={"token": ""}).status_code == 401
    throttled = limited.post(f"/api/s/{code}/download", data={"token": token})
    assert throttled.status_code == 429
    assert "Retry-After" in throttled.headers


def test_info_and_entries_are_rate_limited_by_ip(client: TestClient, settings) -> None:
    user_id = _setup(client)
    dir_file = _seed_directory(settings, user_id, 9)
    code = _make_share(client, dir_file)
    login(client)
    client.patch("/api/v2/settings", json={"share_access_rate_limit": 1})
    login(client, "user", OTHER_PASSWORD)
    token = asyncio.run(_issue_token(client.app, code))
    limited = TestClient(client.app, client=("198.51.100.8", 3333))
    headers = {"Authorization": f"Bearer {token}"}

    assert limited.get(f"/api/s/{code}/info").status_code == 200
    assert limited.get(f"/api/s/{code}/info").status_code == 429

    entries_client = TestClient(client.app, client=("198.51.100.9", 3334))
    assert entries_client.get(f"/api/s/{code}/entries", headers=headers).status_code == 200
    assert entries_client.get(f"/api/s/{code}/entries", headers=headers).status_code == 429
