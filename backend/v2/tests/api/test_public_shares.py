from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import unquote

from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.requests import Request

from aria2deck.application.library.models import DownloadTarget
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
    dir_code = _make_share(client, dir_file)  # 无密码：凭分享码直接访问（V1 语义）。
    guarded_file = seed_file(settings, user_id=user_id, index=2, payload=b"y")[1]
    guarded_code = _make_share(client, guarded_file, password=PASSWORD)
    anon = TestClient(client.app)

    # 无密码分享：无 token 200；对 Bearer 内容不敏感（token 仅对有密码分享有意义）。
    entries = anon.get(f"/api/s/{dir_code}/entries")
    assert entries.status_code == 200, entries.text
    body = entries.json()
    assert body["total"] == 1
    assert [(item["name"], item["kind"]) for item in body["items"]] == [("docs", "directory")]
    assert [c["name"] for c in body["breadcrumbs"]] == ["dir-2"]
    nested = anon.get(f"/api/s/{dir_code}/entries", params={"path": "docs"})
    assert nested.json()["items"][0]["path"] == "docs/child.bin"
    junk = anon.get(f"/api/s/{dir_code}/entries", headers={"Authorization": "Bearer junk"})
    assert junk.status_code == 200, "无密码分享不校验 token"

    # 有密码分享：无 token 401；query token 一律忽略（含真实值）；正确 Bearer 过门后按业务语义 422（单文件）。
    assert anon.get(f"/api/s/{guarded_code}/entries").status_code == 401
    token = anon.post(f"/api/s/{guarded_code}/verify", json={"password": PASSWORD}).json()[
        "access_token"
    ]
    query = anon.get(f"/api/s/{guarded_code}/entries", params={"token": token})
    assert query.status_code == 401, "query token 必须被忽略"
    through = anon.get(
        f"/api/s/{guarded_code}/entries", headers={"Authorization": f"Bearer {token}"}
    )
    assert through.status_code == 422, "过门后单文件分享按业务语义拒绝浏览"

    # 令牌绑定单一分享码：用于其他有密码分享无效。
    other_guard = _make_share(client, file_id, password=PASSWORD)
    cross = anon.get(f"/api/s/{other_guard}/entries", headers={"Authorization": f"Bearer {token}"})
    assert cross.status_code == 401, "令牌绑定单一分享码"


