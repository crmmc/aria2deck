"""schema 1 → schema 2 升级命令：真实旧库、备份、幂等、回滚与 CLI 报告。"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.instance_lock import InstanceLock
from aria2deck.infrastructure.sqlite import upgrade as upgrade_module
from aria2deck.infrastructure.sqlite.bootstrap import (
    SchemaSnapshot,
    _expected_snapshot,
    _snapshot,
    initialize_database,
    validate_database,
)
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.schema import SCHEMA_VERSION, create_table_statement
from aria2deck.infrastructure.sqlite.upgrade import (
    MIGRATED_PLACEHOLDER_PASSWORD,
    RELEASED_SCHEMA_1_USERS_DDL,
    RELEASED_SCHEMA_1_USERS_SQL,
    UpgradeRefused,
    upgrade_database,
)
from tools.migrate_v1.planner import PLACEHOLDER_PASSWORD

FIXED_TIME = datetime(2026, 11, 26, 8, 30, tzinfo=UTC)
PLACEHOLDER = MIGRATED_PLACEHOLDER_PASSWORD
REAL_HASH = "pbkdf2_sha256$120000$00112233445566778899aabbccddeeff$" + "11" * 32
SECOND_HASH = "pbkdf2_sha256$120000$ffeeddccbbaa99887766554433221100$" + "22" * 32

# 上一版发布时 users 表的建表语句原文：直接复用升级模块里的同一常量，避免测试自带一份
# 可能已经漂移的副本（否则测试与产品的「已发布结构」会一起错）。
RELEASED_USERS_DDL = RELEASED_SCHEMA_1_USERS_DDL

USERS = (
    ("admin", REAL_HASH, True),
    ("placeholder", PLACEHOLDER, False),
    ("changed", SECOND_HASH, False),
)


async def _fresh(path: Path) -> None:
    database = SQLiteDatabase(path)
    await initialize_database(database)
    await database.close()


def _downgrade_to_schema_1(path: Path) -> None:
    """把全新库改写成上一版真实结构：去掉 users.status 与 schema 3 新表，并把版本写回 1。"""
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE attempt_manifests")
        connection.execute(
            "CREATE TABLE _users_copy AS SELECT id, username, username_key, password_hash, "
            "is_admin, quota_bytes, is_initial_password, rpc_secret_ciphertext, "
            "rpc_secret_prefix, credential_generation, pending_delete, delete_lease_owner, "
            "delete_lease_expires_at_ms, delete_retry_count, created_at_ms, updated_at_ms "
            "FROM users"
        )
        connection.execute("DROP TABLE users")
        connection.execute(RELEASED_USERS_DDL)
        connection.execute("INSERT INTO users SELECT * FROM _users_copy")
        connection.execute("DROP TABLE _users_copy")
        connection.execute("UPDATE schema_meta SET schema_version = 1")
        connection.commit()
    finally:
        connection.close()


async def make_schema_1(path: Path, *, seed_library: bool = False) -> Path:
    await _fresh(path)
    connection = sqlite3.connect(path)
    try:
        for index, (username, password_hash, is_admin) in enumerate(USERS, start=1):
            connection.execute(
                "INSERT INTO users (username, username_key, password_hash, is_admin, "
                "quota_bytes, is_initial_password, credential_generation, pending_delete, "
                "delete_retry_count, created_at_ms, updated_at_ms, status) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, 0, 0, 10, 10, 'active')",
                (username, username, password_hash, is_admin, 1000 + index, index + 1),
            )
            connection.execute(
                "INSERT INTO quota_accounts (user_id, used_bytes, reserved_bytes, revision, "
                "updated_at_ms) VALUES (?, ?, 0, 0, 0)",
                (index, index * 100),
            )
        if seed_library:
            connection.execute(
                "INSERT INTO content_objects (id, content_key, kind, digest, size, storage_path, "
                "deletion_state, read_leases, delete_retry_count, created_at_ms, updated_at_ms) "
                "VALUES (1, 'content-key', 'file', ?, 10, 'objects/a', 'active', 0, 0, 1, 1)",
                ("ab" * 32,),
            )
            connection.execute(
                "INSERT INTO user_files (id, user_id, content_id, display_name, created_at_ms) "
                "VALUES (1, 2, 1, 'a.bin', 1)"
            )
            connection.execute(
                "INSERT INTO share_links (id, code, owner_id, user_file_id, file_name, file_size, "
                "file_kind, content_key, download_count, created_at_ms) VALUES (1, 'code-1', 2, 1, "
                "'a.bin', 10, 'file', 'content-key', 0, 1)"
            )
            # 真实生产库一定有子表引用：外键若在重建期间仍生效，DROP TABLE 会级联或直接失败。
            connection.execute(
                "INSERT INTO sessions (id, user_id, credential_generation, created_at_ms, "
                "expires_at_ms) VALUES ('session-1', 2, 3, 1, 1000000000000)"
            )
            connection.execute(
                "INSERT INTO api_tokens (user_id, name, token_digest, prefix, created_at_ms) "
                "VALUES (2, 'cli', 'token-digest', 'prefix', 1)"
            )
        connection.commit()
    finally:
        connection.close()
    _downgrade_to_schema_1(path)
    return path


def rows(path: Path, sql: str) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


async def snapshot_of(path: Path) -> SchemaSnapshot:
    database = SQLiteDatabase(path)
    try:
        async with database.connection() as connection:
            return await _snapshot(connection)
    finally:
        await database.close()


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.stem}-pre-status-upgrade-*{path.suffix}"))


async def test_missing_empty_and_unversioned_databases_are_refused(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(UpgradeRefused, match="不存在"):
        await upgrade_database(missing)
    assert not missing.exists()

    empty = tmp_path / "empty.db"
    empty.touch()
    with pytest.raises(UpgradeRefused, match="schema 版本"):
        await upgrade_database(empty)

    unversioned = tmp_path / "unversioned.db"
    await _fresh(unversioned)
    connection = sqlite3.connect(unversioned)
    try:
        connection.execute("DELETE FROM schema_meta")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(UpgradeRefused, match="单行版本记录"):
        await upgrade_database(unversioned)


async def test_unknown_version_and_structural_drift_are_refused_without_writing(
    tmp_path: Path,
) -> None:
    future = await make_schema_1(tmp_path / "future.db")
    connection = sqlite3.connect(future)
    try:
        connection.execute("UPDATE schema_meta SET schema_version = 99")
        connection.commit()
    finally:
        connection.close()
    before = future.read_bytes()
    with pytest.raises(UpgradeRefused, match="不支持的 schema 版本"):
        await upgrade_database(future)
    assert future.read_bytes() == before
    assert backups(future) == []

    drifted = await make_schema_1(tmp_path / "drifted.db")
    connection = sqlite3.connect(drifted)
    try:
        connection.execute("DROP INDEX uq_download_attempts_live_resource")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(UpgradeRefused, match="索引或触发器"):
        await upgrade_database(drifted)
    assert backups(drifted) == []
    assert rows(drifted, "SELECT schema_version FROM schema_meta") == [(1,)]

    half_upgraded = await make_schema_1(tmp_path / "half.db")
    connection = sqlite3.connect(half_upgraded)
    try:
        connection.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(UpgradeRefused, match="schema 1 结构"):
        await upgrade_database(half_upgraded)


async def test_upgrade_backs_up_and_locks_only_placeholder_accounts(tmp_path: Path) -> None:
    path = await make_schema_1(tmp_path / "v2.db", seed_library=True)
    before_users = rows(path, "SELECT id, username, password_hash, quota_bytes, "
                              "credential_generation FROM users ORDER BY id")
    before_counts = rows(path, "SELECT (SELECT COUNT(*) FROM user_files), "
                                "(SELECT COUNT(*) FROM share_links), "
                                "(SELECT SUM(used_bytes) FROM quota_accounts)")

    report = await upgrade_database(path, now=FIXED_TIME)

    assert report.changed is True
    assert report.version == SCHEMA_VERSION == 3
    assert (report.active_accounts, report.locked_accounts) == (2, 1)
    assert report.backup_path is not None and report.backup_path.is_file()
    assert report.backup_path.name == "v2-pre-status-upgrade-20261126T083000Z.db"
    assert rows(report.backup_path, "SELECT schema_version FROM schema_meta") == [(1,)]
    assert "status" not in [column[1] for column in rows(report.backup_path, "PRAGMA table_info(users)")]
    assert "attempt_manifests" not in [
        name for (name,) in rows(report.backup_path, "SELECT name FROM sqlite_master WHERE type = 'table'")
    ]

    assert rows(path, "SELECT username, status FROM users ORDER BY id") == [
        ("admin", "active"),
        ("placeholder", "locked"),
        ("changed", "active"),
    ]
    assert rows(path, "SELECT schema_version FROM schema_meta") == [(3,)]
    # schema 1 库走 1→2→3 两级：账号状态回填与 schema 3 新表都要到位。
    assert rows(
        path, "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'attempt_manifests'"
    ) == [(1,)]
    assert rows(path, "SELECT id, username, password_hash, quota_bytes, credential_generation "
                       "FROM users ORDER BY id") == before_users
    assert rows(path, "SELECT (SELECT COUNT(*) FROM user_files), "
                      "(SELECT COUNT(*) FROM share_links), "
                      "(SELECT SUM(used_bytes) FROM quota_accounts)") == before_counts
    assert rows(path, "SELECT display_name FROM user_files") == [("a.bin",)]
    assert rows(path, "SELECT code FROM share_links") == [("code-1",)]
    # 重建 users 时不得级联删除引用它的子表行。
    assert rows(path, "SELECT id, user_id FROM sessions") == [("session-1", 2)]
    assert rows(path, "SELECT name, user_id FROM api_tokens") == [("cli", 2)]

    database = SQLiteDatabase(path)
    try:
        await validate_database(database)
    finally:
        await database.close()
    assert await snapshot_of(path) == await _expected_snapshot()


async def test_second_run_only_verifies_and_never_relocks_unlocked_accounts(
    tmp_path: Path,
) -> None:
    path = await make_schema_1(tmp_path / "v2.db")
    first = await upgrade_database(path, now=FIXED_TIME)
    assert first.backup_path is not None

    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE users SET status = 'active' WHERE username = 'placeholder'")
        connection.commit()
    finally:
        connection.close()

    second = await upgrade_database(path, now=FIXED_TIME)

    assert second.changed is False
    assert second.backup_path is None
    assert (second.active_accounts, second.locked_accounts) == (3, 0)
    assert len(backups(path)) == 1
    assert rows(path, "SELECT username, status FROM users ORDER BY id") == [
        ("admin", "active"),
        ("placeholder", "active"),
        ("changed", "active"),
    ]


async def test_backup_failure_aborts_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = await make_schema_1(tmp_path / "v2.db")
    before = path.read_bytes()
    real_connect = sqlite3.connect

    def failing_connect(target: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        if "pre-status-upgrade" in str(target):
            raise sqlite3.OperationalError("disk I/O error")
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(upgrade_module.sqlite3, "connect", failing_connect)
    with pytest.raises(UpgradeRefused, match="备份失败"):
        await upgrade_database(path, now=FIXED_TIME)

    assert path.read_bytes() == before
    assert backups(path) == []


async def test_interrupted_upgrade_rolls_back_every_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = await make_schema_1(tmp_path / "v2.db")

    def broken_ddl(_name: str) -> str:
        raise RuntimeError("模拟建表失败")

    monkeypatch.setattr(upgrade_module, "create_table_statement", broken_ddl)
    with pytest.raises(UpgradeRefused, match="已回滚"):
        await upgrade_database(path, now=FIXED_TIME)

    columns = [name for name, *_ in rows(path, "PRAGMA table_info(users)")]
    assert "status" not in columns
    assert rows(path, "SELECT COUNT(*) FROM users") == [(3,)]
    assert rows(path, "SELECT schema_version FROM schema_meta") == [(1,)]
    assert [name for name, *_ in rows(path, "PRAGMA table_info(_users_schema_1)")] == []
    assert len(backups(path)) == 1


async def test_orphaned_rows_abort_the_upgrade(tmp_path: Path) -> None:
    path = await make_schema_1(tmp_path / "v2.db")
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO user_files (id, user_id, content_id, display_name, created_at_ms) "
            "VALUES (9, 999, 999, 'orphan.bin', 1)"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(UpgradeRefused, match="外键校验失败"):
        await upgrade_database(path, now=FIXED_TIME)
    assert rows(path, "SELECT schema_version FROM schema_meta") == [(1,)]
    assert "status" not in [name for name, *_ in rows(path, "PRAGMA table_info(users)")]


async def test_running_instance_blocks_upgrade(tmp_path: Path) -> None:
    path = await make_schema_1(tmp_path / "v2.db")
    lock = InstanceLock(path.with_name(path.name + ".lock"))
    lock.acquire()
    try:
        with pytest.raises(UpgradeRefused, match="实例锁"):
            await upgrade_database(path, now=FIXED_TIME)
    finally:
        lock.release()
    assert rows(path, "SELECT schema_version FROM schema_meta") == [(1,)]


def test_backup_files_are_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "v2.db"
    path.write_bytes(b"")
    first = upgrade_module.create_backup(path, now=FIXED_TIME)
    first.write_bytes(b"keep-me")
    second = upgrade_module.create_backup(path, now=FIXED_TIME)
    assert first != second
    assert first.read_bytes() == b"keep-me"
    assert second.name == "v2-pre-status-upgrade-20261126T083000Z-1.db"


def test_cli_reports_counts_and_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = asyncio.run(make_schema_1(tmp_path / "v2.db"))
    monkeypatch.setenv("ARIA2DECK_DATABASE_PATH", str(path))

    assert upgrade_module.main([]) == 0
    first_run = capsys.readouterr().out
    assert "升级前备份" in first_run
    assert "正常账号: 2，锁定账号: 1" in first_run
    assert "逐个重置密码" in first_run

    assert upgrade_module.main([]) == 0
    assert "已是 schema 3" in capsys.readouterr().out

    missing = tmp_path / "missing.db"
    monkeypatch.setenv("ARIA2DECK_DATABASE_PATH", str(missing))
    assert upgrade_module.main([]) == 1
    assert "升级未执行" in capsys.readouterr().err

    monkeypatch.delenv("ARIA2DECK_DATABASE_PATH")
    assert upgrade_module.main([]) == 1
    assert "未指定数据库" in capsys.readouterr().err


def test_placeholder_constant_matches_offline_migration_tool() -> None:
    assert PLACEHOLDER == PLACEHOLDER_PASSWORD


def test_database_path_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ARIA2DECK_DATABASE_PATH", raising=False)
    with pytest.raises(UpgradeRefused, match="未指定数据库"):
        upgrade_module._database_path()
    monkeypatch.setenv("ARIA2DECK_DATABASE_PATH", str(tmp_path / "env.db"))
    assert upgrade_module._database_path() == tmp_path / "env.db"


async def test_extra_table_and_column_drift_are_refused(tmp_path: Path) -> None:
    extra_table = await make_schema_1(tmp_path / "extra_table.db")
    connection = sqlite3.connect(extra_table)
    try:
        connection.execute("CREATE TABLE rogue_table(id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(UpgradeRefused, match="表集合"):
        await upgrade_database(extra_table, now=FIXED_TIME)

    extra_column = await make_schema_1(tmp_path / "extra_column.db")
    connection = sqlite3.connect(extra_column)
    try:
        connection.execute("ALTER TABLE app_settings ADD COLUMN rogue TEXT")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(UpgradeRefused, match="app_settings"):
        await upgrade_database(extra_column, now=FIXED_TIME)


async def test_users_foreign_key_drift_is_refused(tmp_path: Path) -> None:
    path = await make_schema_1(tmp_path / "v2.db")
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE users_drift AS SELECT * FROM users"
        )
        connection.execute("DROP TABLE users")
        drifted_ddl = RELEASED_USERS_DDL.rstrip()
        assert drifted_ddl.endswith(")")
        connection.execute(
            drifted_ddl[:-1].rstrip()
            + ",\n\tFOREIGN KEY (id) REFERENCES sessions (user_id)\n)"
        )
        connection.execute("INSERT INTO users SELECT * FROM users_drift")
        connection.execute("DROP TABLE users_drift")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(UpgradeRefused, match="外键"):
        await upgrade_database(path, now=FIXED_TIME)


def test_released_users_ddl_is_exactly_the_target_ddl_without_status() -> None:
    """「已发布 schema 1 users」必须逐字等于目标 DDL 去掉 status 两行后的文本。

    这条不变量锚定了常量与真实目标结构的关系：常量既不能是凭印象手写的近似文本，
    也不能被顺手改成“当前库的样子”，否则校验对象会跟着实现漂移。
    它不依赖 git（`verify-v2-standalone` 的临时目录里没有版本库）。
    """
    target = create_table_statement("users")
    without_status = "\n".join(
        line
        for line in target.split("\n")
        if "status TEXT" not in line and "ck_users_status" not in line
    )
    assert without_status == RELEASED_SCHEMA_1_USERS_DDL
    assert RELEASED_SCHEMA_1_USERS_SQL == " ".join(RELEASED_SCHEMA_1_USERS_DDL.split())
    # 已发布版本里确实没有状态列与状态约束。
    assert "status" not in RELEASED_SCHEMA_1_USERS_SQL
    assert "ck_users_status" not in RELEASED_SCHEMA_1_USERS_SQL


@pytest.mark.parametrize(
    "extra_constraint",
    [
        "CONSTRAINT unexpected_users_check CHECK (length(username) > 0)",
        # 约束名不同、语义与已有 CHECK 重复：必须同样拒绝，不能按名字白名单放行。
        "CONSTRAINT ck_users_quota_nonnegative_copy CHECK (quota_bytes >= 0)",
    ],
)
async def test_source_users_ddl_drift_is_refused_without_writing(
    tmp_path: Path, extra_constraint: str
) -> None:
    """多一个 CHECK 也属于异常源结构：重建会把它抹掉，必须在写库和备份前拒绝。"""
    path = await make_schema_1(tmp_path / "v2.db")
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE users_drift AS SELECT * FROM users")
        connection.execute("DROP TABLE users")
        released = RELEASED_USERS_DDL.rstrip()
        assert released.endswith(")")
        connection.execute(
            released[:-1].rstrip() + ",\n\t" + extra_constraint + "\n)"
        )
        connection.execute("INSERT INTO users SELECT * FROM users_drift")
        connection.execute("DROP TABLE users_drift")
        connection.commit()
    finally:
        connection.close()

    before_bytes = path.read_bytes()
    before_rows = rows(path, "SELECT * FROM users ORDER BY id")
    before_ddl = rows(
        path, "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    )

    with pytest.raises(UpgradeRefused, match="建表语句不是已发布的 schema 1 定义"):
        await upgrade_database(path, now=FIXED_TIME)

    assert path.read_bytes() == before_bytes
    assert rows(path, "SELECT * FROM users ORDER BY id") == before_rows
    assert rows(
        path, "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    ) == before_ddl
    assert "status" not in [column[1] for column in rows(path, "PRAGMA table_info(users)")]
    assert rows(path, "SELECT schema_version FROM schema_meta") == [(1,)]
    assert backups(path) == []


async def test_source_users_ddl_missing_a_check_is_also_refused(tmp_path: Path) -> None:
    """少一个 CHECK 同样是文本漂移：源库结构既不能多也不能少。"""
    path = await make_schema_1(tmp_path / "v2.db")
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE users_drift AS SELECT * FROM users")
        connection.execute("DROP TABLE users")
        weakened = RELEASED_USERS_DDL.replace(
            "\tCONSTRAINT ck_users_delete_retry CHECK (delete_retry_count >= 0), \n", ""
        )
        assert weakened != RELEASED_USERS_DDL
        connection.execute(weakened)
        connection.execute("INSERT INTO users SELECT * FROM users_drift")
        connection.execute("DROP TABLE users_drift")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(UpgradeRefused, match="建表语句不是已发布的 schema 1 定义"):
        await upgrade_database(path, now=FIXED_TIME)
    assert rows(path, "SELECT schema_version FROM schema_meta") == [(1,)]
    assert backups(path) == []


async def test_source_users_ddl_with_only_whitespace_difference_is_accepted(
    tmp_path: Path,
) -> None:
    """规范化只压缩空白：同一段 DDL 换行/缩进不同不算漂移，避免误拒合法旧库。"""
    path = await make_schema_1(tmp_path / "v2.db")
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE users_drift AS SELECT * FROM users")
        connection.execute("DROP TABLE users")
        connection.execute(" ".join(RELEASED_USERS_DDL.split()))
        connection.execute("INSERT INTO users SELECT * FROM users_drift")
        connection.execute("DROP TABLE users_drift")
        connection.commit()
    finally:
        connection.close()

    report = await upgrade_database(path, now=FIXED_TIME)

    assert report.changed is True
    assert rows(path, "SELECT username, status FROM users ORDER BY id") == [
        ("admin", "active"),
        ("placeholder", "locked"),
        ("changed", "active"),
    ]


async def test_non_database_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "garbage.db"
    path.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(UpgradeRefused, match="无法读取数据库"):
        await upgrade_database(path, now=FIXED_TIME)


async def test_corrupt_database_is_refused(tmp_path: Path) -> None:
    path = await make_schema_1(tmp_path / "v2.db")
    payload = bytearray(path.read_bytes())
    for index in range(4096, 8192):
        payload[index] = 0xFF
    path.write_bytes(bytes(payload))

    with pytest.raises(UpgradeRefused, match="无法读取数据库"):
        await upgrade_database(path, now=FIXED_TIME)


async def test_integrity_rows_that_are_not_ok_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实 SQLite 一般直接报错；这里用边界替身断言「非 ok 行也拒绝」这一分支。"""
    path = tmp_path / "v2.db"
    path.write_bytes(b"stub")

    class _CorruptRow:
        def scalar_one(self) -> str:
            return "row 3 missing from index"

    class _CorruptConnection:
        async def exec_driver_sql(self, statement: str, parameters: object = None) -> object:
            assert "integrity_check" in statement
            return _CorruptRow()

    class _CorruptDatabase:
        def __init__(self, _path: Path) -> None:
            pass

        @asynccontextmanager
        async def connection(self) -> AsyncIterator[object]:
            yield _CorruptConnection()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(upgrade_module, "SQLiteDatabase", _CorruptDatabase)
    with pytest.raises(UpgradeRefused, match="完整性检查未通过"):
        await upgrade_database(path, now=FIXED_TIME)


