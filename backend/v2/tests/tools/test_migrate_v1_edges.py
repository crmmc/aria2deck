"""边界与防御路径：竞态、越界数据、以及只有纯函数才能构造出来的畸形快照。"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from aria2deck.domain.content import ContentEntryIdentity, ContentKind
from tests.tools.conftest import SampleLayout
from tools.migrate_v1 import MigrationAbort
from tools.migrate_v1.contents import _ObjectBuilder, migrate_contents, objects_root
from tools.migrate_v1.planner import (
    ContentMigration,
    DirectoryEntry,
    Plan,
    Skip,
    StoredFilePlan,
    UserPlan,
    _resolve_source,
    build_plan,
    count_unregistered_store_files,
    finalize,
)
from tools.migrate_v1.reader import V1UserFile, read_snapshot
from tools.migrate_v1.report import Check, Observed, Report, Summary, build_checks, render
from tools.migrate_v1.settings_map import map_settings


def _user(v1_id: int = 1) -> UserPlan:
    return UserPlan(v1_id, "alice", "alice", False, 1024, 1000, 1000)


def _stored(v1_id: int, source: Path, *, is_directory: bool = False) -> StoredFilePlan:
    return StoredFilePlan(v1_id, source, is_directory, (), 0, 1000)


def _empty_plan(**overrides: object) -> Plan:
    base = Plan((_user(),), (), (), (), (), (), 0, 0)
    return dataclasses.replace(base, **overrides)


# ---------------------------------------------------------------- planner 纯函数


def test_resolve_source_rejects_empty_path(tmp_path: Path) -> None:
    assert _resolve_source("   ", tmp_path) is None
    assert _resolve_source("", tmp_path) is None


def test_resolve_source_rejects_download_dir_itself(tmp_path: Path) -> None:
    assert _resolve_source(".", tmp_path) is None


def test_resolve_source_accepts_relative_path_inside(tmp_path: Path) -> None:
    resolved = _resolve_source("store/aa/x.bin", tmp_path)

    assert resolved == tmp_path / "store" / "aa" / "x.bin"


def test_scan_directory_rejects_symlinked_subdirectory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    (root / "link").symlink_to(root / "real", target_is_directory=True)
    plan = _empty_plan(
        stored_files=(
            StoredFilePlan(
                1,
                root,
                True,
                (DirectoryEntry("real", True, 0), DirectoryEntry("link", True, 0)),
                0,
                1000,
            ),
        )
    )

    migration = migrate_contents(plan, tmp_path / "v2", dry_run=False)

    assert migration.failures == ((1, "内容布局不符合 V2 规范：目录内容含符号链接"),)


def test_scan_directory_rejects_non_regular_file(tmp_path: Path) -> None:
    import os

    root = tmp_path / "root"
    root.mkdir()
    fifo = root / "pipe"
    os.mkfifo(fifo)

    migration = migrate_contents(
        _empty_plan(stored_files=(_stored(1, root, is_directory=True),)),
        tmp_path / "v2",
        dry_run=False,
    )

    assert migration.failures == ((1, "内容布局不符合 V2 规范：目录内有非普通文件"),)
    assert not (objects_root(tmp_path / "v2") / ".tmp-directory-1").exists()


def test_scan_directory_rejects_leftover_control_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "x.aria2").write_bytes(b"control")

    migration = migrate_contents(
        _empty_plan(stored_files=(_stored(1, root, is_directory=True),)),
        tmp_path / "v2",
        dry_run=False,
    )

    assert migration.failures == ((1, "内容布局不符合 V2 规范：目录内残留 .aria2 控制文件"),)


def test_builder_treats_missing_source_as_failure(tmp_path: Path) -> None:
    gone = tmp_path / "gone.bin"

    migration = migrate_contents(
        _empty_plan(stored_files=(_stored(1, gone),)), tmp_path / "v2", dry_run=False
    )

    assert migration.failures == ((1, "磁盘文件缺失"),)


def test_builder_cleans_temporary_file_on_read_error(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    builder = _ObjectBuilder(objects_root(tmp_path / "v2"), dry_run=False)
    builder.objects.mkdir(parents=True)
    stale = builder.objects / ".tmp-file-1"
    stale.write_bytes(b"leftover")

    def explode(*_: object) -> tuple[str, int]:
        raise OSError("boom")

    import tools.migrate_v1.contents as module

    original = module._stream
    module._stream = explode  # type: ignore[assignment]
    try:
        with pytest.raises(OSError):
            builder.file(_stored(1, source))
    finally:
        module._stream = original  # type: ignore[assignment]

    assert not stale.exists()


def test_builder_cleans_temporary_directory_on_walk_error(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "x.aria2").write_bytes(b"control")
    builder = _ObjectBuilder(objects_root(tmp_path / "v2"), dry_run=False)

    with pytest.raises(Exception):  # noqa: B017 - 具体类型由 V2 domain 决定
        builder.directory(_stored(1, root, is_directory=True))

    assert not (builder.objects / ".tmp-directory-1").exists()


def test_migration_reraises_migration_abort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tools.migrate_v1.contents as module

    def explode(self: object, *_: object) -> object:
        raise MigrationAbort("停止")

    monkeypatch.setattr(module._ObjectBuilder, "file", explode)

    with pytest.raises(MigrationAbort):
        migrate_contents(
            _empty_plan(stored_files=(_stored(1, tmp_path / "x"),)),
            tmp_path / "v2",
            dry_run=False,
        )


def test_same_digest_with_different_size_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两个不同文件被算成同一摘要（模拟哈希碰撞）时不能静默合并。"""
    import tools.migrate_v1.contents as module

    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"a")
    second.write_bytes(b"bb")

    original = module._stream

    def fake(source: Path, sink) -> tuple[str, int]:
        digest, size = original(source, sink)
        return "0" * 64, size

    monkeypatch.setattr(module, "_stream", fake)

    migration = migrate_contents(
        _empty_plan(stored_files=(_stored(1, first), _stored(2, second))),
        tmp_path / "v2",
        dry_run=False,
    )

    assert migration.failures == ((2, "同摘要内容大小不一致"),)
    assert 1 in migration.by_stored_file
    assert 2 not in migration.by_stored_file