def test_download_matrix_headers_and_form_channel(client: TestClient, settings) -> None:
    user_id = _setup(client)
    payload = b"0123456789"
    file_id = seed_file(settings, user_id=user_id, index=1, payload=payload)[1]
    dir_file = _seed_directory(settings, user_id, 3)
    code = _make_share(client, file_id, max_downloads=5)  # 非 bytes 回退也是完整实体，计入计数。
    dir_code = _make_share(client, dir_file)
    anon = TestClient(client.app)

    # 无密码分享：凭分享码直接下载（无任何 token）。
    full = anon.get(f"/api/s/{code}/download")
    assert full.status_code == 200
    assert full.content == payload
    assert full.headers["content-length"] == "10"
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-disposition"].startswith("attachment;")

    partial = anon.get(f"/api/s/{code}/download", headers={"Range": "bytes=2-4"})
    assert partial.status_code == 206
    assert partial.content == payload[2:5]
    assert partial.headers["content-range"] == "bytes 2-4/10"

    suffix = anon.get(f"/api/s/{code}/download", headers={"Range": "bytes=-3"})
    assert suffix.status_code == 206 and suffix.content == payload[-3:]

    fallback = anon.get(f"/api/s/{code}/download", headers={"Range": "items=0-1"})
    assert fallback.status_code == 200 and fallback.content == payload

    invalid = anon.get(f"/api/s/{code}/download", headers={"Range": "bytes=99-"})
    assert invalid.status_code == 416
    assert invalid.json()["error"]["code"] == "range_not_satisfiable"

    # 表单通道（x-www-form-urlencoded）同样直接可用（token 可为空）。
    form = anon.post(f"/api/s/{code}/download", data={"token": ""})
    assert form.status_code == 200 and form.content == payload

    # 有密码分享的 token 语义：无 token 401；query token 一律忽略（含真实值）；
    # 错误表单 token 401；正确 token 表单通道 200。
    guarded_id = seed_file(settings, user_id=user_id, index=4, payload=b"g")[1]
    guarded = _make_share(client, guarded_id, password=PASSWORD, max_downloads=3)
    assert anon.get(f"/api/s/{guarded}/download").status_code == 401
    token = anon.post(f"/api/s/{guarded}/verify", json={"password": PASSWORD}).json()[
        "access_token"
    ]
    assert (
        anon.get(f"/api/s/{guarded}/download", params={"token": token}).status_code == 401
    ), "query token 必须被忽略"
    assert anon.post(f"/api/s/{guarded}/download", data={"token": "wrong"}).status_code == 401
    guarded_form = anon.post(f"/api/s/{guarded}/download", data={"token": token})
    assert guarded_form.status_code == 200 and guarded_form.content == b"g"
    assert anon.post(f"/api/s/{code}/download", data={}).status_code == 422, "缺 Content-Type 按非表单拒绝"

    # 目录内条目下载 + 目录整体拒绝（无密码分享直接访问）。
    entry = anon.get(
        f"/api/s/{dir_code}/download",
        params={"path": "docs/child.bin"},
        headers={"Range": "bytes=1-2"},
    )
    assert entry.status_code == 206 and entry.content == b"bc"
    assert anon.get(f"/api/s/{dir_code}/download").status_code == 422

    # 计数：完整（200 全量 + 全文件 206 + 表单）逐次计入，达上限后 410。
    whole = anon.get(f"/api/s/{code}/download", headers={"Range": "bytes=0-"})
    assert whole.status_code == 206 and whole.content == payload
    third = anon.get(f"/api/s/{code}/download")
    assert third.status_code == 200
    exhausted = anon.get(f"/api/s/{code}/download")
    assert exhausted.status_code == 410
    assert exhausted.json()["error"]["code"] == "share_exhausted"


def test_form_channel_content_type_size_and_rate_consumption(
    client: TestClient, settings
) -> None:
    user_id = _setup(client)
    file_id = seed_file(settings, user_id=user_id, index=1, payload=b"form")[1]
    code = _make_share(client, file_id, max_downloads=10)

    wrong_type = TestClient(client.app).post(
        f"/api/s/{code}/download", json={"token": "x"}
    )
    assert wrong_type.status_code == 422
    assert wrong_type.json()["error"]["code"] == "invalid_input"

    oversized = TestClient(client.app).post(
        f"/api/s/{code}/download", data={"token": "", "pad": "x" * (64 * 1024 + 1)}
    )
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "task_too_large"

    login(client)
    client.patch("/api/v2/settings", json={"share_access_rate_limit": 1})
    login(client, "user", OTHER_PASSWORD)
    limited = TestClient(client.app, client=("198.51.100.7", 2222))
    # 限流在任何语义之前：第一次成功（无密码分享直接下载），第二次 429。
    assert limited.post(f"/api/s/{code}/download", data={"token": ""}).status_code == 200
    throttled = limited.post(f"/api/s/{code}/download", data={"token": ""})
    assert throttled.status_code == 429
    assert "Retry-After" in throttled.headers


def test_public_rate_limit_covers_every_share_route(client: TestClient, settings) -> None:
    user_id = _setup(client)
    file_id = seed_file(settings, user_id=user_id, index=1, payload=b"payload")[1]
    directory_id = _seed_directory(settings, user_id, 20)
    file_code = _make_share(client, file_id)
    guarded_code = _make_share(client, file_id, password=PASSWORD)
    directory_code = _make_share(client, directory_id)
    login(client)
    assert (
        client.patch("/api/v2/settings", json={"public_api_rate_limit": 1}).status_code
        == 200
    )

    requests = (
        lambda anon: anon.get(f"/api/s/{file_code}/info"),
        lambda anon: anon.post(f"/api/s/{guarded_code}/verify", json={"password": PASSWORD}),
        lambda anon: anon.get(f"/api/s/{directory_code}/entries"),
        lambda anon: anon.get(f"/api/s/{file_code}/download"),
        lambda anon: anon.post(f"/api/s/{file_code}/download", data={"token": ""}),
    )
    for index, request in enumerate(requests):
        anon = TestClient(client.app, client=(f"198.51.100.{30 + index}", 4321))
        first = request(anon)
        assert first.status_code == 200, first.text
        limited = request(anon)
        assert limited.status_code == 429, limited.text
        assert limited.json()["error"]["code"] == "rate_limited"
        assert "Retry-After" in limited.headers


