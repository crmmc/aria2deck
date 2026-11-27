"""schema 2 → schema 3 升级命令：真实 v2 库、快照锚定、漂移拒绝、备份与回滚。"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.sqlite import upgrade as upgrade_module
from aria2deck.infrastructure.sqlite.bootstrap import (
    SchemaSnapshot,
    _expected_snapshot,
    _snapshot,
    initialize_database,
    validate_database,
)
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.schema import SCHEMA_VERSION
from aria2deck.infrastructure.sqlite.upgrade import (
    RELEASED_SCHEMA_2_DDL,
    UpgradeRefused,
    _released_schema_2,
    upgrade_database,
)

FIXED_TIME = datetime(2026, 11, 27, 2, 0, tzinfo=UTC)
MANIFESTS = "attempt_manifests"
REAL_HASH = "pbkdf2_sha256$120000$00112233445566778899aabbccddeeff$" + "11" * 32
SECOND_HASH = "pbkdf2_sha256$120000$ffeeddccbbaa99887766554433221100$" + "22" * 32
SOURCE_TABLES = (
    "users",
    "quota_accounts",
    "sessions",
    "api_tokens",
    "download_sources",
    "download_attempts",
    "task_subscriptions",
    "content_objects",
    "user_files",
    "share_links",
    "app_settings",
    "tracker_cache",
    "schema_meta",
)
# 本地 dev 库是真实的 schema 2 生产同构样本；不存在时该用例跳过，不拖累其他环境。
DEV_LIBRARY = Path(
    os.environ.get(
        "MIJIW2RTKI_DEV_DATABASE",
        "/tmp/aria2deck-dev/data/v2.db",  # noqa: S108  # 本地 dev 库的固定位置，可用环境变量覆盖
    )
)


def rows(path: Path, sql: str, parameters: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


def table_names(path: Path) -> list[str]:
    return [
        str(name)
        for (name,) in rows(
            path,
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name",
        )
    ]


def row_counts(path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in table_names(path):
        (count,) = rows(path, f'SELECT COUNT(*) FROM "{table}"')[0]  # noqa: S608  # 表名取自本库
        counts[table] = int(cast(int, count))
    return counts


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.stem}-pre-status-upgrade-*{path.suffix}"))


def seed(path: Path) -> None:
    """灌入每个既有表都非空的数据，升级才能证明「只加表、不动老数据」。"""
    connection = sqlite3.connect(path)
    try:
        for index, username in enumerate(("admin", "second"), start=1):
            connection.execute(
                "INSERT INTO users (id, username, username_key, password_hash, is_admin, "
                "quota_bytes, is_initial_password, credential_generation, pending_delete, "
                "delete_retry_count, created_at_ms, updated_at_ms, status) "
                "VALUES (?, ?, ?, ?, ?, 1000, 0, 1, 0, 0, 10, 10, 'active')",
                (index, username, username, REAL_HASH if index == 1 else SECOND_HASH, index == 1),
            )
            connection.execute(
                "INSERT INTO quota_accounts (user_id, used_bytes, reserved_bytes, revision, "
                "updated_at_ms) VALUES (?, ?, 0, 3, 10)",
                (index, index * 100),
            )
        connection.execute(
            "INSERT INTO sessions (id, user_id, credential_generation, created_at_ms, "
            "expires_at_ms) VALUES ('session-1', 2, 1, 1, 1000000000000)"
        )
        connection.execute(
            "INSERT INTO api_tokens (user_id, name, token_digest, prefix, created_at_ms) "
            "VALUES (2, 'cli', 'token-digest', 'prefix', 1)"
        )
        for index in (1, 2):
            connection.execute(
                "INSERT INTO download_sources (id, kind, resource_key, payload_json, "
                "options_json, display_name, created_at_ms) "
                "VALUES (?, 'http', ?, '{}', '{}', ?, 10)",
                (index, f"resource-{index}", f"file-{index}.bin"),
            )
            connection.execute(
                "INSERT INTO download_attempts (id, source_id, resource_key, gid, status, "
                "submission_state, completed_length, physical_reserved_bytes, created_at_ms, "
                "updated_at_ms) VALUES (?, ?, ?, ?, 'completed', 'submitted', 10, 10, 10, 10)",
                (index, index, f"resource-{index}", f"gid-{index}"),
            )
            connection.execute(
                "INSERT INTO task_subscriptions (attempt_id, user_id, status, reserved_bytes, "
                "display_name, created_at_ms, updated_at_ms) VALUES (?, 2, 'completed', 0, ?, 10, 10)",
                (index, f"file-{index}.bin"),
            )
            connection.execute(
                "INSERT INTO content_objects (id, content_key, kind, digest, size, storage_path, "
                "deletion_state, read_leases, delete_retry_count, created_at_ms, updated_at_ms) "
                "VALUES (?, ?, 'file', ?, 10, ?, 'active', 0, 0, 10, 10)",
                (index, f"content-key-{index}", "ab" * 32, f"objects/{index}"),
            )
            connection.execute(
                "INSERT INTO user_files (id, user_id, content_id, display_name, created_at_ms) "
                "VALUES (?, 2, ?, ?, 10)",
                (index, index, f"file-{index}.bin"),
            )
            connection.execute(
                "INSERT INTO share_links (id, code, owner_id, user_file_id, file_name, file_size, "
                "file_kind, content_key, download_count, created_at_ms) "
                "VALUES (?, ?, 2, ?, ?, 10, 'file', ?, 1, 10)",
                (index, f"code-{index}", index, f"file-{index}.bin", f"content-key-{index}"),
            )
        connection.commit()
    finally:
        connection.close()


async def make_schema_2(path: Path, *, seeded: bool = True) -> Path:
    """全新 v3 库去掉 `attempt_manifests` 并把版本写回 2：结构与已发布 schema 2 逐字一致。"""
    database = SQLiteDatabase(path)
    try:
        await initialize_database(database)
    finally:
        await database.close()
    if seeded:
        seed(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"DROP TABLE {MANIFESTS}")  # noqa: S608  # 内部常量
        connection.execute("UPDATE schema_meta SET schema_version = 2")
        connection.commit()
    finally:
        connection.close()
    return path


async def snapshot_of(path: Path) -> SchemaSnapshot:
    database = SQLiteDatabase(path)
    try:
        async with database.connection() as connection:
            return await _snapshot(connection)
    finally:
        await database.close()


async def test_released_schema_2_is_the_target_structure_without_the_manifest_table() -> None:
    """快照常量必须锚在真实已发布结构上：既不能凭印象手写，也不能跟着当前 DDL 漂移。"""
    released = await _released_schema_2()
    target = await _expected_snapshot()
    without_manifest = {
        name: table for name, table in target.tables.items() if name != MANIFESTS
    }
    assert released.tables == without_manifest
    assert released.indexes == target.indexes
    assert released.triggers == target.triggers
    # 只有显式 CREATE INDEX 会出现在快照常量里，UNIQUE 约束自带的自动索引由建表语句派生。
    explicit_indexes = [index for index in target.indexes.values() if index.origin == "c"]
    assert len(RELEASED_SCHEMA_2_DDL) == (
        len(without_manifest) + len(explicit_indexes) + len(target.triggers)
    )


async def test_released_schema_2_matches_a_generated_schema_2_database(tmp_path: Path) -> None:
    released = await _released_schema_2()
    assert await snapshot_of(await make_schema_2(tmp_path / "v2.db", seeded=False)) == released


async def test_v2_library_upgrades_to_v3_without_touching_existing_rows(tmp_path: Path) -> None:
    path = await make_schema_2(tmp_path / "v2.db")
    before_counts = row_counts(path)
    before_users = rows(
        path,
        "SELECT id, username, password_hash, quota_bytes, status FROM users ORDER BY id",
    )
    assert MANIFESTS not in before_counts

    report = await upgrade_database(path, now=FIXED_TIME)

    assert report.changed is True
    assert report.version == SCHEMA_VERSION == 3
    assert (report.active_accounts, report.locked_accounts) == (2, 0)
    assert report.backup_path is not None and report.backup_path.is_file()
    assert report.backup_path.name == "v2-pre-status-upgrade-20261127T020000Z.db"
    # 备份是升级前的原件：版本 2、没有新表。
    assert rows(report.backup_path, "SELECT schema_version FROM schema_meta") == [(2,)]
    assert MANIFESTS not in table_names(report.backup_path)

    after_counts = row_counts(path)
    assert MANIFESTS in after_counts
    assert {name: count for name, count in after_counts.items() if name != MANIFESTS} == before_counts
    assert after_counts[MANIFESTS] == 0
    assert rows(path, "SELECT schema_version FROM schema_meta") == [(3,)]
    assert rows(
        path,
        "SELECT id, username, password_hash, quota_bytes, status FROM users ORDER BY id",
    ) == before_users
    assert rows(path, "PRAGMA foreign_key_check") == []

    database = SQLiteDatabase(path)
    try:
        await validate_database(database)
    finally:
        await database.close()
    assert await snapshot_of(path) == await _expected_snapshot()


async def test_real_dev_v2_library_upgrades_on_a_copy(tmp_path: Path) -> None:
    """用本地 dev 真实 v2 库的副本升级：原件不动，行为与合成样本一致。"""
    if not DEV_LIBRARY.is_file():
        pytest.skip(f"本地 dev 库不存在：{DEV_LIBRARY}")
    path = tmp_path / "v2.db"
    source = sqlite3.connect(DEV_LIBRARY)
    try:
        destination = sqlite3.connect(path)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    if rows(path, "SELECT schema_version FROM schema_meta") != [(2,)]:
        pytest.skip("本地 dev 库已不是 schema 2")

    before_counts = row_counts(path)
    attempt_rows_before = rows(path, "SELECT COUNT(*) FROM download_attempts")

    report = await upgrade_database(path, now=FIXED_TIME)

    assert report.changed is True
    assert report.version == 3
    after_counts = row_counts(path)
    assert MANIFESTS in after_counts
    assert {name: count for name, count in after_counts.items() if name != MANIFESTS} == before_counts
    assert rows(path, "SELECT COUNT(*) FROM download_attempts") == attempt_rows_before

    # 升级后的真实库必须能直接过启动链路（initialize_database 含结构校验）。
    database = SQLiteDatabase(path)
    try:
        await initialize_database(database)
    finally:
        await database.close()


async def test_schema_2_library_is_refused_at_startup(tmp_path: Path) -> None:
    """老库不会被启动流程隐式升级：只能拒绝启动，由显式升级工具处理。"""
    path = await make_schema_2(tmp_path / "v2.db", seeded=False)
    database = SQLiteDatabase(path)
    try:
        with pytest.raises(DomainError) as caught:
            await initialize_database(database)
    finally:
        await database.close()
    assert caught.value.code is ErrorCode.SCHEMA_MISMATCH
    assert rows(path, "SELECT schema_version FROM schema_meta") == [(2,)]
    assert MANIFESTS not in table_names(path)


async def test_repeating_the_upgrade_never_applies_step_three_twice(tmp_path: Path) -> None:
    path = await make_schema_2(tmp_path / "v2.db")
    first = await upgrade_database(path, now=FIXED_TIME)
    assert first.backup_path is not None

    second = await upgrade_database(path, now=FIXED_TIME)

    assert second.changed is False
    assert second.backup_path is None
    assert second.version == 3
    assert len(backups(path)) == 1
    assert rows(path, "SELECT schema_version FROM schema_meta") == [(3,)]
    # 幂等重跑既不重复建表，也不丢表里的数据。
    assert rows(path, "SELECT COUNT(*) FROM sqlite_master WHERE name = ?", (MANIFESTS,)) == [(1,)]
    assert table_names(path).count(MANIFESTS) == 1


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("CREATE TABLE rogue_table(id INTEGER PRIMARY KEY)", "表集合"),
        ("ALTER TABLE app_settings ADD COLUMN rogue TEXT", "app_settings"),
        ("ALTER TABLE users ADD COLUMN rogue TEXT", "users"),
        ("DROP TRIGGER trg_share_links_count_monotonic", "索引或触发器"),
    ],
)
async def test_v2_structural_drift_is_refused_without_writing(
    tmp_path: Path, statement: str, message: str
) -> None:
    path = await make_schema_2(tmp_path / "v2.db")
    connection = sqlite3.connect(path)
    try:
        connection.execute(statement)
        connection.commit()
    finally:
        connection.close()
    before = path.read_bytes()

    with pytest.raises(UpgradeRefused, match=message):
        await upgrade_database(path, now=FIXED_TIME)

    assert path.read_bytes() == before
    assert backups(path) == []
    assert rows(path, "SELECT schema_version FROM schema_meta") == [(2,)]
    assert MANIFESTS not in table_names(path)


async def test_v2_library_that_already_has_the_manifest_table_is_refused(tmp_path: Path) -> None:
    """半升级过的库不能当成普通 v2 升级：先建表再建表会失败，必须在写入前就拒绝。"""
    path = await make_schema_2(tmp_path / "v2.db")
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE attempt_manifests (attempt_id INTEGER PRIMARY KEY, "
            "files_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL)"
        )
        connection.commit()
    finally:
        connection.close()
    before = path.read_bytes()

    with pytest.raises(UpgradeRefused, match="表集合"):
        await upgrade_database(path, now=FIXED_TIME)

    assert path.read_bytes() == before
    assert backups(path) == []


async def test_failed_step_three_rolls_back_and_leaves_a_valid_schema_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = await make_schema_2(tmp_path / "v2.db")
    released = await _released_schema_2()

    def broken_ddl(_name: str) -> str:
        raise RuntimeError("模拟建表失败")

    monkeypatch.setattr(upgrade_module, "create_table_statement", broken_ddl)
    with pytest.raises(UpgradeRefused, match="已回滚"):
        await upgrade_database(path, now=FIXED_TIME)
    monkeypatch.undo()

    assert MANIFESTS not in table_names(path)
    assert rows(path, "SELECT schema_version FROM schema_meta") == [(2,)]
    assert await snapshot_of(path) == released
    assert len(backups(path)) == 1

    # 修好之后再跑一次只补做 2→3，不需要重来。
    report = await upgrade_database(path, now=FIXED_TIME)
    assert report.changed is True
    assert rows(path, "SELECT schema_version FROM schema_meta") == [(3,)]
    assert MANIFESTS in table_names(path)


async def test_step_three_detects_pre_existing_foreign_key_damage(tmp_path: Path) -> None:
    """FK 关着写进来的孤儿行不能被带进 schema 3：建表事务里查出来就整体回滚。"""
    path = await make_schema_2(tmp_path / "v2.db")
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO task_subscriptions (attempt_id, user_id, status, reserved_bytes, "
            "display_name, created_at_ms, updated_at_ms) VALUES (999, 999, 'completed', 0, 'x', 1, 1)"
        )
        connection.commit()
    finally:
        connection.close()
    assert rows(path, "PRAGMA foreign_key_check") != []

    with pytest.raises(UpgradeRefused, match="外键校验失败"):
        await upgrade_database(path, now=FIXED_TIME)

    assert MANIFESTS not in table_names(path)
    assert rows(path, "SELECT schema_version FROM schema_meta") == [(2,)]


def test_cli_reports_schema_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = asyncio.run(make_schema_2(tmp_path / "v2.db"))
    monkeypatch.setenv("ARIA2DECK_DATABASE_PATH", str(path))

    assert upgrade_module.main([]) == 0
    first = capsys.readouterr().out
    assert "升级前备份" in first
    assert "数据库已升级到 schema 3。" in first
    assert "schema 版本: 3" in first

    assert upgrade_module.main([]) == 0
    second = capsys.readouterr().out
    assert "数据库已是 schema 3，无需升级。" in second
    assert "升级前备份" not in second
