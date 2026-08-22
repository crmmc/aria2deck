"""M11 Task 4: migration v13 removes the task_backend_snapshots table.

- v12→v13 迁移后 ``task_backend_snapshots`` 表不存在；
- bootstrap 两条路径（升级/fresh）都不再重建该表；
- 升级路径的 ``PRAGMA wal_checkpoint(TRUNCATE)`` 发生在迁移事务提交之后，
  返回 busy==0；
- contract：生产源码中遗留快照表标识符只允许出现在 migrations.py；
- 旧 repo 与其测试文件已删除。
"""

from __future__ import annotations

import ast
import io
import re
import sqlite3
import tokenize
from pathlib import Path

import pytest
from sqlalchemy import event, text

from app.core.config import settings
from app.db.bootstrap import SCHEMA_VERSION, bootstrap_database
from app.db.engine import dispose_engine, get_engine, reset_engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND_ROOT / "app"

# token -> 匹配方式：表名/仓库名用子串（覆盖 ix_task_backend_snapshots_* 等
# 组合标识符），upsert_snapshot 用词边界（排除 sync 模块既有的
# _upsert_snapshot_row 观测仓写函数，它不触碰已删除的表）。
_LEGACY_TOKENS: dict[str, bool] = {
    "task_backend_snapshots": False,
    "backend_snapshots": False,
    "upsert_snapshot": True,
}
_ALLOWED_FILES = {"app/db/migrations.py"}

_V12_SNAPSHOT_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS task_backend_snapshots ("
    "global_download_id INTEGER NOT NULL PRIMARY KEY "
    "REFERENCES global_downloads (id) ON DELETE CASCADE, "
    "download_speed INTEGER NOT NULL DEFAULT 0, "
    "upload_speed INTEGER NOT NULL DEFAULT 0, "
    "total_length INTEGER NOT NULL DEFAULT 0, "
    "completed_length INTEGER NOT NULL DEFAULT 0, "
    "status VARCHAR(32) NOT NULL DEFAULT '', "
    "files_json TEXT NOT NULL DEFAULT '[]', "
    "raw_json TEXT NOT NULL DEFAULT '{}', "
    "updated_at_ms INTEGER NOT NULL)"
)


def _strip_comments_and_docstrings(source: str) -> str:
    tree = ast.parse(source)
    doc_spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            doc_spans.append((first.lineno, first.end_lineno))
    lines = source.splitlines(keepends=True)
    for start, end in doc_spans:
        for index in range(start - 1, end):
            lines[index] = "\n" if lines[index].endswith("\n") else ""
    without_docstrings = "".join(lines)
    return " ".join(
        token.string
        for token in tokenize.generate_tokens(
            io.StringIO(without_docstrings).readline
        )
        if token.type != tokenize.COMMENT
    )


def _contains_token(stripped_source: str, token: str, word_boundary: bool) -> bool:
    pattern = rf"\b{re.escape(token)}\b" if word_boundary else re.escape(token)
    return re.search(pattern, stripped_source) is not None


async def _sqlite_master_tables() -> set[str]:
    async with get_engine().connect() as conn:
        return {
            str(row[0])
            for row in (
                await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            ).all()
        }


async def _schema_version() -> int:
    async with get_engine().connect() as conn:
        return int(
            (
                await conn.execute(text("SELECT version FROM schema_meta WHERE id = 1"))
            ).scalar_one()
        )


async def _simulate_v12_database(tmp_path: Path) -> None:
    """在当前 schema 之上伪造 v12 部署：补建旧表并把版本钉回 12。"""
    await bootstrap_database()
    async with get_engine().begin() as conn:
        await conn.execute(text(_V12_SNAPSHOT_TABLE_DDL))
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_task_backend_snapshots_updated_at "
                "ON task_backend_snapshots (updated_at_ms)"
            )
        )
        await conn.execute(
            text("UPDATE schema_meta SET version = 12 WHERE id = 1")
        )


@pytest.mark.asyncio
async def test_v12_to_v13_migration_drops_task_backend_snapshots(
    tmp_path: Path,
) -> None:
    from app.db.migrations import migrate_v13

    db_path = str(tmp_path / "m11-v13.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE schema_meta ("
        "id INTEGER PRIMARY KEY, "
        "version INTEGER NOT NULL, "
        "created_at_ms INTEGER NOT NULL)"
    )
    conn.execute("INSERT INTO schema_meta VALUES (1, 12, 123)")
    conn.execute(
        "CREATE TABLE global_downloads ("
        "id INTEGER PRIMARY KEY, "
        "resource_key TEXT NOT NULL, "
        "resource_kind TEXT NOT NULL, "
        "source_uri TEXT NOT NULL, "
        "status TEXT NOT NULL, "
        "created_at_ms INTEGER NOT NULL, "
        "updated_at_ms INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE task_backend_snapshots ("
        "global_download_id INTEGER NOT NULL PRIMARY KEY "
        "REFERENCES global_downloads (id) ON DELETE CASCADE, "
        "updated_at_ms INTEGER NOT NULL)"
    )
    conn.commit()
    conn.close()

    original_db = settings.database_path
    settings.database_path = db_path
    reset_engine()
    try:
        async with get_engine().begin() as conn:
            await conn.execute(text("PRAGMA foreign_keys=ON"))
            await migrate_v13(conn)
            tables = {
                str(row[0])
                for row in (
                    await conn.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table'")
                    )
                ).all()
            }
            version = (
                await conn.execute(
                    text("SELECT version FROM schema_meta WHERE id = 1")
                )
            ).scalar_one()
        assert "task_backend_snapshots" not in tables
        assert int(version) == 13

        # 幂等：重复执行不报错、状态不变
        async with get_engine().begin() as conn:
            await migrate_v13(conn)
        assert "task_backend_snapshots" not in await _sqlite_master_tables()
        assert await _schema_version() == 13
    finally:
        await dispose_engine()
        reset_engine()
        settings.database_path = original_db