def test_info_and_entries_are_rate_limited_by_ip(client: TestClient, settings) -> None:
    user_id = _setup(client)
    dir_file = _seed_directory(settings, user_id, 9)
    code = _make_share(client, dir_file)
    login(client)
    client.patch("/api/v2/settings", json={"share_access_rate_limit": 1})
    login(client, "user", OTHER_PASSWORD)
    limited = TestClient(client.app, client=("198.51.100.8", 3333))

    assert limited.get(f"/api/s/{code}/info").status_code == 200
    assert limited.get(f"/api/s/{code}/info").status_code == 429

    entries_client = TestClient(client.app, client=("198.51.100.9", 3334))
    assert entries_client.get(f"/api/s/{code}/entries").status_code == 200
    assert entries_client.get(f"/api/s/{code}/entries").status_code == 429


def _filename_star(disposition: str) -> str:
    marker = "filename*=UTF-8''"
    assert marker in disposition, disposition
    return unquote(disposition.split(marker, 1)[1].strip().strip('"'))


def test_share_info_and_download_name_follow_rename(
    client: TestClient, settings
) -> None:
    user_id = _setup(client)
    payload = b"0123456789"
    file_id = seed_file(settings, user_id=user_id, index=7, name="old-name.bin", payload=payload)[1]
    code = _make_share(client, file_id)

    anon = TestClient(client.app)
    info_before = anon.get(f"/api/s/{code}/info")
    assert info_before.status_code == 200
    assert info_before.json()["file_name"] == "old-name.bin"

    renamed = client.patch(f"/api/v2/files/{file_id}", json={"name": "mid-name.bin"})
    assert renamed.status_code == 200
    renamed_again = client.patch(f"/api/v2/files/{file_id}", json={"name": "new-name.bin"})
    assert renamed_again.status_code == 200

    info = anon.get(f"/api/s/{code}/info")
    assert info.status_code == 200
    # 修复前此处返回创建时快照 "old-name.bin"，与下载响应头的新名矛盾。
    assert info.json()["file_name"] == "new-name.bin"

    download = anon.get(f"/api/s/{code}/download")
    assert download.status_code == 200
    assert _filename_star(download.headers["content-disposition"]) == info.json()["file_name"]


def test_directory_share_breadcrumb_root_follows_rename(
    client: TestClient, settings
) -> None:
    user_id = _setup(client)
    dir_file = _seed_directory(settings, user_id, 8)
    code = _make_share(client, dir_file)
    anon = TestClient(client.app)

    response = client.patch(f"/api/v2/files/{dir_file}", json={"name": "bundle-v2"})
    assert response.status_code == 200

    info = anon.get(f"/api/s/{code}/info")
    assert info.status_code == 200
    assert info.json()["file_name"] == "bundle-v2"  # 修复前为 "dir-8"

    entries = anon.get(f"/api/s/{code}/entries")
    assert entries.status_code == 200
    body = entries.json()
    assert body["breadcrumbs"][0]["name"] == "bundle-v2"  # 修复前为 "dir-8"
    # 目录条目名来自内容层，不受目录改名影响。
    assert [item["name"] for item in body["items"]] == ["docs"]


