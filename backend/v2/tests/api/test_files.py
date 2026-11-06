from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from aria2deck.domain.packs import PackSourceSnapshot
from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.packs import PackRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork
from tests.api.conftest import (
    OTHER_PASSWORD,
    THIRD_PASSWORD,
    create_admin,
    create_user,
    login,
)


def _digest(index: int) -> str:
    return f"{index:064x}"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def setup_users(client: TestClient) -> tuple[int, int]:
    admin = create_admin(client)
    login(client)
    user = create_user(client)
    login(client, "user", OTHER_PASSWORD)
    return int(admin["id"]), int(user["id"])


def remove_reference(settings: Settings, *, user_id: int, file_id: int) -> None:
    async def write() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                await ContentRepository(uow.connection).remove_user_file(
                    user_id=user_id, file_id=file_id
                )
                from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository

                await QuotaRepository(uow.connection).rebuild(user_id)
        finally:
            await database.close()

    asyncio.run(write())


def seed_file(
    settings: Settings,
    *,
    user_id: int,
    index: int,
    name: str | None = None,
    payload: bytes | None = None,
) -> tuple[int, int]:
    size = len(payload) if payload is not None else 10
    if payload is not None:
        target = settings.download_directory / "objects" / f"file-{index}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    async def write() -> tuple[int, int]:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                repository = ContentRepository(uow.connection)
                content_id = await repository.create_object(
                    kind="file",
                    digest=_digest(index),
                    size=size,
                    storage_path=f"objects/file-{index}",
                    now_ms=_now_ms(),
                )
                file_id = await repository.add_user_file(
                    user_id=user_id,
                    content_id=content_id,
                    display_name=name or f"file-{index}",
                    now_ms=_now_ms(),
                )
                from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository

                await QuotaRepository(uow.connection).rebuild(user_id)
                return content_id, file_id
        finally:
            await database.close()

    return asyncio.run(write())


