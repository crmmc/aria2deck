"""单元 4：建 V2 库并写入——外键顺序、ID 映射、占位密码、配额重算、设置映射。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from aria2deck.domain.content import ContentKind, content_identity
from aria2deck.infrastructure.security import CredentialManager
from aria2deck.infrastructure.sqlite.bootstrap import validate_database
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from tests.tools.conftest import (
    V1_RPC_SECRET,
    V1_RPC_SECRET_ENCRYPTED,
    V1_SHARE_PASSWORD_HASH,
    V1_USER_PASSWORD_HASH,
    SampleLayout,
)
from tools.migrate_v1.contents import migrate_contents
from tools.migrate_v1.planner import PLACEHOLDER_PASSWORD, build_plan, finalize
from tools.migrate_v1.reader import read_snapshot
from tools.migrate_v1.settings_map import (
    DROPPED_V1_SETTINGS,
    map_settings,
    split_trackers,
)
from tools.migrate_v1.writer import read_back, write_v2


def _fetch(v2_root: Path, select_expression: object) -> list[tuple[object, ...]]:
    from sqlalchemy import select

    async def run() -> list[tuple[object, ...]]:
        database = SQLiteDatabase(v2_root / "data" / "v2.db")
        try:
            async with database.connection() as connection:
                rows = (await connection.execute(select_expression)).all()  # type: ignore[arg-type]
                return [tuple(row) for row in rows]
        finally:
            await database.close()

    assert select is not None
    return asyncio.run(run())


def _prepare(layout: SampleLayout, v2_root: Path):
    snapshot = read_snapshot(layout.database)
    plan = build_plan(snapshot, layout.download_dir)
    mapped = map_settings(snapshot.settings)
    migration = migrate_contents(plan, v2_root, dry_run=False)
    write = finalize(plan, migration)
    return plan, mapped, write


def _run(layout: SampleLayout, v2_root: Path):
    plan, mapped, write = _prepare(layout, v2_root)
    result = asyncio.run(write_v2(v2_root, write, mapped.settings))
    observed = asyncio.run(read_back(v2_root))
    return plan, mapped, write, result, observed


@pytest.fixture
def migrated(v1_sample: SampleLayout, tmp_path: Path):
    return _run(v1_sample, tmp_path / "v2")


def test_database_passes_v2_structure_validation(
    v1_sample: SampleLayout, migrated, tmp_path: Path
) -> None:
    database = SQLiteDatabase(tmp_path / "v2" / "data" / "v2.db")
    asyncio.run(validate_database(database))
    asyncio.run(database.close())


def test_row_counts_match_the_write_plan(v1_sample: SampleLayout, migrated) -> None:
    _, _, write, _, observed = migrated

    assert observed.counts["users"] == len(write.users) == 3
    assert observed.counts["user_files"] == len(write.user_files)
    assert observed.counts["share_links"] == len(write.shares)
    assert observed.counts["content_objects"] == len(write.contents)


def test_id_mapping_covers_every_entity(v1_sample: SampleLayout, migrated) -> None:
    _, _, write, result, _ = migrated

    assert set(result.user_ids) == {user.v1_id for user in write.users}
    assert set(result.content_ids) == {content.content_key for content in write.contents}
    assert set(result.user_file_ids) == {row.v1_id for row in write.user_files}


def test_placeholder_password_cannot_authenticate(v1_sample: SampleLayout, migrated) -> None:
    """用 V2 真实的 verify_password 断言占位串不可登录。"""
    credentials = CredentialManager(pepper=None, debug=True)
    password_hash = credentials.derive_password_hash(username="alice", password="anything")  # noqa: S106

    assert credentials.verify_password(password_hash, PLACEHOLDER_PASSWORD) is False
    assert credentials.verify_password("x" * 20, PLACEHOLDER_PASSWORD) is False


def test_users_are_marked_as_initial_password(v1_sample: SampleLayout, tmp_path: Path) -> None:
    _run(v1_sample, tmp_path / "v2")

    async def load() -> list[dict[str, object]]:
        from sqlalchemy import select

        from aria2deck.infrastructure.sqlite.schema import users

        database = SQLiteDatabase(tmp_path / "v2" / "data" / "v2.db")
        try:
            async with database.connection() as connection:
                rows = (await connection.execute(select(users))).mappings().all()
                return [dict(row) for row in rows]
        finally:
            await database.close()

    rows = asyncio.run(load())
    assert [row["username"] for row in rows] == ["alice", "bob", "dave"]
    assert all(row["is_initial_password"] == 1 for row in rows)
    # 引导管理员拿真实哈希，其余账号仍是占位串
    placeholder = {str(row["username"]) for row in rows if row["password_hash"] == PLACEHOLDER_PASSWORD}
    assert placeholder == {"bob", "dave"}
    assert all(row["rpc_secret_ciphertext"] is None for row in rows)
    assert {str(row["username_key"]) for row in rows} == {"alice", "bob", "dave"}


def test_admin_flag_and_quota_are_preserved(v1_sample: SampleLayout, tmp_path: Path) -> None:
    _run(v1_sample, tmp_path / "v2")

    async def load() -> list[tuple[object, ...]]:
        from sqlalchemy import select

        from aria2deck.infrastructure.sqlite.schema import users

        database = SQLiteDatabase(tmp_path / "v2" / "data" / "v2.db")
        try:
            async with database.connection() as connection:
                return [
                    tuple(row)
                    for row in (
                        await connection.execute(
                            select(users.c.username, users.c.is_admin, users.c.quota_bytes)
                        )
                    ).all()
                ]
        finally:
            await database.close()

    rows = asyncio.run(load())
    assert rows[0][1] == 1
    assert rows[0][2] == 10 * 1024**3
    assert rows[1][1] == 0


def test_share_passwords_are_dropped(v1_sample: SampleLayout, tmp_path: Path) -> None:
    plan, _, _ = _prepare(v1_sample, tmp_path / "v2")

    async def load() -> list[tuple[object, ...]]:
        from sqlalchemy import select

        from aria2deck.infrastructure.sqlite.schema import share_links

        database = SQLiteDatabase(tmp_path / "v2" / "data" / "v2.db")
        try:
            async with database.connection() as connection:
                return [
                    tuple(row)
                    for row in (
                        await connection.execute(
                            select(
                                share_links.c.code,
                                share_links.c.password_hash,
                                share_links.c.file_name,
                                share_links.c.file_kind,
                                share_links.c.download_count,
                                share_links.c.revoked_at_ms,
                            )
                        )
                    ).all()
                ]
        finally:
            await database.close()

    _run(v1_sample, tmp_path / "v2")
    rows = asyncio.run(load())
    assert {row[0] for row in rows} == {share.code for share in plan.shares}
    assert all(row[1] is None for row in rows)


def test_share_snapshot_is_filled_from_user_file(v1_sample: SampleLayout, tmp_path: Path) -> None:
    plan, _, write = _prepare(v1_sample, tmp_path / "v2")
    written = asyncio.run(write_v2(tmp_path / "v2", write, map_settings(read_snapshot(v1_sample.database).settings).settings))
    del written

    async def load() -> dict[str, tuple[object, ...]]:
        from sqlalchemy import select

        from aria2deck.infrastructure.sqlite.schema import content_objects, share_links

        database = SQLiteDatabase(tmp_path / "v2" / "data" / "v2.db")
        try:
            async with database.connection() as connection:
                rows = (
                    await connection.execute(
                        select(
                            share_links.c.code,
                            share_links.c.file_name,
                            share_links.c.file_size,
                            share_links.c.file_kind,
                            share_links.c.content_key,
                        )
                    )
                ).all()
                objects = (
                    await connection.execute(
                        select(content_objects.c.content_key, content_objects.c.size, content_objects.c.kind)
                    )
                ).all()
                return {str(row[0]): tuple(row) for row in rows} | {
                    f"object:{row[0]}": tuple(row) for row in objects
                }
        finally:
            await database.close()

    rows = asyncio.run(load())
    share = rows["shareaaa"]
    assert share[1] == "shared.bin"
    assert rows[f"object:{share[4]}"][1] == share[2]
    assert rows[f"object:{share[4]}"][2] == share[3]


def test_quota_is_recomputed_from_user_files(v1_sample: SampleLayout, migrated) -> None:
    plan, _, write, result, observed = migrated

    by_user = {user.v1_id: user for user in plan.users}
    for v1_id, used in write.used_bytes.items():
        assert observed.quota_used[result.user_ids[v1_id]] == used
        assert used == sum(
            next(c.size for c in write.contents if c.content_key == row.content_key)
            for row in write.user_files
            if row.v1_user_id == v1_id
        )
        assert used >= 0
        assert by_user[v1_id].quota_bytes >= 0


def test_directory_entries_are_written(v1_sample: SampleLayout, tmp_path: Path) -> None:
    _run(v1_sample, tmp_path / "v2")

    async def load() -> list[tuple[object, ...]]:
        from sqlalchemy import select

        from aria2deck.infrastructure.sqlite.schema import content_entries

        database = SQLiteDatabase(tmp_path / "v2" / "data" / "v2.db")
        try:
            async with database.connection() as connection:
                return [
                    tuple(row)
                    for row in (
                        await connection.execute(
                            select(
                                content_entries.c.path,
                                content_entries.c.parent_path,
                                content_entries.c.name,
                                content_entries.c.entry_kind,
                                content_entries.c.size,
                            )
                        )
                    ).all()
                ]
        finally:
            await database.close()

    rows = asyncio.run(load())
    paths = {str(row[0]) for row in rows}
    assert paths == {"nested", "nested/two.txt", "one.txt"}
    assert {str(row[1]) for row in rows if str(row[0]) == "nested/two.txt"} == {"nested"}


def test_settings_columns_match_v1(v1_sample: SampleLayout, migrated) -> None:
    snapshot = read_snapshot(v1_sample.database)
    _, mapped, _, _, observed = migrated
    settings = observed.settings

    assert settings["site_title"] == snapshot.settings.site_title
    assert settings["aria2_rpc_url"] == snapshot.settings.aria2_rpc_url
    assert settings["pack_format"] == snapshot.settings.pack_format
    assert settings["pack_compression_level"] == snapshot.settings.pack_compression_level
    assert settings["history_retention_days"] == snapshot.settings.history_retention_days
    assert settings["max_task_size"] == snapshot.settings.max_task_size_bytes
    assert settings["min_free_disk"] == snapshot.settings.min_free_disk_bytes
    assert settings["authenticated_api_rate_limit"] == snapshot.settings.authenticated_api_rate_limit
    assert settings["account_security_rate_limit"] == snapshot.settings.account_security_rate_limit
    assert settings["aria2_test_rate_limit"] == snapshot.settings.aria2_test_rate_limit
    assert settings["ws_reconnect_max_delay_seconds"] == 30
    assert settings["ws_reconnect_jitter_seconds"] == 15
    assert settings["ws_reconnect_factor"] == 3
    assert settings["tracker_refresh_interval_minutes"] == 10
    assert json.loads(str(settings["tracker_fixed_list_json"])) == [
        "udp://tracker.one:6969/announce",
        "http://tracker.two/announce",
    ]
    assert json.loads(str(settings["tracker_remote_urls_json"])) == [
        "https://tracker.three/list"
    ]
    assert json.loads(str(settings["hidden_file_extensions_json"])) == [".part"]
    assert settings["aria2_rpc_secret_ciphertext"] is None
    assert mapped.fallbacks == ()


def test_settings_do_not_leak_secrets_into_the_database(
    v1_sample: SampleLayout, migrated
) -> None:
    _, _, _, _, observed = migrated

    dump = repr(observed.settings)
    assert V1_RPC_SECRET not in dump
    assert V1_RPC_SECRET_ENCRYPTED not in dump
    assert V1_USER_PASSWORD_HASH not in dump
    assert V1_SHARE_PASSWORD_HASH not in dump


def test_dropped_settings_are_listed(v1_sample: SampleLayout) -> None:
    outcome = map_settings(read_snapshot(v1_sample.database).settings)

    assert outcome.dropped == DROPPED_V1_SETTINGS
    assert "rate_limit_anonymous_download" in outcome.dropped
    assert "download_total_connections" in outcome.dropped
    assert "aria2_rpc_secret" in outcome.dropped


@pytest.mark.parametrize(
    ("column", "value", "field"),
    [
        ("pack_format", "rar", "pack_format"),
        ("pack_compression_level", 99, "pack_compression_level"),
        ("history_retention_days", 0, "history_retention_days"),
        ("tracker_refresh_interval_minutes", 3, "tracker_refresh_interval_minutes"),
    ],
)
def test_out_of_range_settings_fall_back_to_defaults(
    v1_sample: SampleLayout, column: str, value: object, field: str
) -> None:
    import dataclasses

    from aria2deck.domain.settings import SystemSettings

    snapshot = read_snapshot(v1_sample.database)
    patched = dataclasses.replace(snapshot, settings=dataclasses.replace(snapshot.settings, **{column: value}))

    outcome = map_settings(patched.settings)

    assert getattr(outcome.settings, field) == getattr(SystemSettings(), field)
    assert any(field in item for item in outcome.fallbacks)


def test_jitter_is_converted_from_ratio_to_seconds(v1_sample: SampleLayout) -> None:
    import dataclasses

    snapshot = read_snapshot(v1_sample.database)
    patched = dataclasses.replace(snapshot.settings, ws_reconnect_jitter="0.25", ws_reconnect_max_delay=40)

    outcome = map_settings(patched)

    assert outcome.settings.ws_reconnect_jitter_seconds == 10


def test_invalid_jitter_and_factor_fall_back(v1_sample: SampleLayout) -> None:
    import dataclasses

    snapshot = read_snapshot(v1_sample.database)
    patched = dataclasses.replace(
        snapshot.settings, ws_reconnect_jitter="abc", ws_reconnect_factor="oops"
    )

    outcome = map_settings(patched)

    # jitter 无法解析时按 0 处理，max(round(0), 1) = 1 秒，属于合法值不回退；
    # 只有 factor 越界才进回退清单。
    assert outcome.settings.ws_reconnect_jitter_seconds == 1
    assert outcome.settings.ws_reconnect_factor == 2
    assert outcome.fallbacks == (
        "ws_reconnect_factor（WebSocket 重连倍数无效），已回退为 V2 默认值",
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\nb", ("a", "b")),
        ("a,b", ("a", "b")),
        (" a ,, b \n", ("a", "b")),
        ("a\na", ("a",)),
        ("", ()),
    ],
)
def test_tracker_text_is_split_into_entries(raw: str, expected: tuple[str, ...]) -> None:
    assert split_trackers(raw) == expected


def test_written_content_objects_are_active(
    v1_sample: SampleLayout, tmp_path: Path
) -> None:
    _run(v1_sample, tmp_path / "v2")

    async def load() -> list[tuple[object, ...]]:
        from sqlalchemy import select

        from aria2deck.infrastructure.sqlite.schema import content_objects

        database = SQLiteDatabase(tmp_path / "v2" / "data" / "v2.db")
        try:
            async with database.connection() as connection:
                return [
                    tuple(row)
                    for row in (
                        await connection.execute(
                            select(
                                content_objects.c.content_key,
                                content_objects.c.kind,
                                content_objects.c.digest,
                                content_objects.c.size,
                                content_objects.c.storage_path,
                                content_objects.c.deletion_state,
                                content_objects.c.read_leases,
                            )
                        )
                    ).all()
                ]
        finally:
            await database.close()

    rows = asyncio.run(load())
    for row in rows:
        assert str(row[0]) == f"{row[1]}:{row[2]}"
        assert str(row[4]) == f"objects/{row[1]}-{row[2]}"
        assert row[5] == "active"
        assert row[6] == 0
        assert content_identity(ContentKind(str(row[1])), str(row[2]))

# ------------------------------------- 引导管理员的真实密码（迁移后唯一能登录的入口）


def _user_rows(v2_root: Path) -> dict[str, tuple[str, bool]]:
    """读出「用户名 → (存库哈希, is_initial_password)」。"""
    from sqlalchemy import select

    from aria2deck.infrastructure.sqlite.schema import users

    rows = _fetch(
        v2_root, select(users.c.username, users.c.password_hash, users.c.is_initial_password)
    )
    return {str(row[0]): (str(row[1]), bool(row[2])) for row in rows}


def _submitted_hash(credentials: CredentialManager, username: str, password: str) -> str:
    """模拟前端：先在本地把明文派生一次，再把 64 位摘要提交给后端登录。"""
    return credentials.derive_password_hash(username=username, password=password)


def test_generated_password_logs_the_bootstrap_admin_in(
    v1_sample: SampleLayout, tmp_path: Path
) -> None:
    credentials = CredentialManager(pepper=None, debug=True)
    _, mapped, write = _prepare(v1_sample, tmp_path / "v2")
    result = asyncio.run(write_v2(tmp_path / "v2", write, mapped.settings))

    assert result.bootstrap_admin is not None
    username, password = result.bootstrap_admin
    stored, is_initial = _user_rows(tmp_path / "v2")[username]

    submitted = _submitted_hash(credentials, username, password)
    assert credentials.verify_password(submitted, stored) is True
    # 登录后应看到改密横幅
    assert is_initial is True


def test_generated_password_is_at_least_24_chars_and_unique(
    v1_sample: SampleLayout, tmp_path: Path
) -> None:
    _, mapped, write = _prepare(v1_sample, tmp_path / "v2")
    result = asyncio.run(write_v2(tmp_path / "v2", write, mapped.settings))
    second = asyncio.run(write_v2(tmp_path / "v2b", write, mapped.settings))

    assert result.bootstrap_admin is not None
    assert second.bootstrap_admin is not None
    username, password = result.bootstrap_admin
    _, other_password = second.bootstrap_admin

    assert username == "alice"
    assert len(password) >= 24
    assert password != other_password


@pytest.mark.parametrize("candidate", ["", "anything", PLACEHOLDER_PASSWORD, "a" * 24])
def test_placeholder_accounts_reject_every_password(
    v1_sample: SampleLayout, tmp_path: Path, candidate: str
) -> None:
    """其余账号是占位串：任何输入（含占位串本身）都登录不了。"""
    credentials = CredentialManager(pepper=None, debug=True)
    _, mapped, write = _prepare(v1_sample, tmp_path / "v2")
    asyncio.run(write_v2(tmp_path / "v2", write, mapped.settings))
    rows = _user_rows(tmp_path / "v2")

    for username in ("bob", "dave"):
        stored, is_initial = rows[username]
        assert stored == PLACEHOLDER_PASSWORD
        assert credentials.verify_password(candidate, stored) is False
        assert is_initial is True


def test_placeholder_is_not_forced_into_a_locale_hash(
    v1_sample: SampleLayout, tmp_path: Path
) -> None:
    """占位串本身也不是合法 pbkdf2 记录，连格式都不像真实哈希。"""
    _, mapped, write = _prepare(v1_sample, tmp_path / "v2")
    asyncio.run(write_v2(tmp_path / "v2", write, mapped.settings))

    rows = _user_rows(tmp_path / "v2")

    assert all("pbkdf2" not in stored for name, (stored, _) in rows.items() if name != "alice")
    assert "pbkdf2" in rows["alice"][0]