def test_deleted_file_share_keeps_snapshot_shell_and_blocks_access(
    client: TestClient, settings
) -> None:
    user_id = _setup(client)
    file_id = seed_file(settings, user_id=user_id, index=9, name="gone.bin", payload=b"x")[1]
    code = _make_share(client, file_id)

    deleted = client.delete(f"/api/v2/files/{file_id}")
    assert deleted.status_code in (200, 202, 204)

    anon = TestClient(client.app)
    info = anon.get(f"/api/s/{code}/info")
    assert info.status_code == 410
    assert info.json()["error"]["code"] == "share_unavailable"
    download = anon.get(f"/api/s/{code}/download")
    assert download.status_code == 410

    listing = client.get("/api/v2/shares")
    assert listing.status_code == 200
    item = next(share for share in listing.json()["items"] if share["code"] == code)
    # 拥有者列表保留创建时快照外壳与失效状态。
    assert item["file_name"] == "gone.bin"
    assert item["status"] == "unavailable"


class _ShareDisconnectLease:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def release(self) -> None:
        self._calls.append("connection")


class _ShareDisconnectLimiter:
    def __init__(self, lease: _ShareDisconnectLease) -> None:
        self._lease = lease

    def check_public(self, ip: str) -> None:  # noqa: ARG002  # 假件不校验
        return None

    def check_share_access(self, ip: str) -> None:  # noqa: ARG002  # 假件不校验
        return None

    async def acquire_anonymous(self, *, client_ip: str, file_key: str) -> _ShareDisconnectLease:  # noqa: ARG002, E501
        return self._lease


class _ShareDisconnectService:
    """直连 download_share 的假服务：download 返回真实 PublicShareDownload（携带 target）。"""

    def __init__(self, calls: list[str], chunks: list[bytes]) -> None:
        self._calls = calls
        self._chunks = chunks

    async def check_access(self, code: str, token: str | None):  # noqa: ARG002
        from types import SimpleNamespace

        return SimpleNamespace(content_key="content-key")

    async def download(
        self, code: str, token: str | None, path: str | None, range_header: str | None  # noqa: ARG002, E501
    ):
        from pathlib import Path

        from aria2deck.application.shares.models import PublicShareDownload
        from aria2deck.domain.shares import DownloadDecision

        decision = DownloadDecision(start=0, end=sum(len(c) for c in self._chunks) - 1, status_code=200, count_download=True)
        physical = Path("/nonexistent/share-disconnect.bin")
        return PublicShareDownload(
            decision=decision,
            content_id=7,
            storage_path="objects/share-disconnect.bin",
            entry_relative_path=None,
            physical_path=physical,
            display_name="share-disconnect.bin",
            total_size=decision.content_length,
            target=DownloadTarget(
                content_id=7,
                storage_path="objects/share-disconnect.bin",
                entry_relative_path=None,
                physical_path=physical,
                display_name="share-disconnect.bin",
                total_size=decision.content_length,
                range_spec=None,
            ),
        )

    def iter_chunks(self, target):  # noqa: ARG002
        async def _generate():
            for chunk in self._chunks:
                yield chunk

        return _generate()

    async def release(self, result) -> None:  # noqa: ARG002
        self._calls.append("content")


def _share_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/s/code/download",
            "client": ("127.0.0.1", 50000),
            "headers": [],
        }
    )


async def test_share_download_releases_read_lease_when_client_disconnects_mid_stream() -> None:
    """回归（变异测试发现）：分享下载路径的读租约释放此前零测试保护。

    把 public_shares.py 的 releases= 改回只放连接租约，全量测试仍绿——
    该断言锁住分享路径（GET 入口）与文件路径同级的释放契约。
    """
    import pytest
    from starlette.requests import ClientDisconnect

    from aria2deck.api.public_shares import download_share

    calls: list[str] = []
    service = _ShareDisconnectService(calls, [b"chunk-1", b"chunk-2", b"chunk-3"])
    lease = _ShareDisconnectLease(calls)
    response = await download_share(
        request=_share_request(),
        code="code",
        path=None,
        range_header=None,
        authorization=None,
        limiter=_ShareDisconnectLimiter(lease),
        service=service,
        connections=_ShareDisconnectLimiter(lease),
    )

    bodies = 0

    async def send(message: dict) -> None:
        nonlocal bodies
        if message["type"] == "http.response.body" and message.get("body"):
            bodies += 1
            if bodies == 2:
                raise ClientDisconnect()

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnect):
        await response({"type": "http"}, receive, send)

    assert set(calls) == {"content", "connection"}