def test_count_unregistered_returns_zero_without_store(tmp_path: Path) -> None:
    assert count_unregistered_store_files(set(), (), tmp_path / "missing") == 0


def test_count_unregistered_skips_files_inside_registered_directory(tmp_path: Path) -> None:
    store = tmp_path / "store"
    (store / "folder").mkdir(parents=True)
    (store / "folder" / "inside.txt").write_bytes(b"x")
    (store / "loose.txt").write_bytes(b"y")

    assert (
        count_unregistered_store_files(set(), (str(store / "folder"),), store) == 1
    )


def test_user_file_with_dangling_reference_is_skipped(tmp_path: Path) -> None:
    """V1 的 SQLite 连接不强制外键，历史数据里可能出现悬空引用。"""
    layout = _sample(tmp_path)
    snapshot = dataclasses.replace(
        read_snapshot(layout.database),
        user_files=(V1UserFile(99, 4242, 1, "orphan.bin", 1000),),
    )

    plan = build_plan(snapshot, layout.download_dir)

    assert any(
        skip.kind == "user_file" and skip.reason == "引用了不存在的账号或内容"
        for skip in plan.skipped
    )


def _sample(tmp_path: Path):
    from tests.tools.conftest import create_v1_sample

    return create_v1_sample(tmp_path / "v1")


def test_share_with_dangling_owner_is_skipped(tmp_path: Path) -> None:
    layout = _sample(tmp_path)
    snapshot = read_snapshot(layout.database)
    broken = dataclasses.replace(
        snapshot,
        share_links=(
            dataclasses.replace(snapshot.share_links[0], owner_id=4242),
        ),
    )

    plan = build_plan(broken, layout.download_dir)

    assert any(
        skip.kind == "share" and skip.reason == "引用了不存在的账号或文件"
        for skip in plan.skipped
    )


def test_share_owner_mismatch_is_skipped(tmp_path: Path) -> None:
    layout = _sample(tmp_path)
    snapshot = read_snapshot(layout.database)
    row = next(item for item in snapshot.user_files if item.user_id == 2)
    broken = dataclasses.replace(
        snapshot,
        share_links=(
            dataclasses.replace(
                snapshot.share_links[0],
                owner_id=1,
                user_file_id=row.id,
            ),
        ),
    )

    plan = build_plan(broken, layout.download_dir)

    assert any(
        skip.reason == "归属账号与文件归属不一致" for skip in plan.skipped
    )


def test_duplicate_share_code_aborts(tmp_path: Path) -> None:
    layout = _sample(tmp_path)
    snapshot = read_snapshot(layout.database)
    duplicate = dataclasses.replace(snapshot.share_links[0])
    broken = dataclasses.replace(
        snapshot, share_links=(snapshot.share_links[0], duplicate)
    )

    with pytest.raises(MigrationAbort) as raised:
        build_plan(broken, layout.download_dir)

    assert "分享码重复" in raised.value.message


def test_negative_download_count_is_skipped(tmp_path: Path) -> None:
    layout = _sample(tmp_path)
    snapshot = read_snapshot(layout.database)
    broken = dataclasses.replace(
        snapshot,
        share_links=(
            dataclasses.replace(snapshot.share_links[0], download_count=-1),
        ),
    )

    plan = build_plan(broken, layout.download_dir)

    assert plan.shares == ()


