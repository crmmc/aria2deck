"""单元 5：七项校验各一条 PASS / FAIL，报告渲染与敏感信息不泄漏。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from tests.tools.conftest import (
    V1_RPC_SECRET,
    V1_RPC_SECRET_ENCRYPTED,
    V1_SECOND_PASSWORD_HASH,
    V1_SHARE_PASSWORD_HASH,
    V1_USER_PASSWORD_HASH,
    SampleLayout,
)
from tools.migrate_v1.contents import migrate_contents
from tools.migrate_v1.planner import Skip, build_plan, finalize
from tools.migrate_v1.reader import read_snapshot
from tools.migrate_v1.report import (
    BootstrapAdmin,
    Check,
    Observed,
    Report,
    Summary,
    build_checks,
    estimate_seconds,
    quota_overruns,
    render,
    summarize,
    write_report,
)
from tools.migrate_v1.settings_map import map_settings
from tools.migrate_v1.writer import write_v2


def _parts(layout: SampleLayout, v2_root: Path):
    snapshot = read_snapshot(layout.database)
    plan = build_plan(snapshot, layout.download_dir)
    migration = migrate_contents(plan, v2_root, dry_run=False)
    write = finalize(plan, migration)
    return snapshot, plan, write


def _observed(
    plan, write, *, bump: str | None = None, delta: int = 0, full: bool = True
) -> Observed:
    """构造观测值；只有 `full=True` 时才带配额与设置，便于单点证伪。"""
    counts = {
        "users": len(write.users),
        "user_files": len(write.user_files),
        "share_links": len(write.shares),
        "content_objects": len(write.contents),
        "content_entries": 0,
    }
    if bump is not None:
        counts[bump] += delta
    return Observed(
        counts=counts,
        content_bytes=sum(c.size for c in write.contents) + (delta if bump is None else 0),
        quota_used=None if not full else {},
        settings=None,
    )


def _checks(plan, write, observed: Observed, user_ids: dict[int, int]):
    return build_checks(
        plan=plan,
        write=write,
        observed=observed,
        user_ids=user_ids,
        expected_settings=None,
    )


@pytest.fixture
def parts(v1_sample: SampleLayout, tmp_path: Path):
    return _parts(v1_sample, tmp_path / "v2")


def _user_ids(plan) -> dict[int, int]:
    return {user.v1_id: index + 1 for index, user in enumerate(plan.users)}


def test_dry_run_checks_pass_without_observations(parts, tmp_path: Path) -> None:
    _, plan, write = parts
    checks = _checks(plan, write, Observed(), {})

    assert len(checks) == 7
    assert all(check.passed for check in checks)
    assert all("预演未写入" in check.actual for check in checks[:4])


@pytest.mark.parametrize(
    ("label", "bump"),
    [
        ("用户数", "users"),
        ("用户文件数", "user_files"),
        ("分享数", "share_links"),
        ("内容对象数", "content_objects"),
    ],
)
def test_count_checks_detect_mismatch(parts, label: str, bump: str) -> None:
    _, plan, write = parts
    observed = _observed(plan, write, bump=bump, delta=1, full=False)

    checks = _checks(plan, write, observed, _user_ids(plan))

    failed = [check.label for check in checks if not check.passed]
    assert failed == [label]


def test_byte_check_detects_mismatch(parts) -> None:
    _, plan, write = parts
    observed = _observed(plan, write, delta=1, full=False)

    checks = _checks(plan, write, observed, _user_ids(plan))

    failed = [check.label for check in checks if not check.passed]
    assert failed == ["磁盘字节数"]


def test_quota_check_detects_mismatch(parts) -> None:
    _, plan, write = parts
    user_ids = _user_ids(plan)
    wrong = {user_id: 0 for user_id in user_ids.values()}
    observed = replace(_observed(plan, write), quota_used=wrong)

    checks = _checks(plan, write, observed, user_ids)

    quota = next(check for check in checks if check.label == "配额用量")
    assert quota.passed is False
    assert "不一致" in quota.actual


def test_quota_check_passes_with_correct_values(parts) -> None:
    _, plan, write = parts
    user_ids = _user_ids(plan)
    observed = replace(
        _observed(plan, write),
        quota_used={user_ids[v1_id]: used for v1_id, used in write.used_bytes.items()},
    )

    checks = _checks(plan, write, observed, user_ids)

    assert next(check for check in checks if check.label == "配额用量").passed is True


def test_settings_check_detects_mismatch(parts) -> None:
    _, plan, write = parts
    observed = replace(
        _observed(plan, write),
        settings={"site_title": "别的标题", "aria2_rpc_url": "http://x/y"},
    )
    checks = build_checks(
        plan=plan,
        write=write,
        observed=observed,
        user_ids=_user_ids(plan),
        expected_settings={"site_title": "我的站点", "aria2_rpc_url": "http://x/y"},
    )

    settings_check = next(check for check in checks if check.label == "系统设置")
    assert settings_check.passed is False
    assert "site_title" in settings_check.actual


def test_settings_check_passes_when_values_match(parts) -> None:
    _, plan, write = parts
    expected = {"site_title": "我的站点"}
    observed = replace(_observed(plan, write), settings={"site_title": "我的站点"})

    checks = build_checks(
        plan=plan,
        write=write,
        observed=observed,
        user_ids=_user_ids(plan),
        expected_settings=expected,
    )

    assert next(check for check in checks if check.label == "系统设置").passed is True


@pytest.mark.parametrize("label", ["用户数", "用户文件数", "分享数", "内容对象数", "磁盘字节数"])
def test_each_check_can_fail(parts, label: str) -> None:
    """逐项证明断言有效：把对应观测值改错，必须只让该项变红。"""
    _, plan, write = parts
    counts = {
        "用户数": "users",
        "用户文件数": "user_files",
        "分享数": "share_links",
        "内容对象数": "content_objects",
    }
    if label == "磁盘字节数":
        observed = _observed(plan, write, delta=1, full=False)
    else:
        observed = _observed(plan, write, bump=counts[label], delta=1, full=False)

    checks = _checks(plan, write, observed, _user_ids(plan))

    assert next(check for check in checks if check.label == label).passed is False


def test_report_renders_summary_and_checks(parts) -> None:
    snapshot, plan, write = parts
    report = Report(
        mode="正式执行",
        v1_database=str(snapshot.db_path if hasattr(snapshot, "db_path") else "/v1.db"),
        v1_version=18,
        integrity_check="ok",
        v1_download_dir="/v1/downloads",
        v2_root="/v2",
        summary=summarize(plan, write, 2048),
        skipped=write.skipped,
        dropped_share_passwords=write.dropped_share_passwords,
        quota_overruns=quota_overruns(plan, write),
        settings_fallbacks=("pack_format（打包格式无效），已回退为 V2 默认值",),
        dropped_settings=("aria2_rpc_secret",),
        checks=(Check("用户数", "3", "3", True),),
        elapsed_seconds=1.5,
    )

    text = render(report)

    assert "V1 → V2 迁移报告" in text
    assert "模式: 正式执行" in text
    assert "用户:      创建 3，跳过 1" in text
    assert "内容对象:  创建" in text
    assert "跳过的账号" in text
    assert "失效密码的分享" in text
    assert "code=sharebbb" in text
    assert "回退为 V2 默认值的设置项" in text
    assert "未迁移的 V1 设置项" in text
    assert "用户数 3 == 3   PASS" in text
    assert "结论: 迁移成功" in text
    assert "本次迁移没有留下任何账号" in text


def test_report_renders_abort(parts) -> None:
    _, plan, write = parts
    report = Report(
        mode="预演（--dry-run）",
        v1_database="/v1.db",
        v1_version=0,
        integrity_check="未校验",
        v1_download_dir="/v1/downloads",
        v2_root="/v2",
        summary=Summary(0, 0, 0, {}, 0, 0, 0, 0, 0, 0),
        skipped=(),
        dropped_share_passwords=(),
        quota_overruns=(),
        settings_fallbacks=(),
        dropped_settings=(),
        checks=(),
        elapsed_seconds=0.0,
        aborted_reason="V2 目标目录非空，拒绝覆盖已有部署",
        aborted_details=("/v2",),
    )

    text = render(report)

    assert "结论: 迁移中止" in text
    assert "/v2" in text
    assert report.succeeded is False


def test_report_lists_only_fifty_items_and_counts_the_rest(parts) -> None:
    _, plan, write = parts
    many = tuple(
        Skip("user_file", f"用户文件 id={index}", "磁盘文件缺失") for index in range(60)
    )
    report = Report(
        mode="正式执行",
        v1_database="/v1.db",
        v1_version=18,
        integrity_check="ok",
        v1_download_dir="/v1/downloads",
        v2_root="/v2",
        summary=summarize(plan, write, 0),
        skipped=many,
        dropped_share_passwords=(),
        quota_overruns=(),
        settings_fallbacks=(),
        dropped_settings=(),
        checks=(),
        elapsed_seconds=0.0,
    )

    text = render(report)

    assert "共 60 条" in text
    assert "…另有 10 条未列出" in text


def test_report_never_contains_secrets(v1_sample: SampleLayout, tmp_path: Path) -> None:
    snapshot, plan, write = _parts(v1_sample, tmp_path / "v2")
    mapped = map_settings(snapshot.settings)
    report = Report(
        mode="正式执行",
        v1_database=str(v1_sample.database),
        v1_version=18,
        integrity_check="ok",
        v1_download_dir=str(v1_sample.download_dir),
        v2_root=str(tmp_path / "v2"),
        summary=summarize(plan, write, 0),
        skipped=write.skipped,
        dropped_share_passwords=write.dropped_share_passwords,
        quota_overruns=quota_overruns(plan, write),
        settings_fallbacks=mapped.fallbacks,
        dropped_settings=mapped.dropped,
        checks=(),
        elapsed_seconds=0.0,
    )

    text = render(report)

    for secret in (
        V1_USER_PASSWORD_HASH,
        V1_SECOND_PASSWORD_HASH,
        V1_SHARE_PASSWORD_HASH,
        V1_RPC_SECRET,
        V1_RPC_SECRET_ENCRYPTED,
    ):
        assert secret not in text
    assert "pbkdf2" not in text.lower()


def test_quota_overruns_lists_users_above_quota(parts) -> None:
    _, plan, write = parts
    squeezed = replace(
        write,
        used_bytes={v1_id: 2**40 for v1_id in write.used_bytes},
    )

    overruns = quota_overruns(plan, squeezed)

    assert overruns
    assert all("配额" in item and "实际" in item for item in overruns)


def test_report_file_is_written(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "report.txt"

    write_report(target, "报告正文")

    assert target.read_text(encoding="utf-8") == "报告正文"


def test_estimate_seconds_uses_throughput() -> None:
    assert estimate_seconds(0) == 0
    assert estimate_seconds(300 * 1024 * 1024) == pytest.approx(1.0)


# ------------------------------------------------ 管理员引导：只写用户名，绝不写密码


def _report_with(admin: BootstrapAdmin | None) -> Report:
    return Report(
        mode="正式执行",
        v1_database="/v1.db",
        v1_version=18,
        integrity_check="ok",
        v1_download_dir="/v1/downloads",
        v2_root="/v2",
        summary=Summary(0, 0, 0, {}, 0, 0, 0, 0, 0, 0),
        skipped=(),
        dropped_share_passwords=(),
        quota_overruns=(),
        settings_fallbacks=(),
        dropped_settings=(),
        checks=(),
        elapsed_seconds=0.0,
        bootstrap_admin=admin,
    )


def test_report_names_the_bootstrap_admin_without_its_password(v1_sample: SampleLayout, tmp_path: Path) -> None:
    """从真实迁移里取出密码，再断言渲染出来的报告文本一个字符都不含它。"""
    snapshot, plan, write = _parts(v1_sample, tmp_path / "v2")
    mapped = map_settings(snapshot.settings)
    result = asyncio.run(write_v2(tmp_path / "v2", write, mapped.settings))
    assert result.bootstrap_admin is not None
    username, password = result.bootstrap_admin
    report = replace(
        _report_with(
            BootstrapAdmin(
                username=username,
                v2_id=result.user_ids[1],
                promoted=False,
                placeholder_accounts=2,
                dry_run=False,
            )
        ),
        summary=summarize(plan, write, 0),
    )

    text = render(report)

    assert "管理员引导" in text
    assert f"已将账号 {username} (id={result.user_ids[1]}) 设为可登录" in text
    assert "密码只在终端显示，不写入本报告" in text
    assert "其余 2 个账号为占位密码" in text
    # 关键断言：密码明文（整体与任意 8 字符片段）都不在报告里
    assert password not in text
    for start in range(0, len(password) - 8, 8):
        assert password[start : start + 8] not in text


def test_report_reports_a_promoted_admin_explicitly() -> None:
    text = render(
        _report_with(
            BootstrapAdmin(
                username="bob",
                v2_id=7,
                promoted=True,
                placeholder_accounts=1,
                dry_run=False,
            )
        )
    )

    assert "V1 里没有任何管理员" in text
    assert "提升为管理员" in text
    assert "bob (id=7)" in text


def test_report_says_nothing_to_reset_when_no_other_accounts() -> None:
    text = render(
        _report_with(
            BootstrapAdmin("alice", 1, promoted=False, placeholder_accounts=0, dry_run=False)
        )
    )

    assert "没有其他账号需要设置临时密码" in text


def test_dry_run_report_promises_the_password_without_claiming_it_was_shown() -> None:
    text = render(
        _report_with(
            BootstrapAdmin(
                username="alice", v2_id=None, promoted=False, placeholder_accounts=2, dry_run=True
            )
        )
    )

    assert "预演不生成密码" in text
    assert "只在终端显示" not in text


def test_dry_run_report_marks_the_promotion_decision() -> None:
    text = render(
        _report_with(
            BootstrapAdmin(
                username="bob", v2_id=None, promoted=True, placeholder_accounts=1, dry_run=True
            )
        )
    )

    assert "提升为管理员" in text
    assert "V1 里没有任何管理员" in text


def test_report_without_any_account_says_nobody_can_log_in() -> None:
    text = render(_report_with(None))

    assert "本次迁移没有留下任何账号" in text
    assert "管理员引导" not in text