@pytest.mark.asyncio
async def test_fresh_bootstrap_does_not_recreate_snapshot_table(
    temp_db: str,
) -> None:
    tables = await _sqlite_master_tables()
    assert "task_backend_snapshots" not in tables
    assert await _schema_version() == SCHEMA_VERSION == 15


@pytest.mark.asyncio
async def test_upgrade_bootstrap_does_not_recreate_snapshot_table(
    tmp_path: Path,
) -> None:
    original_db = settings.database_path
    settings.database_path = str(tmp_path / "m11-upgrade.db")
    reset_engine()
    try:
        await _simulate_v12_database(tmp_path)
        assert "task_backend_snapshots" in await _sqlite_master_tables()

        await bootstrap_database()

        assert "task_backend_snapshots" not in await _sqlite_master_tables()
        assert await _schema_version() == SCHEMA_VERSION == 15
    finally:
        await dispose_engine()
        reset_engine()
        settings.database_path = original_db


@pytest.mark.asyncio
async def test_migration_wal_checkpoint_runs_after_migration_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.db.bootstrap as bootstrap_mod

    original_db = settings.database_path
    settings.database_path = str(tmp_path / "m11-checkpoint.db")
    reset_engine()
    try:
        await _simulate_v12_database(tmp_path)

        events: list[str] = []
        checkpoint_results: list[int | None] = []
        original_checkpoint = getattr(
            bootstrap_mod, "_truncate_wal_after_bootstrap", None
        )

        async def _checkpoint_spy() -> int | None:
            events.append("checkpoint")
            if original_checkpoint is None:
                return None
            busy = await original_checkpoint()
            checkpoint_results.append(busy)
            return busy

        monkeypatch.setattr(
            bootstrap_mod, "_truncate_wal_after_bootstrap", _checkpoint_spy,
            raising=False,
        )

        sync_engine = get_engine().sync_engine

        @event.listens_for(sync_engine, "commit")
        def _on_commit(_conn) -> None:
            events.append("commit")

        @event.listens_for(sync_engine, "after_cursor_execute")
        def _on_cursor_execute(
            _conn, _cursor, statement, _parameters, _context, _executemany
        ) -> None:
            if "DROP TABLE" in statement and "task_backend_snapshots" in statement:
                events.append("drop-snapshots")

        try:
            await bootstrap_database()
        finally:
            event.remove(sync_engine, "commit", _on_commit)
            event.remove(sync_engine, "after_cursor_execute", _on_cursor_execute)

        assert "drop-snapshots" in events, "v13 迁移未删除 task_backend_snapshots"
        assert "checkpoint" in events, "升级路径缺少迁移后 WAL checkpoint"
        drop_index = events.index("drop-snapshots")
        checkpoint_index = events.index("checkpoint")
        commits_after_drop = [
            index
            for index, item in enumerate(events[:checkpoint_index])
            if item == "commit" and index > drop_index
        ]
        assert commits_after_drop, (
            "wal_checkpoint 必须发生在迁移事务提交之后："
            f"events={events}"
        )
        assert checkpoint_results == [0], "迁移后 WAL checkpoint 应返回 busy==0"
    finally:
        await dispose_engine()
        reset_engine()
        settings.database_path = original_db


def test_legacy_snapshot_contract_is_confined_to_migrations() -> None:
    offenders: set[str] = set()
    for path in sorted(APP_ROOT.rglob("*.py")):
        stripped = _strip_comments_and_docstrings(
            path.read_text(encoding="utf-8")
        )
        if any(
            _contains_token(stripped, token, word_boundary)
            for token, word_boundary in _LEGACY_TOKENS.items()
        ):
            offenders.add(str(path.relative_to(BACKEND_ROOT)))
    assert offenders == _ALLOWED_FILES, (
        "task_backend_snapshots 生产接线应只剩历史迁移 DDL："
        f"offenders={sorted(offenders)}"
    )


def test_legacy_snapshot_repo_and_tests_are_removed() -> None:
    assert not (APP_ROOT / "repositories" / "backend_snapshots.py").exists()
    assert not (BACKEND_ROOT / "tests" / "test_backend_snapshots_repo_v9.py").exists()
