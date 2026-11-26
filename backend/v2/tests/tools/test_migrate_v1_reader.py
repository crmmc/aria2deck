"""单元 1：reader 只读打开、版本与完整性校验、五个表全量读出。"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from tests.tools.conftest import SampleLayout, create_v1_sample
from tools.migrate_v1 import MigrationAbort
from tools.migrate_v1.reader import open_v1, read_snapshot

READER_SOURCE = Path(__file__).resolve().parents[2] / "tools" / "migrate_v1" / "reader.py"


def test_reads_full_snapshot(v1_sample: SampleLayout) -> None:
    snapshot = read_snapshot(v1_sample.database)

    assert snapshot.version == 18
    assert snapshot.integrity_check == "ok"
    assert [user.username for user in snapshot.users] == ["alice", "bob", "carol", "dave"]
    assert {user.username: user.is_admin for user in snapshot.users}["alice"] is True
    assert {user.username: user.pending_delete for user in snapshot.users}["carol"] is True
    assert len(snapshot.stored_files) == 5
    assert len(snapshot.user_files) == 7
    assert len(snapshot.share_links) == 4
    assert snapshot.settings.site_title == "我的站点"
    assert snapshot.settings.pack_format == "zip"
    assert snapshot.settings.tracker_refresh_interval_minutes == 10


def test_share_password_presence_is_recorded_without_the_hash(
    v1_sample: SampleLayout,
) -> None:
    snapshot = read_snapshot(v1_sample.database)

    by_code = {share.share_code: share for share in snapshot.share_links}
    assert by_code["sharebbb"].has_password is True
    assert by_code["shareaaa"].has_password is False
    assert repr(snapshot).find("pbkdf2") == -1


def test_rejects_version_mismatch(v1_sample: SampleLayout) -> None:
    connection = sqlite3.connect(v1_sample.database)
    try:
        connection.execute("UPDATE schema_meta SET version = 17 WHERE id = 1")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(MigrationAbort) as raised:
        read_snapshot(v1_sample.database)

    assert "版本" in raised.value.message
    assert raised.value.details == ("当前版本 17，期望版本 18",)


def test_rejects_corrupted_database(tmp_path: Path) -> None:
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is definitely not a sqlite database" * 20)

    with pytest.raises(MigrationAbort) as raised:
        read_snapshot(broken)

    assert "损坏" in raised.value.message


def test_rejects_failed_integrity_check(
    v1_sample: SampleLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """磁盘级损坏（能打开、但 integrity_check 报错）无法稳定造出来，
    这里替换连接来验证我们把结果映射成中止而不是继续迁移。"""
    import sqlite3 as sqlite_module

    from tools.migrate_v1 import reader as reader_module

    class _Cursor:
        def __init__(self, rows: list[tuple[object, ...]]) -> None:
            self._rows = rows

        def fetchall(self) -> list[tuple[object, ...]]:
            return self._rows

    real_connect = sqlite_module.connect

    class _Loop:
        def __init__(self, connection: sqlite_module.Connection) -> None:
            self._connection = connection

        def execute(self, sql: str) -> object:
            if sql == "PRAGMA integrity_check":
                return _Cursor([("*** in database main ***",)])
            return self._connection.execute(sql)

        def __getattr__(self, name: str) -> object:
            return getattr(self._connection, name)

    monkeypatch.setattr(
        reader_module.sqlite3,
        "connect",
        lambda *args, **kwargs: _Loop(real_connect(*args, **kwargs)),
    )

    with pytest.raises(MigrationAbort) as raised:
        read_snapshot(v1_sample.database)

    assert raised.value.message == "V1 数据库完整性校验失败"
    assert raised.value.details == ("*** in database main ***",)


def test_rejects_missing_table(tmp_path: Path) -> None:
    layout = create_v1_sample(tmp_path / "v1")
    connection = sqlite3.connect(layout.database)
    try:
        connection.execute("DROP TABLE share_links")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(MigrationAbort) as raised:
        read_snapshot(layout.database)

    assert raised.value.message == "V1 数据库缺少迁移所需的表"
    assert "share_links" in raised.value.details


def test_rejects_missing_schema_meta_row(tmp_path: Path) -> None:
    layout = create_v1_sample(tmp_path / "v1")
    connection = sqlite3.connect(layout.database)
    try:
        connection.execute("DELETE FROM schema_meta")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(MigrationAbort) as raised:
        read_snapshot(layout.database)

    assert "schema_meta" in raised.value.message


def test_rejects_missing_settings_row(tmp_path: Path) -> None:
    layout = create_v1_sample(tmp_path / "v1")
    connection = sqlite3.connect(layout.database)
    try:
        connection.execute("DELETE FROM app_settings")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(MigrationAbort) as raised:
        read_snapshot(layout.database)

    assert "app_settings" in raised.value.message


def test_rejects_missing_database_file(tmp_path: Path) -> None:
    with pytest.raises(MigrationAbort) as raised:
        read_snapshot(tmp_path / "nope.db")

    assert "不存在" in raised.value.message


def test_open_v1_cannot_write(v1_sample: SampleLayout) -> None:
    with open_v1(v1_sample.database) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM users")


def _starts_with_sql_keyword(text: str) -> bool:
    """关键字后必须紧跟空白或括号，避免 `created_at_ms` 被误判成 `CREATE`。"""
    head = text.lstrip().upper()
    for keyword in _SQL_STARTS:
        if head.startswith(keyword) and head[len(keyword) :][:1] in {"", " ", "(", "\n", "\t"}:
            return True
    return False


_SQL_STARTS = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "PRAGMA",
    "CREATE",
    "DROP",
    "ALTER",
    "WITH",
    "REPLACE",
)


def test_every_sql_statement_is_read_only() -> None:
    """V1 侧只允许 SELECT / PRAGMA：扫源码里每条 SQL 语句字面量逐一断言。

    隐式字符串拼接在 AST 里就是一个 Constant，因此跨行写的整条语句也能被完整看到。
    """
    tree = ast.parse(READER_SOURCE.read_text(encoding="utf-8"))
    statements = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _starts_with_sql_keyword(node.value)
    ]

    assert len(statements) >= 6, f"扫到的 SQL 语句太少，断言可能空转：{len(statements)}"
    for statement in statements:
        assert statement.lstrip().upper().startswith(("SELECT", "PRAGMA")), statement


def test_snapshot_does_not_expose_password_hashes(v1_sample: SampleLayout) -> None:
    snapshot = read_snapshot(v1_sample.database)

    text = repr(snapshot)
    assert "pbkdf2_sha256$120000$aabbccdd$deadbeefdeadbeef" not in text
    assert "pbkdf2_sha256$120000$99887766$0badf00d0badf00d" not in text
    assert "gAAAAAB-v1-rpc-secret-ciphertext" not in text
