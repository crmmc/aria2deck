from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text

from aria2deck.application.library.download import LibraryDownloadService
from aria2deck.application.library.leases import ReadLeaseManager
from aria2deck.application.shares.access import PublicShareService
from aria2deck.application.shares.manage import ShareManageService
from aria2deck.application.shares.models import ShareRecord
from aria2deck.application.shares.tokens import ShareAccessTokenStore
from aria2deck.domain.content import ContentKind
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.shares import ShareStatus
from aria2deck.infrastructure.content_store import DownloadContentStore
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.content import ContentRepository
from aria2deck.infrastructure.sqlite.repositories.shares import ShareRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

from .test_library_services import FakeClock, _digest, _setup

SHARE_PASSWORD = "测试分享口令"  # noqa: S105  # 测试夹具口令，非凭据
SHARE_WRONG = "错误口令"  # noqa: S106  # 测试夹具口令


def _write(root: Path, storage_path: str, payload: bytes) -> None:
    target = root.joinpath(*storage_path.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


async def _user(database: SQLiteDatabase, username: str = "owner") -> int:
    from aria2deck.infrastructure.sqlite.repositories.accounts import AccountRepository

    async with SQLiteUnitOfWork(database) as uow:
        return await AccountRepository(uow.connection).create_user(
            username=username,
            password_hash=_digest(1),
            is_admin=True,
            quota_bytes=10_000,
            now_ms=1,
        )


async def _seed_file(
    database: SQLiteDatabase, root: Path, user_id: int, index: int, payload: bytes
) -> tuple[int, int]:
    _write(root, f"objects/file-{index}", payload)
    async with SQLiteUnitOfWork(database) as uow:
        repository = ContentRepository(uow.connection)
        content_id = await repository.create_object(
            kind="file",
            digest=_digest(index),
            size=len(payload),
            storage_path=f"objects/file-{index}",
            now_ms=1,
        )
        file_id = await repository.add_user_file(
            user_id=user_id, content_id=content_id, display_name=f"file-{index}.bin", now_ms=1
        )
    return content_id, file_id


async def _seed_directory(
    database: SQLiteDatabase, root: Path, user_id: int, index: int
) -> tuple[int, int]:
    _write(root, f"objects/directory-{index}/docs/child.bin", b"abcd")
    async with SQLiteUnitOfWork(database) as uow:
        repository = ContentRepository(uow.connection)
        content_id = await repository.create_object(
            kind="directory",
            digest=_digest(index),
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
        file_id = await repository.add_user_file(
            user_id=user_id, content_id=content_id, display_name=f"dir-{index}", now_ms=1
        )
    return content_id, file_id


class ShareWorld:
    def __init__(self, database: SQLiteDatabase, clock: FakeClock, root: Path) -> None:
        store = DownloadContentStore(root)
        self.database = database
        self.clock = clock
        self.root = root
        self.store = store
        self.tokens = ShareAccessTokenStore(clock_ms=clock)
        self.leases = ReadLeaseManager(
            uow_factory=lambda: SQLiteUnitOfWork(database),
            repository_factory=lambda uow: ContentRepository(uow.connection),
        )
        self.manage = ShareManageService(
            uow_factory=lambda: SQLiteUnitOfWork(database),
            repository_factory=lambda uow: ShareRepository(uow.connection),
            clock_ms=clock,
        )
        streamer = LibraryDownloadService(
            uow_factory=lambda: SQLiteUnitOfWork(database),
            repository_factory=lambda uow: ContentRepository(uow.connection),
            leases=self.leases,
            content_store=store,
        )
        self.public = PublicShareService(
            uow_factory=lambda: SQLiteUnitOfWork(database),
            repository_factory=lambda uow: ShareRepository(uow.connection),
            content_repository_factory=lambda uow: ContentRepository(uow.connection),
            tokens=self.tokens,
            leases=self.leases,
            content_paths=store,
            streamer=streamer,
            clock_ms=clock,
        )


async def _world(tmp_path: Path) -> tuple[ShareWorld, int]:
    database, clock = await _setup(tmp_path)
    root = tmp_path / "downloads"
    root.mkdir()
    user_id = await _user(database)
    return ShareWorld(database, clock, root), user_id


def test_token_store_lifecycle_binding_and_capacity() -> None:
    clock = FakeClock(1_000)
    store = ShareAccessTokenStore(clock_ms=clock, capacity=2, ttl_seconds=100)

    token_a, expires_in = store.issue("code-a")
    assert expires_in == 100
    assert store.verify("code-a", token_a) is True
    assert store.verify("code-b", token_a) is False, "令牌必须绑定单一分享码"

    clock.advance(100_000)
    assert store.verify("code-a", token_a) is False, "过期令牌必须失效"

    first, _ = store.issue("code-a")
    clock.advance(50)
    second, _ = store.issue("code-a")
    third, _ = store.issue("code-a")  # 容量 2：驱逐最旧。
    assert store.verify("code-a", first) is False
    assert store.verify("code-a", second) is True
    assert store.verify("code-a", third) is True

    with pytest.raises(DomainError):
        ShareAccessTokenStore(clock_ms=clock, capacity=0)


@pytest.mark.parametrize(
    "expires,max_downloads,password",
    [(0, None, None), (30 * 86400 + 1, None, None), (None, 0, None), (None, 10001, None), (None, None, ""), (None, None, "x" * 101)],
)
async def test_create_rejects_invalid_options(
    tmp_path: Path, expires: int | None, max_downloads: int | None, password: str | None
) -> None:
    world, user_id = await _world(tmp_path)
    _, file_id = await _seed_file(world.database, world.root, user_id, 1, b"data")
    with pytest.raises(DomainError) as caught:
        await world.manage.create(
            user_id,
            user_file_id=file_id,
            expires_in_seconds=expires,
            max_downloads=max_downloads,
            password=password,
        )
    assert caught.value.code is ErrorCode.INVALID_INPUT


async def test_create_concurrency_never_exceeds_capacity(tmp_path: Path) -> None:
    world, user_id = await _world(tmp_path)
    _, file_id = await _seed_file(world.database, world.root, user_id, 1, b"data")

    async def attempt(index: int) -> bool:
        try:
            await world.manage.create(
                user_id,
                user_file_id=file_id,
                expires_in_seconds=3600,
                max_downloads=None,
                password=None,
            )
            return True
        except DomainError as error:
            assert error.code is ErrorCode.SHARE_LIMIT_EXCEEDED
            return False

    results = await asyncio.gather(*(attempt(index) for index in range(11)))
    assert results.count(True) == 10, "并发创建不得超过每文件 10 个有效分享"


async def test_create_ownership_and_expiry_semantics(tmp_path: Path) -> None:
    world, user_id = await _world(tmp_path)
    other = await _user(world.database, "other")
    _, file_id = await _seed_file(world.database, world.root, user_id, 1, b"data")
    with pytest.raises(DomainError) as caught:
        await world.manage.create(
            other, user_file_id=file_id, expires_in_seconds=None, max_downloads=None, password=None
        )
    assert caught.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(DomainError) as caught:
        await world.manage.create(
            user_id, user_file_id=999, expires_in_seconds=None, max_downloads=None, password=None
        )
    assert caught.value.code is ErrorCode.NOT_FOUND

    created = await world.manage.create(
        user_id,
        user_file_id=file_id,
        expires_in_seconds=3600,
        max_downloads=5,
        password=SHARE_PASSWORD,
    )
    assert created.code
    assert created.view.status is ShareStatus.ACTIVE
    assert created.view.has_password is True
    assert created.view.expires_at_ms == world.clock() + 3_600_000

    world.clock.advance(3_600_000)
    listed, total = await world.manage.list_shares(
        user_id, status=ShareStatus.EXPIRED, page=1, page_size=10
    )
    assert total == 1
    assert listed[0].status is ShareStatus.EXPIRED
    active, active_total = await world.manage.list_shares(
        user_id, status=ShareStatus.ACTIVE, page=1, page_size=10
    )
    assert (active, active_total) == ([], 0)


async def test_revoke_delete_and_revoke_all_semantics(tmp_path: Path) -> None:
    world, user_id = await _world(tmp_path)
    other = await _user(world.database, "other")
    _, first = await _seed_file(world.database, world.root, user_id, 1, b"a")
    _, second = await _seed_file(world.database, world.root, user_id, 2, b"bb")
    _, foreign = await _seed_file(world.database, world.root, other, 3, b"ccc")
    one = await world.manage.create(user_id, user_file_id=first, expires_in_seconds=None, max_downloads=None, password=None)
    two = await world.manage.create(user_id, user_file_id=second, expires_in_seconds=None, max_downloads=None, password=None)
    foreign_share = await world.manage.create(other, user_file_id=foreign, expires_in_seconds=None, max_downloads=None, password=None)
    share_ids = [one.view.id, two.view.id]

    revoked = await world.manage.revoke(user_id, [share_ids[0], share_ids[0], 999, foreign_share.view.id])
    assert [(item.ok, item.already_revoked, item.error_code) for item in revoked] == [
        (True, False, None),
        (True, True, None),  # 重复撤销：幂等成功并标记。
        (False, False, "not_found"),
        (False, False, "not_found"),
    ]

    deleted = await world.manage.delete(user_id, [share_ids[0], 999])
    assert [(item.ok, item.error_code) for item in deleted] == [(True, None), (False, "not_found")]
    async with SQLiteUnitOfWork(world.database) as uow:
        gone = await ShareRepository(uow.connection).find_share_by_code(one.code)
    assert gone is None, "删除后公开链路必须 404"

    count = await world.manage.revoke_all(user_id)
    assert count == 1  # 仅剩 two；foreign 属他人。
    async with SQLiteUnitOfWork(world.database) as uow:
        record = await ShareRepository(uow.connection).get_share(two.view.id)
        foreign_record = await ShareRepository(uow.connection).get_share(foreign_share.view.id)
    assert record.revoked_at_ms is not None
    assert foreign_record.revoked_at_ms is None


async def test_public_info_status_matrix(tmp_path: Path) -> None:
    world, user_id = await _world(tmp_path)
    _, file_id = await _seed_file(world.database, world.root, user_id, 1, b"payload")
    active = await world.manage.create(user_id, user_file_id=file_id, expires_in_seconds=3600, max_downloads=2, password=SHARE_PASSWORD)
    expired = await world.manage.create(user_id, user_file_id=file_id, expires_in_seconds=1, max_downloads=None, password=None)
    to_revoke = await world.manage.create(user_id, user_file_id=file_id, expires_in_seconds=3600, max_downloads=None, password=None)

    info = await world.public.info(active.code)
    assert (info.file_name, info.file_size, info.file_kind, info.has_password) == (
        "file-1.bin", 7, "file", True,
    )
    assert info.max_downloads == 2 and info.download_count == 0

    with pytest.raises(DomainError) as caught:
        await world.public.info("missing-code")
    assert caught.value.code is ErrorCode.NOT_FOUND

    await world.manage.revoke(user_id, [to_revoke.view.id])
    with pytest.raises(DomainError) as caught:
        await world.public.info(to_revoke.code)
    assert caught.value.code is ErrorCode.SHARE_REVOKED

    world.clock.advance(2_000)
    with pytest.raises(DomainError) as caught:
        await world.public.info(expired.code)
    assert caught.value.code is ErrorCode.SHARE_EXPIRED

    async with SQLiteUnitOfWork(world.database) as uow:
        await uow.connection.execute(
            text("UPDATE share_links SET download_count = 2 WHERE code = :c"),
            {"c": active.code},
        )
    with pytest.raises(DomainError) as caught:
        await world.public.info(active.code)
    assert caught.value.code is ErrorCode.SHARE_EXHAUSTED

    fresh = await world.manage.create(user_id, user_file_id=file_id, expires_in_seconds=None, max_downloads=1, password=None)
    async with SQLiteUnitOfWork(world.database) as uow:
        await uow.connection.execute(
            text("UPDATE share_links SET download_count = 1 WHERE code = :c"),
            {"c": fresh.code},
        )
    with pytest.raises(DomainError) as caught:
        await world.public.info(fresh.code)
    assert caught.value.code is ErrorCode.SHARE_EXHAUSTED

    gone = await world.manage.create(user_id, user_file_id=file_id, expires_in_seconds=None, max_downloads=None, password=None)
    async with SQLiteUnitOfWork(world.database) as uow:
        await uow.connection.execute(
            text("DELETE FROM user_files WHERE id = :id"), {"id": file_id},
        )
    with pytest.raises(DomainError) as caught:
        await world.public.info(gone.code)
    assert caught.value.code is ErrorCode.SHARE_UNAVAILABLE


async def test_verify_password_semantics(tmp_path: Path) -> None:
    world, user_id = await _world(tmp_path)
    _, file_id = await _seed_file(world.database, world.root, user_id, 1, b"data")
    plain = await world.manage.create(user_id, user_file_id=file_id, expires_in_seconds=None, max_downloads=None, password=None)
    guarded = await world.manage.create(user_id, user_file_id=file_id, expires_in_seconds=3600, max_downloads=None, password=SHARE_PASSWORD)

    with pytest.raises(DomainError) as caught:
        await world.public.verify(plain.code, "whatever")
    assert caught.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(DomainError) as caught:
        await world.public.verify(guarded.code, "wrong")
    assert caught.value.code is ErrorCode.INVALID_CREDENTIALS

    token, expires_in = await world.public.verify(guarded.code, SHARE_PASSWORD)
    assert expires_in == 1800
    assert world.tokens.verify(guarded.code, token) is True

    world.clock.advance(3_600_000)
    with pytest.raises(DomainError) as caught:
        await world.public.verify(guarded.code, SHARE_PASSWORD)
    assert caught.value.code is ErrorCode.SHARE_EXPIRED


async def test_browse_requires_token_and_directory(tmp_path: Path) -> None:
    world, user_id = await _world(tmp_path)
    _, file_id = await _seed_file(world.database, world.root, user_id, 1, b"data")
    _, dir_file = await _seed_directory(world.database, world.root, user_id, 2)
    # 无密码分享：凭分享码直接浏览（V1 语义，token 被忽略）。
    open_shared = await world.manage.create(user_id, user_file_id=dir_file, expires_in_seconds=None, max_downloads=None, password=None)
    entries, breadcrumbs, total = await world.public.browse(open_shared.code, "", "", 1, 10)
    assert total == 1
    assert [(entry.name, entry.kind) for entry in entries] == [("docs", ContentKind.DIRECTORY)]
    assert [crumb.name for crumb in breadcrumbs] == ["dir-2"]
    nested, _, nested_total = await world.public.browse(open_shared.code, "", "docs", 1, 10)
    assert nested_total == 1
    assert nested[0].path == "docs/child.bin"
    with pytest.raises(DomainError) as caught:
        await world.public.browse(open_shared.code, "", "docs/child.bin", 1, 10)
    assert caught.value.code is ErrorCode.NOT_FOUND

    # 有密码分享：token 门生效（错误/跨分享/缺失一律 401；单文件浏览 422）。
    guarded = await world.manage.create(user_id=user_id, user_file_id=file_id, expires_in_seconds=None, max_downloads=None, password=SHARE_PASSWORD)
    guarded_dir = await world.manage.create(user_id=user_id, user_file_id=dir_file, expires_in_seconds=None, max_downloads=None, password=SHARE_PASSWORD)
    with pytest.raises(DomainError) as caught:
        await world.public.browse(guarded_dir.code, "", "", 1, 10)
    assert caught.value.code is ErrorCode.UNAUTHENTICATED
    with pytest.raises(DomainError) as caught:
        await world.public.browse(guarded_dir.code, "bad-token", "", 1, 10)
    assert caught.value.code is ErrorCode.UNAUTHENTICATED
    other_token, _ = world.tokens.issue(guarded.code)
    with pytest.raises(DomainError) as caught:
        await world.public.browse(guarded_dir.code, other_token, "", 1, 10)
    assert caught.value.code is ErrorCode.UNAUTHENTICATED
    token, _ = await world.public.verify(guarded.code, SHARE_PASSWORD)
    with pytest.raises(DomainError) as caught:
        await world.public.browse(guarded.code, token, "", 1, 10)
    assert caught.value.code is ErrorCode.INVALID_INPUT
    dir_token, _ = await world.public.verify(guarded_dir.code, SHARE_PASSWORD)
    assert await world.public.browse(guarded_dir.code, dir_token, "", 1, 10) is not None


async def test_download_decision_matrix_and_lease(tmp_path: Path) -> None:
    world, user_id = await _world(tmp_path)
    payload = b"0123456789"
    content_id, file_id = await _seed_file(world.database, world.root, user_id, 1, payload)
    shared = await world.manage.create(user_id, user_file_id=file_id, expires_in_seconds=None, max_downloads=3, password=None)
    token, _ = world.tokens.issue(shared.code)

    full = await world.public.download(shared.code, token, None, None)
    assert (full.decision.status_code, full.decision.count_download) == (200, True)
    body = b"".join([chunk async for chunk in world.public.iter_chunks(full)])
    assert body == payload
    await world.public.release(full)
    assert world.leases.in_flight(content_id) == 0

    whole = await world.public.download(shared.code, token, None, "bytes=0-")
    assert (whole.decision.status_code, whole.decision.count_download) == (206, True)
    partial = await world.public.download(shared.code, token, None, "bytes=2-4")
    assert (partial.decision.status_code, partial.decision.count_download) == (206, False)
    window = b"".join([chunk async for chunk in world.public.iter_chunks(partial)])
    assert window == payload[2:5]
    await world.public.release(partial)

    with pytest.raises(DomainError) as caught:
        await world.public.download(shared.code, token, None, "bytes=99-")
    assert caught.value.code is ErrorCode.RANGE_NOT_SATISFIABLE

    async with SQLiteUnitOfWork(world.database) as uow:
        count = (
            await uow.connection.execute(
                text("SELECT download_count FROM share_links WHERE code = :c"),
                {"c": shared.code},
            )
        ).scalar_one()
    assert count == 2, "两次完整实体各计 1，部分区间不计"

    third = await world.public.download(shared.code, token, None, None)  # 第 3 次达上限。
    await world.public.release(third)
    with pytest.raises(DomainError) as caught:
        await world.public.download(shared.code, token, None, None)
    assert caught.value.code is ErrorCode.SHARE_EXHAUSTED
    assert world.leases.in_flight(content_id) == 0, "计数失败不得泄漏读租约"


@pytest.mark.parametrize("range_header", [None, "bytes=0-0"])
@pytest.mark.parametrize(
    "state,expected_code",
    [
        ("revoked", ErrorCode.SHARE_REVOKED),
        ("expired", ErrorCode.SHARE_EXPIRED),
        ("exhausted", ErrorCode.SHARE_EXHAUSTED),
        ("file_deleted", ErrorCode.SHARE_UNAVAILABLE),
    ],
)
async def test_download_rechecks_share_after_lease_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    expected_code: ErrorCode,
    range_header: str | None,
) -> None:
    world, user_id = await _world(tmp_path)
    content_id, file_id = await _seed_file(world.database, world.root, user_id, 1, b"payload")
    shared = await world.manage.create(
        user_id,
        user_file_id=file_id,
        expires_in_seconds=1 if state == "expired" else None,
        max_downloads=1,
        password=None,
    )
    token, _ = world.tokens.issue(shared.code)
    original_acquire = world.leases.acquire

    async def acquire_then_invalidate(leased_content_id: int) -> None:
        await original_acquire(leased_content_id)
        if state == "expired":
            world.clock.advance(1_000)
        elif state == "revoked":
            await world.manage.revoke(user_id, [shared.view.id])
        else:
            async with SQLiteUnitOfWork(world.database) as uow:
                if state == "exhausted":
                    await uow.connection.execute(
                        text("UPDATE share_links SET download_count = 1 WHERE id = :id"),
                        {"id": shared.view.id},
                    )
                else:
                    await uow.connection.execute(
                        text("DELETE FROM user_files WHERE id = :id"), {"id": file_id}
                    )

    monkeypatch.setattr(world.leases, "acquire", acquire_then_invalidate)
    with pytest.raises(DomainError) as caught:
        await world.public.download(shared.code, token, None, range_header)
    assert caught.value.code is expected_code
    assert world.leases.in_flight(content_id) == 0
    async with SQLiteUnitOfWork(world.database) as uow:
        current = await ShareRepository(uow.connection).get_share(shared.view.id)
    assert current.download_count == (1 if state == "exhausted" else 0)


async def test_count_retry_rechecks_revoked_share_and_releases_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, user_id = await _world(tmp_path)
    content_id, file_id = await _seed_file(world.database, world.root, user_id, 1, b"payload")
    shared = await world.manage.create(
        user_id, user_file_id=file_id, expires_in_seconds=None, max_downloads=3, password=None
    )
    token, _ = world.tokens.issue(shared.code)
    original_acquire = world.leases.acquire
    original_find = world.public._find_share

    async def acquire_then_create_count_conflict(leased_content_id: int) -> None:
        await original_acquire(leased_content_id)
        async with SQLiteUnitOfWork(world.database) as uow:
            await uow.connection.execute(
                text("UPDATE share_links SET download_count = 1 WHERE id = :id"),
                {"id": shared.view.id},
            )

    async def find_after_revoke(code: str) -> ShareRecord:
        await world.manage.revoke(user_id, [shared.view.id])
        return await original_find(code)

    monkeypatch.setattr(world.leases, "acquire", acquire_then_create_count_conflict)
    monkeypatch.setattr(world.public, "_find_share", find_after_revoke)
    with pytest.raises(DomainError) as caught:
        await world.public.download(shared.code, token, None, None)
    assert caught.value.code is ErrorCode.SHARE_REVOKED
    assert world.leases.in_flight(content_id) == 0
    async with SQLiteUnitOfWork(world.database) as uow:
        current = await ShareRepository(uow.connection).get_share(shared.view.id)
    assert current.download_count == 1


async def test_count_retry_uses_fresh_active_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, user_id = await _world(tmp_path)
    content_id, file_id = await _seed_file(world.database, world.root, user_id, 1, b"payload")
    shared = await world.manage.create(
        user_id, user_file_id=file_id, expires_in_seconds=None, max_downloads=3, password=None
    )
    token, _ = world.tokens.issue(shared.code)
    original_acquire = world.leases.acquire

    async def acquire_then_create_count_conflict(leased_content_id: int) -> None:
        await original_acquire(leased_content_id)
        async with SQLiteUnitOfWork(world.database) as uow:
            await uow.connection.execute(
                text("UPDATE share_links SET download_count = 1 WHERE id = :id"),
                {"id": shared.view.id},
            )

    monkeypatch.setattr(world.leases, "acquire", acquire_then_create_count_conflict)
    result = await world.public.download(shared.code, token, None, None)
    await world.public.release(result)
    assert world.leases.in_flight(content_id) == 0
    async with SQLiteUnitOfWork(world.database) as uow:
        current = await ShareRepository(uow.connection).get_share(shared.view.id)
    assert current.download_count == 2


async def test_count_retry_stops_after_five_active_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, user_id = await _world(tmp_path)
    _, file_id = await _seed_file(world.database, world.root, user_id, 1, b"payload")
    shared = await world.manage.create(
        user_id, user_file_id=file_id, expires_in_seconds=None, max_downloads=5, password=None
    )
    record = await world.public._active_share(shared.code)
    original_factory = world.public.repository_factory
    attempts = 0

    class _ConflictingRepository:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

        async def increment_download_count(
            self, share_id: int, *, expected_count: int, now_ms: int
        ) -> None:
            nonlocal attempts
            attempts += 1
            raise DomainError(ErrorCode.INVALID_STATE, "分享计数已变化，请重试")

    def conflicting_factory(uow: object) -> object:
        return _ConflictingRepository(original_factory(uow))

    monkeypatch.setattr(world.public, "repository_factory", conflicting_factory)
    with pytest.raises(DomainError) as caught:
        await world.public._count_download(record)
    assert caught.value.code is ErrorCode.INVALID_STATE
    assert attempts == 5


async def test_concurrent_full_downloads_hit_max_exactly(tmp_path: Path) -> None:
    world, user_id = await _world(tmp_path)
    payload = b"abcdef"
    content_id, file_id = await _seed_file(world.database, world.root, user_id, 1, payload)
    shared = await world.manage.create(user_id, user_file_id=file_id, expires_in_seconds=None, max_downloads=3, password=None)
    token, _ = world.tokens.issue(shared.code)

    async def full_download() -> str:
        result = await world.public.download(shared.code, token, None, None)
        body = b"".join([chunk async for chunk in world.public.iter_chunks(result)])
        await world.public.release(result)
        return body.decode()

    bodies = await asyncio.gather(*(full_download() for _ in range(3)))
    assert bodies == ["abcdef"] * 3

    with pytest.raises(DomainError) as caught:
        await world.public.download(shared.code, token, None, None)
    assert caught.value.code is ErrorCode.SHARE_EXHAUSTED
    assert world.leases.in_flight(content_id) == 0


async def test_directory_entry_download_and_invalid_targets(tmp_path: Path) -> None:
    world, user_id = await _world(tmp_path)
    _, dir_file = await _seed_directory(world.database, world.root, user_id, 2)
    _, file_id = await _seed_file(world.database, world.root, user_id, 1, b"xyz")
    shared = await world.manage.create(user_id, user_file_id=dir_file, expires_in_seconds=None, max_downloads=None, password=None)
    single = await world.manage.create(user_id, user_file_id=file_id, expires_in_seconds=None, max_downloads=None, password=None)
    token, _ = world.tokens.issue(shared.code)

    entry = await world.public.download(shared.code, token, "docs/child.bin", "bytes=1-2")
    assert entry.decision.status_code == 206
    window = b"".join([chunk async for chunk in world.public.iter_chunks(entry)])
    assert window == b"bc"
    await world.public.release(entry)

    with pytest.raises(DomainError) as caught:
        await world.public.download(shared.code, token, None, None)
    assert caught.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(DomainError) as caught:
        await world.public.download(shared.code, token, "docs", None)
    assert caught.value.code is ErrorCode.INVALID_INPUT
    single_token, _ = world.tokens.issue(single.code)
    with pytest.raises(DomainError) as caught:
        await world.public.download(single.code, single_token, "nested/x", None)
    assert caught.value.code is ErrorCode.INVALID_INPUT


async def test_count_failure_releases_lease_on_any_exception(tmp_path: Path) -> None:
    world, user_id = await _world(tmp_path)
    _, file_id = await _seed_file(world.database, world.root, user_id, 1, b"payload")
    shared = await world.manage.create(
        user_id, user_file_id=file_id, expires_in_seconds=None, max_downloads=5, password=None
    )
    token, _ = world.tokens.issue(shared.code)

    original_factory = world.public.repository_factory

    class _ExplodingRepository:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

        async def increment_download_count(
            self, share_id: int, *, expected_count: int, now_ms: int
        ) -> None:
            raise RuntimeError("计数存储故障")

    def exploding_factory(uow: object) -> object:
        return _ExplodingRepository(original_factory(uow))

    world.public.repository_factory = exploding_factory  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            await world.public.download(shared.code, token, None, None)
    finally:
        world.public.repository_factory = original_factory  # type: ignore[assignment]
    assert world.leases.in_flight(1) == 0, "非 DomainError 异常也必须释放读租约"


@pytest.mark.parametrize("range_header", [None, "bytes=0-0"])
async def test_download_cancellation_after_acquire_releases_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, range_header: str | None
) -> None:
    world, user_id = await _world(tmp_path)
    content_id, file_id = await _seed_file(world.database, world.root, user_id, 1, b"payload")
    shared = await world.manage.create(
        user_id, user_file_id=file_id, expires_in_seconds=None, max_downloads=5, password=None
    )
    token, _ = world.tokens.issue(shared.code)
    cancelled = asyncio.CancelledError("下载准入取消")

    if range_header is None:

        async def cancel_count(record: ShareRecord) -> None:
            assert world.leases.in_flight(content_id) == 1
            raise cancelled

        monkeypatch.setattr(world.public, "_count_download", cancel_count)
    else:
        original_active_share = world.public._active_share
        active_share_calls = 0

        async def cancel_recheck(code: str) -> ShareRecord:
            nonlocal active_share_calls
            active_share_calls += 1
            if active_share_calls == 1:
                return await original_active_share(code)
            assert world.leases.in_flight(content_id) == 1
            raise cancelled

        monkeypatch.setattr(world.public, "_active_share", cancel_recheck)

    with pytest.raises(asyncio.CancelledError) as caught:
        await world.public.download(shared.code, token, None, range_header)
    assert caught.value is cancelled
    assert world.leases.in_flight(content_id) == 0