async def test_share_download_releases_read_lease_on_normal_completion() -> None:
    from aria2deck.api.public_shares import download_share

    calls: list[str] = []
    service = _ShareDisconnectService(calls, [b"chunk-1"])
    lease = _ShareDisconnectLease(calls)
    response = await download_share(
        request=_share_request(),
        code="code",
        path=None,
        range_header=None,
        authorization=None,
        limiter=_ShareDisconnectLimiter(lease),
        service=service,
        connections=_ShareDisconnectLimiter(lease),
    )
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    receive_calls = 0

    async def receive() -> dict:
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls >= 2:
            return {"type": "http.disconnect"}
        return {"type": "http.request"}

    await response({"type": "http"}, receive, send)

    assert set(calls) == {"content", "connection"}


async def test_share_form_download_releases_read_lease_when_client_disconnects_mid_stream() -> None:
    """POST 表单入口（token 只经表单/头进入）与 GET 入口共用 _streaming，同样要锁住释放。"""
    import pytest
    from starlette.requests import ClientDisconnect

    from aria2deck.api.public_shares import download_share_by_form

    calls: list[str] = []
    service = _ShareDisconnectService(calls, [b"chunk-1", b"chunk-2", b"chunk-3"])
    lease = _ShareDisconnectLease(calls)
    form_body = b"token="

    async def receive() -> dict:
        return {"type": "http.request", "body": form_body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/s/code/download",
            "client": ("127.0.0.1", 50000),
            "headers": [
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"content-length", str(len(form_body)).encode()),
            ],
        },
        receive,
    )
    response = await download_share_by_form(
        request=request,
        code="code",
        range_header=None,
        limiter=_ShareDisconnectLimiter(lease),
        service=service,
        connections=_ShareDisconnectLimiter(lease),
    )

    bodies = 0

    async def send(message: dict) -> None:
        nonlocal bodies
        if message["type"] == "http.response.body" and message.get("body"):
            bodies += 1
            if bodies == 2:
                raise ClientDisconnect()

    async def receive_disconnect() -> dict:
        return {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnect):
        await response({"type": "http"}, receive_disconnect, send)

    assert set(calls) == {"content", "connection"}


async def test_share_release_is_exactly_once_for_repeated_calls() -> None:
    """评审发现：_as_target 每次新建 target 时 double-release 会吃掉他人租约。

    PublicShareDownload 携带同一个 target，release 透传同一对象，
    由 DownloadTarget.released（真实语义见 test_library_services 幂等用例）挡第二次。
    """
    from aria2deck.application.shares.access import PublicShareService
    from aria2deck.application.shares.models import PublicShareDownload
    from aria2deck.domain.shares import DownloadDecision

    calls: list[str] = []
    seen: list[DownloadTarget] = []

    class _RecordingStreamer:
        """按 LibraryDownloadService.release 的真实契约记账：released 标志恰好一次。"""

        async def release(self, target: DownloadTarget) -> None:
            seen.append(target)
            if target.released:
                return
            target.released = True
            calls.append("content")

    service = PublicShareService.__new__(PublicShareService)  # 仅借用 release 方法
    service.streamer = _RecordingStreamer()  # type: ignore[attr-defined]

    decision = DownloadDecision(start=0, end=0, status_code=200, count_download=True)
    result = PublicShareDownload(
        decision=decision,
        content_id=7,
        storage_path="objects/share-once.bin",
        entry_relative_path=None,
        physical_path=Path("/nonexistent/share-once.bin"),
        display_name="share-once.bin",
        total_size=1,
        target=DownloadTarget(
            content_id=7,
            storage_path="objects/share-once.bin",
            entry_relative_path=None,
            physical_path=Path("/nonexistent/share-once.bin"),
            display_name="share-once.bin",
            total_size=1,
            range_spec=None,
        ),
    )

    await service.release(result)
    await service.release(result)
    assert seen[0] is seen[1]  # 两次释放透传同一个 target，幂等标志才能生效
    assert calls == ["content"]  # 第二次调用被 released 标志挡下，不重复扣租约