# ---------------------------------------------------------------- 设置映射边界


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("not json", ()),
        ("[]", ()),
        ('[".a", 5]', ()),
    ],
)
def test_hidden_extensions_with_bad_payload_falls_back_to_empty(
    tmp_path: Path, raw: str, expected: tuple[str, ...]
) -> None:
    layout = _sample(tmp_path)
    snapshot = read_snapshot(layout.database)
    patched = dataclasses.replace(snapshot.settings, hidden_file_extensions_json=raw)

    assert map_settings(patched).settings.hidden_file_extensions == expected


@pytest.mark.parametrize("jitter", ["1.5", "-0.2"])
def test_jitter_outside_ratio_range_becomes_one_second(
    tmp_path: Path, jitter: str
) -> None:
    layout = _sample(tmp_path)
    snapshot = read_snapshot(layout.database)
    patched = dataclasses.replace(snapshot.settings, ws_reconnect_jitter=jitter)

    assert map_settings(patched).settings.ws_reconnect_jitter_seconds == 1


def test_max_delay_out_of_range_falls_back_and_reports(tmp_path: Path) -> None:
    layout = _sample(tmp_path)
    snapshot = read_snapshot(layout.database)
    patched = dataclasses.replace(snapshot.settings, ws_reconnect_max_delay=9000)

    outcome = map_settings(patched)

    assert outcome.settings.ws_reconnect_max_delay_seconds == 30
    assert any("ws_reconnect_max_delay_seconds" in item for item in outcome.fallbacks)


# ---------------------------------------------------------------- 报告渲染边界


def test_quota_check_is_neutral_without_accounts(tmp_path: Path) -> None:
    plan = _empty_plan()
    migration = ContentMigration((), {}, ())
    write = finalize(plan, migration)

    checks = build_checks(
        plan=plan,
        write=write,
        observed=Observed(counts=None, content_bytes=None, quota_used={}, settings=None),
        user_ids={},
        expected_settings=None,
    )

    quota = next(check for check in checks if check.label == "配额用量")
    assert quota.actual == "（无账号）"


def test_report_with_failed_checks_renders_mismatch_conclusion() -> None:
    report = Report(
        mode="正式执行",
        v1_database="/v1.db",
        v1_version=18,
        integrity_check="ok",
        v1_download_dir="/v1",
        v2_root="/v2",
        summary=Summary(0, 0, 0, {}, 0, 0, 0, 0, 0, 0),
        skipped=(),
        dropped_share_passwords=(),
        quota_overruns=(),
        settings_fallbacks=(),
        dropped_settings=(),
        checks=(Check("用户数", "3", "2", False),),
        elapsed_seconds=0.0,
    )

    text = render(report)

    assert "结论: 迁移数据与 V1 侧统计不一致" in text
    assert report.succeeded is False


def test_report_renders_quota_overruns() -> None:
    report = Report(
        mode="正式执行",
        v1_database="/v1.db",
        v1_version=18,
        integrity_check="ok",
        v1_download_dir="/v1",
        v2_root="/v2",
        summary=Summary(0, 0, 0, {}, 0, 0, 0, 0, 0, 0),
        skipped=(),
        dropped_share_passwords=(),
        quota_overruns=("alice：配额 1.0 GB，实际 2.0 GB",),
        settings_fallbacks=(),
        dropped_settings=(),
        checks=(),
        elapsed_seconds=0.0,
    )

    text = render(report)

    assert "配额超出上限的用户" in text
    assert "alice" in text


def test_unlink_removes_symlinked_temporary(tmp_path: Path) -> None:
    """临时名若被换成符号链接，清理只删链接本身，不跟随。"""
    from tools.migrate_v1.contents import _discard

    target = tmp_path / "target"
    target.write_bytes(b"keep")
    link = tmp_path / ".tmp-file-1"
    link.symlink_to(target)

    _discard(link)

    assert not link.exists()
    assert target.read_bytes() == b"keep"


def test_relative_path_normalization_rejects_traversal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    entry = ContentEntryIdentity("../escape", ContentKind.FILE, 0, "0" * 64)

    plan = _empty_plan(
        stored_files=(
            dataclasses.replace(
                _stored(1, root, is_directory=True),
            ),
        )
    )
    import tools.migrate_v1.contents as module

    original = module._relative

    def hostile(item: Path, base: Path) -> str:
        return entry.path

    module._relative = hostile  # type: ignore[assignment]
    (root / "x.bin").write_bytes(b"x")
    try:
        migration = migrate_contents(plan, tmp_path / "v2", dry_run=False)
    finally:
        module._relative = original  # type: ignore[assignment]

    assert migration.failures
    assert migration.failures[0][0] == 1


