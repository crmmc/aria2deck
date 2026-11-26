"""把已发布的 schema 1 数据库原地升级到 schema 2（新增 `users.status`）。

只做一件事：应用停止后，校验源结构与完整性 → 生成不覆盖旧文件的备份 →
在同一事务里重建 `users` 表并回填账号状态 → 提交后用现有结构校验确认结果。

不从应用启动流程调用；重复执行只做验证并报告，不重新回填，避免把已经解锁的账号再次锁定。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from ...domain.errors import DomainError
from ..instance_lock import InstanceLock
from .bootstrap import (
    SchemaSnapshot,
    # 与 validate_database 共用同一套规范化：两条路径对「同一份结构」必须给出同一结论。
    _expected_snapshot,
    _normalized_sql,
    _snapshot,
    validate_database,
)
from .engine import SQLiteDatabase
from .schema import SCHEMA_VERSION, create_table_statement, users

# 与离线迁移工具 planner.PLACEHOLDER_PASSWORD 是同一个固定串；两边各自保留兼容识别，
# 由测试锚定口径一致，运行时代码不导入离线迁移包。
MIGRATED_PLACEHOLDER_PASSWORD = "!migrated-placeholder!"  # noqa: S105  # 占位串，不是密钥

# 上一版发布时 `users` 的建表语句原文（schema 1，没有 status 列与状态约束）。
# 升级只接受规范化后等于这段文本的源表：多一个 CHECK、少一个约束、改一个约束名
# 都属于异常结构，因为重建时 DROP TABLE 会把它们一并抹掉，等于悄悄改写用户库的结构。
RELEASED_SCHEMA_1_USERS_DDL = (
    "CREATE TABLE users (\n"
    "\tid INTEGER NOT NULL, \n"
    "\tusername TEXT NOT NULL, \n"
    "\tusername_key TEXT NOT NULL, \n"
    "\tpassword_hash TEXT NOT NULL, \n"
    "\tis_admin BOOLEAN NOT NULL, \n"
    "\tquota_bytes BIGINT NOT NULL, \n"
    "\tis_initial_password BOOLEAN NOT NULL, \n"
    "\trpc_secret_ciphertext TEXT, \n"
    "\trpc_secret_prefix TEXT, \n"
    "\tcredential_generation INTEGER NOT NULL, \n"
    "\tpending_delete BOOLEAN NOT NULL, \n"
    "\tdelete_lease_owner TEXT, \n"
    "\tdelete_lease_expires_at_ms BIGINT, \n"
    "\tdelete_retry_count INTEGER NOT NULL, \n"
    "\tcreated_at_ms BIGINT NOT NULL, \n"
    "\tupdated_at_ms BIGINT NOT NULL, \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT uq_users_username_key UNIQUE (username_key), \n"
    "\tCONSTRAINT ck_users_quota_nonnegative CHECK (quota_bytes >= 0), \n"
    "\tCONSTRAINT ck_users_credential_generation CHECK (credential_generation >= 1), \n"
    "\tCONSTRAINT ck_users_delete_retry CHECK (delete_retry_count >= 0), \n"
    "\tCONSTRAINT ck_users_rpc_secret_pair CHECK ((rpc_secret_ciphertext IS NULL) = (rpc_secret_prefix IS NULL)), \n"
    "\tCONSTRAINT ck_users_delete_lease_pair CHECK ((delete_lease_owner IS NULL) = (delete_lease_expires_at_ms IS NULL))\n"
    ")"
)
RELEASED_SCHEMA_1_USERS_SQL = _normalized_sql(RELEASED_SCHEMA_1_USERS_DDL)
SOURCE_VERSION = 1
_USERS = "users"
_STATUS = "status"
_TEMP_TABLE = "_users_schema_1"
_BACKUP_SUFFIX = "-pre-status-upgrade"
_MAX_BACKUP_CANDIDATES = 100


class UpgradeRefused(RuntimeError):
    """源库不满足升级前提，或升级过程失败；调用方必须把库当成未升级。"""


@dataclass(frozen=True, slots=True)
class UpgradeReport:
    version: int
    active_accounts: int
    locked_accounts: int
    backup_path: Path | None
    changed: bool


def _refuse(message: str) -> UpgradeRefused:
    return UpgradeRefused(message)


def _instance_lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


async def _read_version(connection: AsyncConnection) -> int:
    try:
        row = (
            await connection.exec_driver_sql(
                "SELECT schema_version FROM schema_meta WHERE id = 1"
            )
        ).first()
    except SQLAlchemyError as error:
        raise _refuse(f"无法读取 schema 版本：{error}") from error
    if row is None:
        raise _refuse("schema_meta 缺少单行版本记录")
    return int(row[0])


def _ensure_source_structure(actual: SchemaSnapshot, expected: SchemaSnapshot) -> None:
    """schema 1 与目标结构的唯一差异只能是 `users.status` 缺席，其他一律按漂移拒绝。"""
    if set(actual.tables) != set(expected.tables):
        raise _refuse("数据库表集合与预期不一致")
    if actual.indexes != expected.indexes or actual.triggers != expected.triggers:
        raise _refuse("数据库索引或触发器与预期不一致")
    for name, expected_table in expected.tables.items():
        actual_table = actual.tables[name]
        if name != _USERS:
            if actual_table != expected_table:
                raise _refuse(f"表 {name} 的结构与预期不一致")
            continue
        without_status = tuple(
            column for column in expected_table.columns if column.name != _STATUS
        )
        if actual_table.columns != without_status:
            raise _refuse("users 表不是预期的 schema 1 结构")
        if actual_table.foreign_keys != expected_table.foreign_keys:
            raise _refuse("users 表外键与预期不一致")
        # 列与外键都由建表语句派生，能通过上面两关仍可能多出 CHECK 或改写约束文本；
        # 逐字比对已发布 DDL 才能保证重建后不悄悄丢弃源库上的任何结构。
        if actual_table.sql != RELEASED_SCHEMA_1_USERS_SQL:
            raise _refuse("users 表建表语句不是已发布的 schema 1 定义")


async def _inspect(path: Path) -> tuple[int, SchemaSnapshot]:
    database = SQLiteDatabase(path)
    try:
        try:
            async with database.connection() as connection:
                integrity = (
                    await connection.exec_driver_sql("PRAGMA integrity_check")
                ).scalar_one()
                if str(integrity).strip().lower() != "ok":
                    raise _refuse(f"SQLite 完整性检查未通过：{integrity}")
                return await _read_version(connection), await _snapshot(connection)
        except SQLAlchemyError as error:
            raise _refuse(f"无法读取数据库：{error}") from error
    finally:
        await database.close()


def backup_path_for(path: Path, *, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    for index in range(_MAX_BACKUP_CANDIDATES):
        suffix = "" if index == 0 else f"-{index}"
        candidate = path.with_name(f"{path.stem}{_BACKUP_SUFFIX}-{stamp}{suffix}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise _refuse("备份文件名候选已用尽，请先整理数据目录")


def create_backup(path: Path, *, now: datetime | None = None) -> Path:
    """用 SQLite 备份 API 生成独立备份；WAL 内容一并落进单文件，不覆盖已有文件。"""
    target = backup_path_for(path, now=now)
    try:
        source = sqlite3.connect(path)
        try:
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
                destination.execute("PRAGMA journal_mode = delete")
            finally:
                destination.close()
        finally:
            source.close()
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


async def _apply_schema_2(connection: AsyncConnection) -> None:
    await connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
    await connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        await connection.exec_driver_sql(
            f"CREATE TABLE {_TEMP_TABLE} AS SELECT * FROM {_USERS}"  # noqa: S608  # 表名来自内部常量
        )
        await connection.exec_driver_sql(f"DROP TABLE {_USERS}")  # noqa: S608
        await connection.exec_driver_sql(create_table_statement(_USERS))
        columns = [column.name for column in users.columns]
        select_columns = ", ".join(
            (
                "CASE WHEN password_hash = :placeholder THEN 'locked' ELSE 'active' END"
                if column == _STATUS
                else column
            )
            for column in columns
        )
        statement = (
            f"INSERT INTO {_USERS} ({', '.join(columns)}) "  # noqa: S608  # 内部常量，值走绑定参数
            f"SELECT {select_columns} FROM {_TEMP_TABLE}"
        )
        await connection.exec_driver_sql(
            statement, {"placeholder": MIGRATED_PLACEHOLDER_PASSWORD}
        )
        await connection.exec_driver_sql(f"DROP TABLE {_TEMP_TABLE}")  # noqa: S608
        await connection.exec_driver_sql(
            "UPDATE schema_meta SET schema_version = :version WHERE id = 1",
            {"version": SCHEMA_VERSION},
        )
        broken = (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all()
        if broken:
            raise _refuse(f"外键校验失败（{len(broken)} 行）")
        await connection.commit()
    except BaseException:
        await connection.rollback()
        raise


async def _counts(path: Path) -> tuple[int, int]:
    database = SQLiteDatabase(path)
    try:
        async with database.connection() as connection:
            rows = (
                await connection.exec_driver_sql(
                    "SELECT status, COUNT(*) FROM users GROUP BY status"
                )
            ).all()
    finally:
        await database.close()
    counted = {str(status): int(count) for status, count in rows}
    return counted.get("active", 0), counted.get("locked", 0)


async def _validate(path: Path) -> None:
    database = SQLiteDatabase(path)
    try:
        await validate_database(database)
    except DomainError as error:
        raise _refuse(f"升级后结构校验失败：{error}") from error
    finally:
        await database.close()


async def upgrade_database(path: Path, *, now: datetime | None = None) -> UpgradeReport:
    if not path.is_file():
        raise _refuse(f"数据库文件不存在：{path}")
    lock = InstanceLock(_instance_lock_path(path))
    try:
        lock.acquire()
    except DomainError as error:
        raise _refuse(f"无法取得实例锁，应用可能仍在运行：{error}") from error
    try:
        version, snapshot = await _inspect(path)
        if version == SCHEMA_VERSION:
            await _validate(path)
            active, locked = await _counts(path)
            return UpgradeReport(version, active, locked, None, False)
        if version != SOURCE_VERSION:
            raise _refuse(f"不支持的 schema 版本：{version}")
        expected = await _expected_snapshot()
        _ensure_source_structure(snapshot, expected)
        try:
            backup = create_backup(path, now=now)
        except (OSError, sqlite3.Error) as error:
            raise _refuse(f"备份失败，未写入数据库：{error}") from error
        database = SQLiteDatabase(path)
        try:
            async with database.connection() as connection:
                await _apply_schema_2(connection)
        except Exception as error:
            raise _refuse(f"升级失败，已回滚：{error}") from error
        finally:
            await database.close()
        await _validate(path)
        active, locked = await _counts(path)
        return UpgradeReport(SCHEMA_VERSION, active, locked, backup, True)
    finally:
        lock.release()


def _database_path() -> Path:
    value = os.environ.get("ARIA2DECK_DATABASE_PATH")
    if value is None or not value.strip():
        raise _refuse("未指定数据库：请设置 ARIA2DECK_DATABASE_PATH")
    return Path(value).expanduser()


def run(path: Path, *, now: datetime | None = None) -> UpgradeReport:
    return asyncio.run(upgrade_database(path, now=now))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aria2deck.infrastructure.sqlite.upgrade",
        description=(
            "把已发布的 schema 1 数据库升级到 schema 2（users.status）。\n"
            "数据库路径取自 ARIA2DECK_DATABASE_PATH；必须先停止应用，"
            "升级前自动生成不覆盖旧文件的备份。"
        ),
    )
    parser.parse_args(argv)
    try:
        report = run(_database_path())
    except (UpgradeRefused, DomainError) as error:
        print(f"升级未执行：{error}", file=sys.stderr)
        return 1
    if report.backup_path is not None:
        print(f"升级前备份: {report.backup_path}")
        print("数据库已升级到 schema 2。")
    else:
        print("数据库已是 schema 2，无需升级。")
    print(f"schema 版本: {report.version}")
    print(
        f"正常账号: {report.active_accounts}，锁定账号: {report.locked_accounts}"
    )
    if report.locked_accounts:
        print("锁定账号需要管理员在「用户管理」逐个重置密码后才能登录。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
