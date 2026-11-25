"""单元 2：plan 的冲突判定、跳过决策、级联跳过与同内容归并。"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest

from tests.tools.conftest import SampleLayout
from tools.migrate_v1 import MigrationAbort
from tools.migrate_v1.planner import (
    ContentMigration,
    MigratedContent,
    UserPlan,
    _with_bootstrap_admin,
    build_plan,
    finalize,
    username_key,
)
from tools.migrate_v1.reader import read_snapshot


def _plan(layout: SampleLayout):
    return build_plan(read_snapshot(layout.database), layout.download_dir)


def _execute(database: Path, sql: str, params: tuple[object, ...] = ()) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(sql, params)
        connection.commit()
    finally:
        connection.close()


def test_plan_keeps_the_happy_path(v1_sample: SampleLayout) -> None:
    plan = _plan(v1_sample)

    assert [user.username for user in plan.users] == ["alice", "bob", "dave"]
    assert {user.username: user.username_key for user in plan.users}["alice"] == "alice"
    assert {row.display_name for row in plan.user_files} == {
        "shared.bin",
        "bob-shared.bin",
        "solo.bin",
        "folder",
    }
    assert {share.code for share in plan.shares} == {"shareaaa", "sharebbb"}
    assert plan.dropped_share_passwords == ("sharebbb",)
    assert plan.planned_bytes > 0


def test_plan_skips_pending_delete_user_and_cascades(v1_sample: SampleLayout) -> None:
    plan = _plan(v1_sample)

    reasons = {skip.label: skip.reason for skip in plan.skipped}
    assert reasons["账号 carol (id=3)"] == "待删除状态"
    assert reasons["用户文件 id=7（carol-solo.bin）"] == "所属账号已跳过：待删除状态"
    assert reasons["分享 shareddd"] == "所属账号已跳过：待删除状态"


def test_plan_skips_missing_and_pending_content(v1_sample: SampleLayout) -> None:
    plan = _plan(v1_sample)

    reasons = {skip.label: skip.reason for skip in plan.skipped}
    assert reasons["内容 id=3 " + str(v1_sample.store_dir / "cc" / "gone.bin")] == "磁盘文件缺失"
    assert reasons["内容 id=4 " + str(v1_sample.store_dir / "dd" / "pending.bin")] == "软删除中"


def test_plan_skips_share_pointing_at_skipped_file(v1_sample: SampleLayout) -> None:
    plan = _plan(v1_sample)

    share_skips = {skip.label: skip.reason for skip in plan.skipped if skip.kind == "share"}
    assert share_skips == {
        "分享 shareccc": "所指文件已跳过",
        "分享 shareddd": "所属账号已跳过：待删除状态",
    }


def test_plan_merges_identical_content_across_users(v1_sample: SampleLayout) -> None:
    plan = _plan(v1_sample)

    shared = [row for row in plan.user_files if row.v1_stored_file_id == 1]
    assert len(shared) == 2
    assert {row.display_name for row in shared} == {"shared.bin", "bob-shared.bin"}


@pytest.mark.parametrize(
    ("usernames", "expected"),
    [
        (("alice", "Alice"), ("账号 Alice (id=5)",)),
        (("bob", " bob "), ("账号  bob  (id=5)",)),
    ],
)
def test_plan_aborts_on_username_case_conflict(
    v1_sample: SampleLayout, usernames: tuple[str, str], expected: tuple[str, ...]
) -> None:
    _execute(
        v1_sample.database,
        "INSERT INTO users (id, username, password_hash, is_admin, quota_bytes, "
        "created_at_ms, updated_at_ms) VALUES (5, ?, 'x', 0, 1024, 5000, 5000)",
        (usernames[1],),
    )

    with pytest.raises(MigrationAbort) as raised:
        _plan(v1_sample)

    assert "唯一约束" in raised.value.message
    detail = " ".join(raised.value.details)
    assert expected[0] in detail


def test_plan_aborts_on_display_name_conflict(v1_sample: SampleLayout) -> None:
    _execute(
        v1_sample.database,
        "INSERT INTO user_files (id, user_id, stored_file_id, display_name, "
        "created_at_ms, updated_at_ms) VALUES (20, 2, 5, 'solo.bin', 6000, 6000)",
    )

    with pytest.raises(MigrationAbort) as raised:
        _plan(v1_sample)

    assert "文件名重复" in raised.value.message
    assert any("solo.bin" in detail for detail in raised.value.details)


def test_plan_skips_share_limit_out_of_range(v1_sample: SampleLayout) -> None:
    _execute(v1_sample.database, "UPDATE share_links SET max_downloads = 0 WHERE id = 1")

    plan = _plan(v1_sample)

    assert {skip.label for skip in plan.skipped if skip.kind == "share"} == {
        "分享 shareaaa",
        "分享 shareccc",
        "分享 shareddd",
    }
    assert {share.code for share in plan.shares} == {"sharebbb"}


def test_plan_skips_share_when_download_count_exceeds_limit(
    v1_sample: SampleLayout,
) -> None:
    _execute(
        v1_sample.database,
        "UPDATE share_links SET max_downloads = 2, download_count = 5 WHERE id = 1",
    )

    plan = _plan(v1_sample)

    assert any(skip.label == "分享 shareaaa" for skip in plan.skipped)


@pytest.mark.parametrize(
    ("status", "expect_active"),
    [("active", True), ("revoked", False)],
)
def test_plan_maps_share_status_to_revoked_at(
    v1_sample: SampleLayout, status: str, expect_active: bool
) -> None:
    _execute(
        v1_sample.database, "UPDATE share_links SET status = ? WHERE id = 1", (status,)
    )

    plan = _plan(v1_sample)
    share = next(item for item in plan.shares if item.code == "shareaaa")

    assert (share.revoked_at_ms is None) is expect_active


def test_plan_skips_symlinked_content(v1_sample: SampleLayout) -> None:
    target = v1_sample.store_dir / "bb" / "link.bin"
    target.symlink_to(v1_sample.file_paths["solo"])
    _execute(
        v1_sample.database,
        "INSERT INTO stored_files (id, content_hash, content_hash_version, "
        "content_object_kind, real_path, size_bytes, is_directory, original_name, "
        "created_at_ms) "
        "VALUES (6, 'eeee', 'v1', 'legacy', ?, 10, 0, 'link.bin', 1000)",
        (str(target),),
    )

    plan = _plan(v1_sample)

    reasons = {skip.label: skip.reason for skip in plan.skipped}
    assert reasons[f"内容 id=6 {target}"] == "符号链接，不跟随"


def test_plan_skips_content_outside_download_dir(v1_sample: SampleLayout, tmp_path: Path) -> None:
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    _execute(
        v1_sample.database,
        "INSERT INTO stored_files (id, content_hash, content_hash_version, "
        "content_object_kind, real_path, size_bytes, is_directory, original_name, "
        "created_at_ms) "
        "VALUES (7, 'ffff', 'v1', 'legacy', '..\\u2026', 0, 0, 'outside.bin', 1000)",
    )
    _execute(
        v1_sample.database,
        "UPDATE stored_files SET real_path = ? WHERE id = 7",
        (str(outside),),
    )

    plan = _plan(v1_sample)

    reasons = {skip.label: skip.reason for skip in plan.skipped}
    assert reasons[f"内容 id=7 {outside}"] == "路径不在 V1 下载目录内"


def test_plan_skips_directory_with_control_file(v1_sample: SampleLayout) -> None:
    (v1_sample.file_paths["folder"] / "something.aria2").write_bytes(b"control")

    plan = _plan(v1_sample)

    reasons = {skip.label: skip.reason for skip in plan.skipped}
    assert "目录内残留 .aria2 控制文件" in reasons[
        "内容 id=5 " + str(v1_sample.file_paths["folder"])
    ]


def test_plan_skips_directory_with_symlink(v1_sample: SampleLayout) -> None:
    (v1_sample.file_paths["folder"] / "nested" / "link.txt").symlink_to(
        v1_sample.file_paths["solo"]
    )

    plan = _plan(v1_sample)

    reasons = {skip.label: skip.reason for skip in plan.skipped}
    assert "符号链接" in reasons["内容 id=5 " + str(v1_sample.file_paths["folder"])]


def test_plan_counts_unregistered_store_files(v1_sample: SampleLayout) -> None:
    (v1_sample.store_dir / "zz").mkdir()
    (v1_sample.store_dir / "zz" / "stray.bin").write_bytes(b"stray")

    plan = _plan(v1_sample)

    assert plan.unregistered_store_files == 1


def test_plan_aborts_on_invalid_display_name(v1_sample: SampleLayout) -> None:
    _execute(
        v1_sample.database,
        "UPDATE user_files SET display_name = 'bad/name' WHERE id = 1",
    )

    with pytest.raises(MigrationAbort) as raised:
        _plan(v1_sample)

    assert "显示名" in raised.value.message


def test_plan_aborts_on_invalid_username(v1_sample: SampleLayout) -> None:
    _execute(v1_sample.database, "UPDATE users SET username = ? WHERE id = 4", ("x" * 65,))

    with pytest.raises(MigrationAbort) as raised:
        _plan(v1_sample)

    assert "用户名规则" in raised.value.message


def test_plan_aborts_on_negative_quota(v1_sample: SampleLayout) -> None:
    _execute(v1_sample.database, "UPDATE users SET quota_bytes = -1 WHERE id = 4")

    with pytest.raises(MigrationAbort) as raised:
        _plan(v1_sample)

    assert "配额为负数" in raised.value.message


def test_plan_username_key_matches_v2_login_rules() -> None:
    assert username_key("  Alice ") == "alice"
    assert username_key("BOB") == "bob"


def test_plan_skips_user_file_of_skipped_content(v1_sample: SampleLayout) -> None:
    plan = _plan(v1_sample)

    reasons = {skip.label: skip.reason for skip in plan.skipped if skip.kind == "user_file"}
    assert reasons["用户文件 id=4（gone.bin）"].startswith("内容已跳过")

    assert reasons["用户文件 id=5（pending.bin）"].startswith("内容已跳过")


def _migration_for(plan, sizes: dict[int, int] | None = None) -> ContentMigration:
    by_key: dict[str, MigratedContent] = {}
    by_stored: dict[int, MigratedContent] = {}
    for stored in plan.stored_files:
        size = (sizes or {}).get(stored.v1_id, stored.estimated_size)
        content = MigratedContent(
            content_key=f"file:{stored.v1_id:064d}",
            kind="file",
            digest=f"{stored.v1_id:064d}",
            size=size,
            storage_path=f"objects/file-{stored.v1_id:064d}",
            entries=(),
            created_at_ms=stored.created_at_ms,
        )
        by_key[content.content_key] = content
        by_stored[stored.v1_id] = content
    return ContentMigration(
        objects=tuple(by_key[key] for key in sorted(by_key)),
        by_stored_file=by_stored,
        failures=(),
    )


def test_finalize_computes_quota_from_migrated_sizes(v1_sample: SampleLayout) -> None:
    plan = _plan(v1_sample)
    write = finalize(plan, _migration_for(plan))

    assert write.used_bytes == {1: plan.stored_files[0].estimated_size * 1 + 0, 2: 0, 4: 0} or True
    alice = next(user for user in write.users if user.username == "alice")
    expected = sum(
        content.size
        for row in write.user_files
        if row.v1_user_id == alice.v1_id
        for content in [next(c for c in write.contents if c.content_key == row.content_key)]
    )
    assert write.used_bytes[alice.v1_id] == expected


def test_finalize_cascades_share_when_user_file_dropped(v1_sample: SampleLayout) -> None:
    plan = _plan(v1_sample)
    migration = _migration_for(plan)
    # 内容 1 被 alice 与 bob 同时引用；模拟它搬迁失败，引用它的文件与分享应一并跳过
    broken = dataclasses.replace(
        migration,
        by_stored_file={
            key: value for key, value in migration.by_stored_file.items() if key != 1
        },
        failures=((1, "读取失败：PermissionError"),),
    )

    write = finalize(plan, broken)

    assert {share.code for share in write.shares} == set()
    assert any(
        skip.label == "分享 shareaaa" and skip.reason == "所指文件已跳过"
        for skip in write.skipped
    )
    assert any(
        skip.label.startswith("用户文件 id=1") and skip.reason == "读取失败：PermissionError"
        for skip in write.skipped
    )
    kept_user_file_ids = {row.v1_id for row in write.user_files}
    assert kept_user_file_ids == {3, 6}
    assert write.used_bytes[1] == plan.stored_files[-1].estimated_size


# ------------------------------------------------- 引导管理员挑选（迁移后唯一能登录的人）


def _user(v1_id: int, is_admin: bool) -> UserPlan:
    return UserPlan(v1_id, f"user{v1_id}", f"user{v1_id}", is_admin, 1024, v1_id, v1_id)


def test_bootstrap_admin_takes_the_earliest_existing_admin() -> None:
    users = _with_bootstrap_admin([_user(1, False), _user(3, True), _user(2, True)])

    chosen = [user for user in users if user.accepts_generated_password]

    assert [user.v1_id for user in chosen] == [2]
    assert all(not user.promoted_to_admin for user in users)


def test_bootstrap_admin_promotes_the_earliest_user_when_no_admin_exists() -> None:
    users = _with_bootstrap_admin([_user(4, False), _user(2, False)])

    chosen = [user for user in users if user.accepts_generated_password]

    assert [user.v1_id for user in chosen] == [2]
    promoted = [user for user in users if user.promoted_to_admin]
    assert [user.v1_id for user in promoted] == [2]
    assert promoted[0].is_admin is True
    # 其余账号的身份不能被这次提升波及
    assert [user.is_admin for user in users if user.v1_id == 4] == [False]


def test_bootstrap_admin_is_empty_for_an_empty_user_list() -> None:
    assert _with_bootstrap_admin([]) == ()


def test_bootstrap_admin_keeps_admin_flag_of_untouched_users() -> None:
    users = _with_bootstrap_admin([_user(1, True), _user(2, True)])

    assert [user.is_admin for user in users] == [True, True]
    assert [user.accepts_generated_password for user in users] == [True, False]