def test_dry_run_records_failure_without_touching_disk(tmp_path: Path) -> None:
    """预演里读失败只记失败，不创建任何临时文件。"""
    migration = migrate_contents(
        _empty_plan(stored_files=(_stored(1, tmp_path / "gone.bin"),)),
        tmp_path / "v2",
        dry_run=True,
    )

    assert migration.failures == ((1, "磁盘文件缺失"),)
    assert not (tmp_path / "v2").exists()


def test_dry_run_records_layout_violation_without_touching_disk(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "x.aria2").write_bytes(b"control")

    migration = migrate_contents(
        _empty_plan(stored_files=(_stored(1, root, is_directory=True),)),
        tmp_path / "v2",
        dry_run=True,
    )

    assert migration.failures == ((1, "内容布局不符合 V2 规范：目录内残留 .aria2 控制文件"),)
    assert not (tmp_path / "v2").exists()


def test_planner_rejects_symlinked_subdirectory(tmp_path: Path) -> None:
    layout = _sample_for(tmp_path)
    root = layout.download_dir / "store" / "content"
    (root / "real").mkdir(parents=True)
    (root / "link").symlink_to(root / "real", target_is_directory=True)
    snapshot = dataclasses.replace(
        layout.snapshot,
        stored_files=(
            dataclasses.replace(
                layout.snapshot.stored_files[0],
                id=1,
                real_path=str(root),
                is_directory=True,
            ),
        ),
        user_files=(),
        share_links=(),
    )

    plan = build_plan(snapshot, layout.download_dir)

    assert any(skip.reason == "目录内容含符号链接" for skip in plan.skipped)


def test_planner_rejects_non_regular_file_in_directory(tmp_path: Path) -> None:
    import os

    layout = _sample_for(tmp_path)
    root = layout.download_dir / "store" / "content"
    root.mkdir()
    os.mkfifo(root / "pipe")

    snapshot = dataclasses.replace(
        layout.snapshot,
        stored_files=(
            dataclasses.replace(
                layout.snapshot.stored_files[0],
                real_path=str(root),
                is_directory=True,
            ),
        ),
        user_files=(),
        share_links=(),
    )

    plan = build_plan(snapshot, layout.download_dir)

    assert any(skip.reason == "目录内有非普通文件" for skip in plan.skipped)


def test_planner_rejects_directory_record_pointing_at_a_file(tmp_path: Path) -> None:
    layout = _sample_for(tmp_path)
    plain = layout.download_dir / "plain.txt"
    plain.write_bytes(b"plain")

    snapshot = dataclasses.replace(
        layout.snapshot,
        stored_files=(
            dataclasses.replace(
                layout.snapshot.stored_files[0],
                real_path=str(plain),
                is_directory=True,
            ),
        ),
        user_files=(),
        share_links=(),
    )

    plan = build_plan(snapshot, layout.download_dir)

    assert any(skip.reason == "记录为目录但磁盘上不是目录" for skip in plan.skipped)


def test_planner_rejects_file_record_pointing_at_a_directory(tmp_path: Path) -> None:
    layout = _sample_for(tmp_path)
    snapshot = dataclasses.replace(
        layout.snapshot,
        stored_files=(
            dataclasses.replace(
                layout.snapshot.stored_files[0],
                real_path=str(layout.download_dir / "store"),
                is_directory=False,
            ),
        ),
        user_files=(),
        share_links=(),
    )

    plan = build_plan(snapshot, layout.download_dir)

    assert any(skip.reason == "记录为文件但磁盘上不是普通文件" for skip in plan.skipped)


@dataclasses.dataclass(frozen=True)
class _Sample:
    snapshot: object
    download_dir: Path


def _sample_for(tmp_path: Path) -> _Sample:
    """每个测试只造一次样本：store/ 已存在时重复创建会抛错。"""
    from tests.tools.conftest import create_v1_sample

    layout = create_v1_sample(tmp_path / "sample")
    return _Sample(read_snapshot(layout.database), layout.download_dir)


def test_sample_plan_still_dry_runs_after_edge_cases(v1_sample: SampleLayout, tmp_path: Path) -> None:
    """所有边界测试之后，正常样本仍能预演成功（无全局状态污染）。"""
    plan = build_plan(read_snapshot(v1_sample.database), v1_sample.download_dir)
    migration = migrate_contents(plan, tmp_path / "v2", dry_run=True)

    assert migration.failures == ()
    assert not (tmp_path / "v2").exists()
    assert all(isinstance(skip, Skip) for skip in plan.skipped)