async def test_backup_name_exhaustion_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "v2.db"
    path.write_bytes(b"")
    for index in range(100):
        suffix = "" if index == 0 else f"-{index}"
        (tmp_path / f"v2-pre-status-upgrade-20261126T083000Z{suffix}.db").write_bytes(b"")
    with pytest.raises(UpgradeRefused, match="备份文件名候选已用尽"):
        upgrade_module.backup_path_for(path, now=FIXED_TIME)


async def test_post_upgrade_validation_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = await make_schema_1(tmp_path / "v2.db")

    async def reject(_database: object) -> None:
        raise DomainError(ErrorCode.SCHEMA_MISMATCH, "结构与当前 V2 定义不一致")

    monkeypatch.setattr(upgrade_module, "validate_database", reject)
    with pytest.raises(UpgradeRefused, match="升级后结构校验失败"):
        await upgrade_database(path, now=FIXED_TIME)


def test_cli_says_nothing_about_locked_accounts_when_all_are_active(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = asyncio.run(_schema_1_with_real_hashes(tmp_path / "v2.db"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("ARIA2DECK_DATABASE_PATH", str(path))
    try:
        assert upgrade_module.main([]) == 0
    finally:
        monkeypatch.undo()
    out = capsys.readouterr().out
    assert "正常账号: 2，锁定账号: 0" in out
    assert "逐个重置密码" not in out


def test_module_entry_point_prints_usage() -> None:
    result = subprocess.run(  # noqa: S603  # 固定当前解释器与模块名
        [sys.executable, "-m", "aria2deck.infrastructure.sqlite.upgrade", "--help"],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "aria2deck.infrastructure.sqlite.upgrade" in result.stdout
    assert "ARIA2DECK_DATABASE_PATH" in result.stdout
    assert "schema 2" in result.stdout


async def _schema_1_with_real_hashes(path: Path) -> Path:
    """只含真实密码哈希的 schema 1 旧库：升级后不应出现任何锁定账号。"""
    await _fresh(path)
    connection = sqlite3.connect(path)
    try:
        for index, (username, password_hash) in enumerate(
            (("admin", REAL_HASH), ("second", SECOND_HASH)), start=1
        ):
            connection.execute(
                "INSERT INTO users (username, username_key, password_hash, is_admin, "
                "quota_bytes, is_initial_password, credential_generation, pending_delete, "
                "delete_retry_count, created_at_ms, updated_at_ms, status) "
                "VALUES (?, ?, ?, ?, 1000, 0, 1, 0, 0, 10, 10, 'active')",
                (username, username, password_hash, index == 1),
            )
            connection.execute(
                "INSERT INTO quota_accounts (user_id, used_bytes, reserved_bytes, revision, "
                "updated_at_ms) VALUES (?, 0, 0, 0, 0)",
                (index,),
            )
        connection.commit()
    finally:
        connection.close()
    _downgrade_to_schema_1(path)
    return path