def seed_directory(
    settings: Settings,
    *,
    user_id: int,
    index: int,
    entries: list[tuple[str, str, str, int]],
    payload_path: str | None = None,
    payload: bytes | None = None,
) -> tuple[int, int]:
    if payload_path is not None and payload is not None:
        target = settings.download_directory.joinpath(*payload_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    async def write() -> tuple[int, int]:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                repository = ContentRepository(uow.connection)
                content_id = await repository.create_object(
                    kind="directory",
                    digest=_digest(index),
                    size=sum(size for *_, size in entries),
                    storage_path=f"objects/directory-{index}",
                    now_ms=_now_ms(),
                )
                for parent, name, kind, size in entries:
                    await repository.add_entry(
                        content_id=content_id,
                        parent_path=parent,
                        name=name,
                        entry_kind=kind,
                        size=size,
                        now_ms=_now_ms(),
                    )
                file_id = await repository.add_user_file(
                    user_id=user_id,
                    content_id=content_id,
                    display_name=f"dir-{index}",
                    now_ms=_now_ms(),
                )
                from aria2deck.infrastructure.sqlite.repositories.quota import QuotaRepository

                await QuotaRepository(uow.connection).rebuild(user_id)
                return content_id, file_id
        finally:
            await database.close()

    return asyncio.run(write())


def seed_pack_source(settings: Settings, *, user_id: int, file_id: int, content_id: int) -> None:
    async def write() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                pack_id = await PackRepository(uow.connection).create_job(
                    owner_id=user_id,
                    output_name="a.zip",
                    pack_format="zip",
                    source_size=10,
                    reserved_bytes=0,
                    physical_reserved_bytes=0,
                    delete_source=True,
                    now_ms=_now_ms(),
                )
                await PackRepository(uow.connection).add_source(
                    pack_id,
                    PackSourceSnapshot(file_id, content_id, "file:" + _digest(1), "a", 1, 10),
                )
        finally:
            await database.close()

    asyncio.run(write())


def read_row(settings: Settings, query: str, params: dict[str, object]) -> object:
    async def read() -> object:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with database.connection() as connection:
                result = await connection.execute(text(query), params)
                return result.first()
        finally:
            await database.close()

    return asyncio.run(read())


def test_root_files_list_contract(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    admin_id, user_id = setup_users(client)
    seed_file(settings, user_id=user_id, index=1, name="Report.pdf", payload=b"0123456789")
    seed_file(settings, user_id=user_id, index=2, name="archive.zip")
    seed_file(settings, user_id=admin_id, index=3, name="admin-only.bin")

    response = client.get("/api/v2/files?page=1&page_size=2")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert [item["display_name"] for item in body["items"]] == ["Report.pdf", "archive.zip"]
    assert body["items"][0]["kind"] == "file"
    assert body["items"][0]["size"] == 10
    assert body["space"]["used_bytes"] == 20
    assert body["space"]["quota_bytes"] == 100 * 1024**3

    beyond = client.get("/api/v2/files?page=9&page_size=2")
    assert beyond.status_code == 200
    assert beyond.json()["items"] == []
    assert beyond.json()["total"] == 2

    anonymous = TestClient(app).get("/api/v2/files")
    assert anonymous.status_code == 401


def test_search_contract_scope_and_rate_limit(
    client: TestClient, settings: Settings
) -> None:
    _, user_id = setup_users(client)
    seed_file(settings, user_id=user_id, index=1, name="Report.pdf")
    _, dir_file = seed_directory(
        settings,
        user_id=user_id,
        index=2,
        entries=[("", "docs", "directory", 0), ("docs", "REPORT-q1.txt", "file", 2)],
    )
    _, plain_file = seed_file(settings, user_id=user_id, index=3, name="notes.txt")

    response = client.get("/api/v2/files", params={"search": "report"})
    assert response.status_code == 200
    body = response.json()
    assert body["truncated"] is False
    assert [(hit["display_name"], hit["path"]) for hit in body["hits"]] == [
        ("Report.pdf", ""),
        ("dir-2", "docs/REPORT-q1.txt"),
    ]

    scoped = client.get("/api/v2/files", params={"search": "report", "scope": dir_file})
    assert scoped.status_code == 200
    assert [hit["path"] for hit in scoped.json()["hits"]] == ["docs/REPORT-q1.txt"]

    blank = client.get("/api/v2/files", params={"search": "   "})
    assert blank.status_code == 422
    assert blank.json()["error"]["code"] == "invalid_input"

    missing_scope = client.get("/api/v2/files", params={"search": "x", "scope": 999})
    assert missing_scope.status_code == 422
    non_directory_scope = client.get(
        "/api/v2/files", params={"search": "x", "scope": plain_file}
    )
    assert non_directory_scope.status_code == 422

    login(client)  # 限流阈值调整需要管理员。
    patched = client.patch("/api/v2/settings", json={"file_search_rate_limit": 1})
    assert patched.status_code == 200
    searcher = create_user(client, "searcher", password=THIRD_PASSWORD)
    login(client, "searcher", THIRD_PASSWORD)  # 新用户使用干净的搜索限流桶。
    assert int(searcher["id"]) > 0
    first = client.get("/api/v2/files", params={"search": "report"})
    assert first.status_code == 200
    second = client.get("/api/v2/files", params={"search": "report"})
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"
    assert "Retry-After" in second.headers


def test_directory_entries_contract(client: TestClient, settings: Settings) -> None:
    _, user_id = setup_users(client)
    _, file_entry = seed_file(settings, user_id=user_id, index=1)
    _, dir_file = seed_directory(
        settings,
        user_id=user_id,
        index=2,
        entries=[
            ("", "z-dir", "directory", 0),
            ("", "a-file.bin", "file", 3),
            ("z-dir", "deep.bin", "file", 4),
        ],
    )

    response = client.get(f"/api/v2/files/{dir_file}/entries", params={"path": "", "page_size": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["name"] for item in body["items"]] == ["z-dir"]
    assert body["items"][0]["kind"] == "directory"
    assert [(crumb["name"], crumb["path"]) for crumb in body["breadcrumbs"]] == [("dir-2", "")]

    nested = client.get(f"/api/v2/files/{dir_file}/entries", params={"path": "z-dir"})
    assert nested.status_code == 200
    nested_body = nested.json()
    assert nested_body["total"] == 1
    assert nested_body["items"][0]["path"] == "z-dir/deep.bin"
    assert [(crumb["name"], crumb["path"]) for crumb in nested_body["breadcrumbs"]] == [
        ("dir-2", ""),
        ("z-dir", "z-dir"),
    ]

    beyond = client.get(f"/api/v2/files/{dir_file}/entries", params={"path": "", "page": 9})
    assert beyond.status_code == 200
    assert beyond.json()["items"] == []
    assert beyond.json()["total"] == 2

    oversized = client.get(f"/api/v2/files/{dir_file}/entries", params={"page_size": 201})
    assert oversized.status_code == 422
    # 非目录属于输入语义错误：全局映射 INVALID_INPUT -> 422（矩阵 §3.1）。
    assert client.get(f"/api/v2/files/{file_entry}/entries").status_code == 422
    assert client.get("/api/v2/files/999/entries").status_code == 404
    assert (
        client.get(f"/api/v2/files/{dir_file}/entries", params={"path": "a-file.bin"}).status_code
        == 404
    )
    assert (
        client.get(f"/api/v2/files/{dir_file}/entries", params={"path": "../escape"}).status_code
        == 422
    )


def test_download_full_and_partial_ranges(client: TestClient, settings: Settings) -> None:
    _, user_id = setup_users(client)
    payload = bytes(range(256)) * 4  # 1 KiB
    _, file_id = seed_file(
        settings, user_id=user_id, index=1, name="数据 文件.bin", payload=payload
    )
    _, dir_file = seed_directory(
        settings,
        user_id=user_id,
        index=2,
        entries=[("", "docs", "directory", 0), ("docs", "child.bin", "file", 4)],
        payload_path="objects/directory-2/docs/child.bin",
        payload=b"abcd",
    )

    full = client.get(f"/api/v2/files/{file_id}/download")
    assert full.status_code == 200
    assert full.content == payload
    assert full.headers["content-length"] == str(len(payload))
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-disposition"].startswith("attachment;")
    assert "filename*=UTF-8''" in full.headers["content-disposition"]

    partial = client.get(f"/api/v2/files/{file_id}/download", headers={"Range": "bytes=2-5"})
    assert partial.status_code == 206
    assert partial.content == payload[2:6]
    assert partial.headers["content-range"] == f"bytes 2-5/{len(payload)}"
    assert partial.headers["content-length"] == "4"

    open_end = client.get(f"/api/v2/files/{file_id}/download", headers={"Range": "bytes=1000-"})
    assert open_end.status_code == 206
    assert open_end.content == payload[1000:]
    assert open_end.headers["content-range"] == f"bytes 1000-{len(payload) - 1}/{len(payload)}"

    suffix = client.get(f"/api/v2/files/{file_id}/download", headers={"Range": "bytes=-3"})
    assert suffix.status_code == 206
    assert suffix.content == payload[-3:]

    whole = client.get(f"/api/v2/files/{file_id}/download", headers={"Range": "bytes=0-"})
    assert whole.status_code == 206
    assert whole.content == payload
    assert whole.headers["content-length"] == str(len(payload))

    truncated_end = client.get(
        f"/api/v2/files/{file_id}/download", headers={"Range": f"bytes=0-{len(payload) + 50}"}
    )
    assert truncated_end.status_code == 206
    assert truncated_end.content == payload

    for fallback_header in ("items=0-1", "bytes=0-1,5-6"):
        fallback = client.get(
            f"/api/v2/files/{file_id}/download", headers={"Range": fallback_header}
        )
        assert fallback.status_code == 200
        assert fallback.content == payload

    invalid = client.get(f"/api/v2/files/{file_id}/download", headers={"Range": "bytes=99999-"})
    assert invalid.status_code == 416
    assert invalid.json()["error"]["code"] == "range_not_satisfiable"
    assert "content-range" not in invalid.headers

    entry = client.get(
        f"/api/v2/files/{dir_file}/download", params={"path": "docs/child.bin"}
    )
    assert entry.status_code == 200
    assert entry.content == b"abcd"
    assert entry.headers["content-disposition"].startswith("attachment;")

    assert client.get(f"/api/v2/files/{dir_file}/download").status_code == 422
    assert (
        client.get(f"/api/v2/files/{dir_file}/download", params={"path": "docs"}).status_code
        == 422
    )
    assert client.get("/api/v2/files/999/download").status_code == 404


def test_download_requires_browser_session(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    create_admin(client)
    login(client)
    _, user_id = None, int(create_user(client)["id"])
    login(client, "user", OTHER_PASSWORD)
    _, file_id = seed_file(settings, user_id=user_id, index=1, payload=b"secret")

    token_response = client.post("/api/v2/credentials/tokens", json={"name": "t"})
    assert token_response.status_code == 200
    token = token_response.json()["token"]

    anon = TestClient(app)
    assert anon.get(f"/api/v2/files/{file_id}/download").status_code == 401
    rejected = anon.get(
        f"/api/v2/files/{file_id}/download", headers={"Authorization": f"Bearer {token}"}
    )
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "permission_denied"
    assert anon.get("/api/v2/files").status_code == 401


def test_rename_contract(client: TestClient, settings: Settings) -> None:
    _, user_id = setup_users(client)
    _, file_id = seed_file(settings, user_id=user_id, index=1, name="one.bin")
    _, other_file = seed_file(settings, user_id=user_id, index=2, name="two.bin")

    response = client.patch(f"/api/v2/files/{file_id}", json={"name": "renamed.bin"})
    assert response.status_code == 200
    assert response.json()["display_name"] == "renamed.bin"

    conflict = client.patch(f"/api/v2/files/{file_id}", json={"name": "two.bin"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "duplicate_resource"

    invalid = client.patch(f"/api/v2/files/{file_id}", json={"name": "a/b"})
    assert invalid.status_code == 422
    too_long = client.patch(f"/api/v2/files/{file_id}", json={"name": "a" * 201})
    assert too_long.status_code == 422
    assert client.patch("/api/v2/files/999", json={"name": "x"}).status_code == 404
    assert other_file > 0


def test_delete_file_contract(client: TestClient, settings: Settings) -> None:
    _, user_id = setup_users(client)
    _, file_id = seed_file(settings, user_id=user_id, index=1, name="doomed.bin")
    _, blocked_file = seed_file(settings, user_id=user_id, index=2, name="busy.bin")

    # 打包来源占用：最后引用删除被拒绝（409）。
    busy = read_row(
        settings,
        "SELECT content_id FROM user_files WHERE id = :id",
        {"id": blocked_file},
    )
    busy_content = int(busy.content_id)  # type: ignore[attr-defined]
    seed_pack_source(
        settings, user_id=user_id, file_id=blocked_file, content_id=busy_content
    )
    busy_response = client.delete(f"/api/v2/files/{blocked_file}")
    assert busy_response.status_code == 409
    assert busy_response.json()["error"]["code"] == "content_busy"
    assert client.get("/api/v2/files").json()["total"] == 2

    response = client.delete(f"/api/v2/files/{file_id}")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    listing = client.get("/api/v2/files")
    assert listing.json()["total"] == 1
    assert listing.json()["space"]["used_bytes"] == 10
    assert client.delete(f"/api/v2/files/{file_id}").status_code == 404


def test_storage_admin_endpoints(client: TestClient, app: FastAPI, settings: Settings) -> None:
    admin_id, user_id = setup_users(client)
    referenced, _ = seed_file(settings, user_id=user_id, index=1)  # 无物理文件：on_disk False。
    orphan, orphan_file = seed_file(settings, user_id=admin_id, index=2, payload=b"orphan")
    remove_reference(settings, user_id=admin_id, file_id=orphan_file)  # 直接制造活跃孤儿，绕开删除 worker。

    login(client)
    listing = client.get("/api/v2/storage/files", params={"page": 1, "page_size": 10})
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 2
    by_id = {item["content_id"]: item for item in body["items"]}
    assert by_id[referenced]["reference_count"] == 1
    assert by_id[referenced]["on_disk"] is False
    assert by_id[orphan]["reference_count"] == 0
    assert by_id[orphan]["on_disk"] is True
    oversized_page = client.get("/api/v2/storage/files", params={"page_size": 101})
    assert oversized_page.status_code == 422

    orphans = client.get(
        "/api/v2/storage/files", params={"orphans_only": True, "search": "2"}
    )
    assert orphans.status_code == 200
    assert [item["content_id"] for item in orphans.json()["items"]] == [orphan]

    users = client.get(f"/api/v2/storage/files/{referenced}/users")
    assert users.status_code == 200
    assert [row["username"] for row in users.json()["items"]] == ["user"]
    empty = client.get(f"/api/v2/storage/files/{orphan}/users")
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    assert client.get("/api/v2/storage/files/999/users").status_code == 404

    accepted = client.post(
        "/api/v2/storage/files/delete",
        json={"content_ids": [orphan, referenced, 999, orphan]},
    )
    assert accepted.status_code == 202
    result = accepted.json()
    assert result["accepted_count"] == 1
    assert result["rejected_count"] == 2  # 重复 ID 已去重。
    assert [(item["content_id"], item["accepted"]) for item in result["results"]] == [
        (orphan, True),
        (referenced, False),
        (999, False),
    ]
    assert result["results"][1]["error"]["code"] == "content_busy"
    assert result["results"][2]["error"]["code"] == "not_found"

    for empty_ids, expected in (([], 422), ([1] * 1001, 422)):
        response = client.post("/api/v2/storage/files/delete", json={"content_ids": empty_ids})
        assert response.status_code == expected

    for endpoint in ("scan", "repair"):
        unimplemented = client.post(f"/api/v2/storage/{endpoint}")
        assert unimplemented.status_code == 501
        assert unimplemented.json()["error"]["code"] == "not_implemented"

    login(client, "user", OTHER_PASSWORD)
    assert client.get("/api/v2/storage/files").status_code == 403
    assert client.post("/api/v2/storage/scan").status_code == 403
    assert TestClient(app).get("/api/v2/storage/files").status_code == 401


def test_openapi_contains_library_paths_and_no_legacy_alias(app: FastAPI) -> None:
    schema = app.openapi()
    paths = schema["paths"]
    for expected in (
        "/api/v2/files",
        "/api/v2/files/{file_id}",
        "/api/v2/files/{file_id}/entries",
        "/api/v2/files/{file_id}/download",
        "/api/v2/storage/files",
        "/api/v2/storage/files/delete",
        "/api/v2/storage/files/{content_id}/users",
        "/api/v2/storage/scan",
        "/api/v2/storage/repair",
    ):
        assert expected in paths, expected
    scan_responses = paths["/api/v2/storage/scan"]["post"]["responses"]
    assert "501" in scan_responses
    assert scan_responses["501"]["description"] == "暂未实现"
    assert not any(path.startswith("/api/files") for path in paths)


def test_storage_listing_exposes_created_at_and_file_names(client: TestClient, settings) -> None:
    admin = create_admin(client)
    login(client)
    user = create_user(client)
    login(client, "user", OTHER_PASSWORD)
    shared_content, _ = seed_file(settings, user_id=int(user["id"]), index=10, name="shared.bin")
    own_content, _ = seed_file(settings, user_id=int(admin["id"]), index=11, name="admin.bin")

    login(client)
    listing = client.get("/api/v2/storage/files", params={"page": 1, "page_size": 10})
    assert listing.status_code == 200
    by_id = {item["content_id"]: item for item in listing.json()["items"]}
    assert by_id[shared_content]["file_names"] == ["shared.bin"]
    assert by_id[own_content]["file_names"] == ["admin.bin"]
    assert by_id[shared_content]["created_at_ms"] > 0
