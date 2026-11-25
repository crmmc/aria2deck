"""单元 6：端到端——样本 → 迁移 → V2 结构校验 → 逐项断言 → 逐字节比对。"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from sqlalchemy import select

from aria2deck.infrastructure.sqlite.bootstrap import validate_database
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.schema import (
    content_entries,
    content_objects,
    quota_accounts,
    share_links,
    user_files,
    users,
)
from tests.tools.conftest import SampleLayout, checksum_tree
from tools.migrate_v1.__main__ import main


def _run_cli(layout: SampleLayout, v2_root: Path, *extra: str) -> int:
    return main(
        [
            "--v1-database",
            str(layout.database),
            "--v1-download-dir",
            str(layout.download_dir),
            "--v2-root",
            str(v2_root),
            *extra,
        ]
    )


def _rows(v2_root: Path, expression: object) -> list[dict[str, object]]:
    async def run() -> list[dict[str, object]]:
        database = SQLiteDatabase(v2_root / "data" / "v2.db")
        try:
            async with database.connection() as connection:
                result = await connection.execute(expression)  # type: ignore[arg-type]
                return [dict(row) for row in result.mappings().all()]
        finally:
            await database.close()

    return asyncio.run(run())


@pytest.fixture
def migrated(v1_sample: SampleLayout, tmp_path: Path, capsys) -> tuple[SampleLayout, Path]:
    v2_root = tmp_path / "v2"
    exit_code = _run_cli(v1_sample, v2_root)
    captured = capsys.readouterr().out
    assert exit_code == 0, captured
    assert "结论: 迁移成功" in captured
    return v1_sample, v2_root


def test_migrated_database_passes_v2_startup_validation(
    migrated: tuple[SampleLayout, Path],
) -> None:
    _, v2_root = migrated
    database = SQLiteDatabase(v2_root / "data" / "v2.db")

    asyncio.run(validate_database(database))

    asyncio.run(database.close())


def test_users_files_and_shares_match_v1(migrated: tuple[SampleLayout, Path]) -> None:
    _, v2_root = migrated
    user_rows = _rows(v2_root, select(users.c.username, users.c.is_admin, users.c.quota_bytes))
    file_rows = _rows(
        v2_root,
        select(user_files.c.user_id, user_files.c.display_name, content_objects.c.kind).select_from(
            user_files.join(content_objects, content_objects.c.id == user_files.c.content_id)
        ),
    )
    share_rows = _rows(v2_root, select(share_links.c.code, share_links.c.password_hash))

    assert [row["username"] for row in user_rows] == ["alice", "bob", "dave"]
    assert {str(row["display_name"]) for row in file_rows} == {
        "shared.bin",
        "bob-shared.bin",
        "solo.bin",
        "folder",
    }
    assert {str(row["code"]) for row in share_rows} == {"shareaaa", "sharebbb"}
    assert all(row["password_hash"] is None for row in share_rows)


def test_shared_content_is_stored_once_on_disk(migrated: tuple[SampleLayout, Path]) -> None:
    layout, v2_root = migrated
    shared_digest = hashlib.sha256(layout.file_paths["shared"].read_bytes()).hexdigest()

    canonical = v2_root / "downloads" / "objects" / f"file-{shared_digest}"

    assert canonical.is_file()
    duplicates = [
        path
        for path in (v2_root / "downloads" / "objects").glob("file-*")
        if hashlib.sha256(path.read_bytes()).hexdigest() == shared_digest
    ]
    assert len(duplicates) == 1

    owners = _rows(
        v2_root,
        select(user_files.c.display_name)
        .select_from(user_files.join(content_objects, content_objects.c.id == user_files.c.content_id))
        .where(content_objects.c.digest == shared_digest),
    )
    assert {str(row["display_name"]) for row in owners} == {"shared.bin", "bob-shared.bin"}


def test_downloaded_bytes_are_identical_to_v1(migrated: tuple[SampleLayout, Path]) -> None:
    layout, v2_root = migrated
    objects = v2_root / "downloads" / "objects"

    for name in ("solo", "shared"):
        source = layout.file_paths[name]
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        assert (objects / f"file-{digest}").read_bytes() == source.read_bytes()


def test_directory_can_be_browsed_and_downloaded(migrated: tuple[SampleLayout, Path]) -> None:
    layout, v2_root = migrated
    entries = _rows(v2_root, select(content_entries.c.path, content_entries.c.entry_kind))
    stored = {str(row["path"]): str(row["entry_kind"]) for row in entries}

    assert stored == {
        "nested": "directory",
        "nested/two.txt": "file",
        "one.txt": "file",
    }

    directory = _rows(
        v2_root, select(content_objects.c.digest).where(content_objects.c.kind == "directory")
    )[0]
    root = v2_root / "downloads" / "objects" / f"directory-{directory['digest']}"
    source = layout.file_paths["folder"]
    for relative in ("one.txt", "nested/two.txt"):
        assert (root / relative).read_bytes() == (source / relative).read_bytes()


def test_quota_matches_recomputed_values(migrated: tuple[SampleLayout, Path]) -> None:
    _, v2_root = migrated
    quotas = _rows(v2_root, select(quota_accounts.c.user_id, quota_accounts.c.used_bytes))
    expected = _rows(
        v2_root,
        select(user_files.c.user_id, content_objects.c.size).select_from(
            user_files.join(content_objects, content_objects.c.id == user_files.c.content_id)
        ),
    )

    totals: dict[int, int] = {}
    for row in expected:
        key = int(row["user_id"])
        totals[key] = totals.get(key, 0) + int(row["size"])

    recorded = {int(row["user_id"]): int(row["used_bytes"]) for row in quotas}
    assert {user_id: recorded[user_id] for user_id in totals} == totals
    assert all(value == 0 for user_id, value in recorded.items() if user_id not in totals)
    assert all(value >= 0 for value in recorded.values())


def test_v1_side_is_unchanged_by_the_migration(
    v1_sample: SampleLayout, tmp_path: Path, capsys
) -> None:
    before_db = hashlib.sha256(v1_sample.database.read_bytes()).hexdigest()
    before_tree = checksum_tree(v1_sample.download_dir)

    assert _run_cli(v1_sample, tmp_path / "v2") == 0
    capsys.readouterr()

    assert hashlib.sha256(v1_sample.database.read_bytes()).hexdigest() == before_db
    assert checksum_tree(v1_sample.download_dir) == before_tree
    assert not (v1_sample.download_dir / "v2").exists()


def test_rerun_into_same_target_is_refused(
    migrated: tuple[SampleLayout, Path], capsys
) -> None:
    layout, v2_root = migrated
    before = checksum_tree(v2_root)

    assert _run_cli(layout, v2_root) == 1

    assert "非空" in capsys.readouterr().out
    assert checksum_tree(v2_root) == before


# ------------------------------ 迁移后真的能登录：用生成的密码走一遍 AccountService.login


def _credentials_from(stdout: str) -> tuple[str, str]:
    lines = stdout.splitlines()
    start = next(index for index, line in enumerate(lines) if "管理员登录凭据" in line)
    user_prefix, password_prefix = "用户名: ", "密  码: "
    username = next(line for line in lines[start:] if line.startswith(user_prefix))[
        len(user_prefix) :
    ]
    password = next(line for line in lines[start:] if line.startswith(password_prefix))[
        len(password_prefix) :
    ]
    return username, password


async def _login(v2_root: Path, username: str, password: str):
    """用 V2 真实服务登录迁移出来的库；前端那一步派生在测试里显式做一遍。"""
    from aria2deck.application.accounts.service import AccountService
    from aria2deck.infrastructure.security import CredentialManager
    from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
    from aria2deck.infrastructure.sqlite.repositories.accounts import AccountRepository
    from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

    credentials = CredentialManager(pepper=None, debug=True)
    database = SQLiteDatabase(v2_root / "data" / "v2.db")
    service = AccountService(
        uow_factory=lambda: SQLiteUnitOfWork(database),
        repository_factory=lambda uow: AccountRepository(uow.connection),
        credentials=credentials,
        clock_ms=lambda: 1_000_000,
        session_ttl_seconds=3600,
    )
    try:
        submitted = credentials.derive_password_hash(username=username, password=password)
        return await service.login(username=username, password_hash=submitted)
    finally:
        await database.close()


def test_generated_password_logs_in_through_account_service(
    v1_sample: SampleLayout, tmp_path: Path, capsys
) -> None:
    """迁移产物直接喂给真实的 AccountService.login，断言能登录且带改密标记。"""
    v2_root = tmp_path / "v2"
    assert _run_cli(v1_sample, v2_root) == 0
    username, password = _credentials_from(capsys.readouterr().out)

    result = asyncio.run(_login(v2_root, username, password))

    assert result.view.user.username == "alice"
    assert result.view.user.is_admin is True
    assert result.view.user.is_initial_password is True
    assert result.session_id


def test_other_accounts_cannot_log_in_with_any_password(
    v1_sample: SampleLayout, tmp_path: Path, capsys
) -> None:
    from aria2deck.domain.errors import DomainError

    v2_root = tmp_path / "v2"
    assert _run_cli(v1_sample, v2_root) == 0
    username, password = _credentials_from(capsys.readouterr().out)

    for other in ("bob", "dave"):
        for candidate in (password, "", "anything", "!migrated-placeholder!"):
            with pytest.raises(DomainError):
                asyncio.run(_login(v2_root, other, candidate))
    # 引导管理员自己也只认那一个密码
    with pytest.raises(DomainError):
        asyncio.run(_login(v2_root, username, "wrong-password"))
