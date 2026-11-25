"""迁移报告：数据结构、七项校验与文本渲染。

报告只含 id、用户名、文件名、分享码；不含密码哈希、分享密码、RPC 密钥，
也不含引导管理员那次生成的明文密码——明文只经 `__main__.py` 打到终端。
校验结论是结构化数据，渲染只是把它写成人能读的文本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .planner import Plan, Skip, WritePlan

MAX_LISTED_ITEMS = 50


@dataclass(frozen=True, slots=True)
class Check:
    label: str
    expected: str
    actual: str
    passed: bool


@dataclass(frozen=True, slots=True)
class Summary:
    users_created: int
    users_skipped: int
    contents_created: int
    contents_by_kind: dict[str, int]
    user_files_created: int
    user_files_skipped: int
    shares_created: int
    shares_skipped: int
    migrated_bytes: int
    unregistered_store_files: int


@dataclass(frozen=True, slots=True)
class BootstrapAdmin:
    """引导管理员：只有用户名与 id 能进报告，密码永远不进。

    `dry_run=True` 表示预演——不写库、不生成密码，报告里也不能说「已在终端显示」。
    """

    username: str
    v2_id: int | None
    promoted: bool
    placeholder_accounts: int
    dry_run: bool


@dataclass(frozen=True, slots=True)
class Report:
    mode: str
    v1_database: str
    v1_version: int
    integrity_check: str
    v1_download_dir: str
    v2_root: str
    summary: Summary
    skipped: tuple[Skip, ...]
    dropped_share_passwords: tuple[str, ...]
    quota_overruns: tuple[str, ...]
    settings_fallbacks: tuple[str, ...]
    dropped_settings: tuple[str, ...]
    checks: tuple[Check, ...]
    elapsed_seconds: float
    aborted_reason: str | None = None
    aborted_details: tuple[str, ...] = ()
    extra_notes: tuple[str, ...] = field(default_factory=tuple)
    bootstrap_admin: BootstrapAdmin | None = None

    @property
    def succeeded(self) -> bool:
        return self.aborted_reason is None and all(check.passed for check in self.checks)


def summarize(
    plan: Plan, write: WritePlan, migrated_bytes: int
) -> Summary:
    by_kind: dict[str, int] = {}
    for content in write.contents:
        by_kind[content.kind] = by_kind.get(content.kind, 0) + 1
    return Summary(
        users_created=len(write.users),
        users_skipped=sum(1 for skip in write.skipped if skip.kind == "user"),
        contents_created=len(write.contents),
        contents_by_kind=by_kind,
        user_files_created=len(write.user_files),
        user_files_skipped=sum(1 for skip in write.skipped if skip.kind == "user_file"),
        shares_created=len(write.shares),
        shares_skipped=sum(1 for skip in write.skipped if skip.kind == "share"),
        migrated_bytes=migrated_bytes,
        unregistered_store_files=plan.unregistered_store_files,
    )


@dataclass(frozen=True, slots=True)
class Observed:
    """写入后从 V2 库读回的事实；`--dry-run` 时为空，对应项标记为预演。"""

    counts: dict[str, int] | None = None
    content_bytes: int | None = None
    quota_used: dict[int, int] | None = None
    settings: dict[str, object] | None = None


def build_checks(
    *,
    plan: Plan,
    write: WritePlan,
    observed: Observed,
    user_ids: dict[int, int],
    expected_settings: dict[str, object] | None,
) -> tuple[Check, ...]:
    """七项校验：用户、用户文件、分享、内容对象、磁盘字节、配额、设置。"""
    return (
        _count_check("用户数", len(write.users), observed.counts, "users"),
        _count_check("用户文件数", len(write.user_files), observed.counts, "user_files"),
        _count_check("分享数", len(write.shares), observed.counts, "share_links"),
        _count_check("内容对象数", len(write.contents), observed.counts, "content_objects"),
        _bytes_check(write, observed.content_bytes),
        _quota_check(plan, write, observed.quota_used, user_ids),
        _settings_check(observed.settings, expected_settings),
    )


def _count_check(
    label: str, expected: int, observed: dict[str, int] | None, key: str
) -> Check:
    if observed is None:
        return Check(label, str(expected), "（预演未写入）", True)
    actual = observed[key]
    return Check(label, str(expected), str(actual), actual == expected)


def _bytes_check(write: WritePlan, observed_bytes: int | None) -> Check:
    expected = sum(content.size for content in write.contents)
    if observed_bytes is None:
        return Check("磁盘字节数", str(expected), "（预演未写入）", True)
    return Check("磁盘字节数", str(expected), str(observed_bytes), observed_bytes == expected)


def _quota_check(
    plan: Plan,
    write: WritePlan,
    observed_quota: dict[int, int] | None,
    user_ids: dict[int, int],
) -> Check:
    """逐账号比对：V1 侧的账号名 → 期望 used_bytes，与实际写进库的值对齐。"""
    names = {user.v1_id: user.username for user in plan.users}
    expected = {
        v1_id: used
        for v1_id, used in write.used_bytes.items()
        if v1_id in names and v1_id in user_ids
    }
    if observed_quota is None:
        total = sum(expected.values())
        return Check("配额用量", str(total), "（预演未写入）", True)
    if not expected:
        return Check("配额用量", "无可比对的账号", "（无账号）", True)
    mismatched = sorted(
        names[v1_id]
        for v1_id, used in expected.items()
        if observed_quota.get(user_ids[v1_id], -1) != used
    )
    detail = "一致" if not mismatched else f"不一致：{', '.join(mismatched)}"
    return Check("配额用量", f"{len(expected)} 个账号", detail, not mismatched)


def _settings_check(
    observed: dict[str, object] | None, expected: dict[str, object] | None
) -> Check:
    if observed is None or expected is None:
        return Check("系统设置", "与 V1 一致", "（预演未写入）", True)
    mismatched = sorted(
        name for name, value in expected.items() if observed.get(name) != value
    )
    detail = "一致" if not mismatched else f"不一致：{', '.join(mismatched)}"
    return Check("系统设置", "与 V1 一致", detail, not mismatched)


def quota_overruns(plan: Plan, write: WritePlan) -> tuple[str, ...]:
    by_v1 = {user.v1_id: user for user in plan.users}
    overruns: list[str] = []
    for v1_id, used in sorted(write.used_bytes.items()):
        user = by_v1[v1_id]
        if used > user.quota_bytes:
            overruns.append(
                f"{user.username}：配额 {_gb(user.quota_bytes)}，实际 {_gb(used)}"
            )
    return tuple(overruns)


def _gb(value: int) -> str:
    return f"{value / 1024**3:.1f} GB"


ESTIMATE_BYTES_PER_SECOND = 300 * 1024 * 1024


def estimate_seconds(total_bytes: int) -> float:
    """按 300 MB/s 读盘速度粗估耗时，只用于让运维心里有数。"""
    return total_bytes / ESTIMATE_BYTES_PER_SECOND


def render(report: Report) -> str:
    summary = report.summary
    lines: list[str] = [
        "V1 → V2 迁移报告",
        "==================",
        f"模式: {report.mode}",
        f"V1 数据库: {report.v1_database}（版本 {report.v1_version}，"
        f"integrity_check: {report.integrity_check}）",
        f"V1 下载目录: {report.v1_download_dir}",
        f"V2 根目录: {report.v2_root}",
        "",
        "汇总",
        f" 用户:      创建 {summary.users_created}，跳过 {summary.users_skipped}",
        f" 内容对象:  创建 {summary.contents_created}"
        f"（文件 {summary.contents_by_kind.get('file', 0)}，"
        f"目录 {summary.contents_by_kind.get('directory', 0)}）",
        f" 用户文件:  创建 {summary.user_files_created}，跳过 {summary.user_files_skipped}",
        f" 分享:      创建 {summary.shares_created}，跳过 {summary.shares_skipped}",
        f" 迁移字节:  {summary.migrated_bytes}（约 {_gb(summary.migrated_bytes)}）",
        f" 读盘预估:  约 {estimate_seconds(summary.migrated_bytes):.0f} 秒（按 300 MB/s）",
        f" 实际耗时:  {report.elapsed_seconds:.1f} 秒",
        f" V1 store 目录下未登记文件: {summary.unregistered_store_files} 个（未迁移）",
        "",
    ]
    lines.extend(_render_groups(report))
    lines.extend(["校验"])
    for check in report.checks:
        verdict = "PASS" if check.passed else "FAIL"
        lines.append(f" {check.label} {check.expected} == {check.actual}   {verdict}")
    lines.append("")
    if report.aborted_reason is not None:
        lines.append(f"结论: 迁移中止 — {report.aborted_reason}")
        lines.extend(f" {detail}" for detail in report.aborted_details)
    elif report.succeeded:
        lines.append("结论: 迁移成功")
        lines.extend(_render_bootstrap_admin(report.bootstrap_admin))
    else:
        lines.append("结论: 迁移数据与 V1 侧统计不一致，请核对上面的 FAIL 项")
    if report.extra_notes:
        lines.append("")
        lines.extend(report.extra_notes)
    return "\n".join(lines) + "\n"


def _render_bootstrap_admin(admin: BootstrapAdmin | None) -> list[str]:
    """管理员引导说明：只写用户名与 id，密码只在终端出现（见 __main__.py）。"""
    if admin is None:
        return ["注意: 本次迁移没有留下任何账号，迁移后的 V2 里无人能登录。"]
    identifier = (
        f"{admin.username}" if admin.v2_id is None else f"{admin.username} (id={admin.v2_id})"
    )
    lines = ["管理员引导"]
    if admin.dry_run:
        verb = "提升为管理员" if admin.promoted else "设为可登录"
        lines.append(
            f" 正式执行时会把账号 {identifier} {verb}，并只在终端打印一次随机密码；预演不生成密码，也不写入本报告。"
        )
        if admin.promoted:
            lines.append(" V1 里没有任何管理员，因此选的是最早的账号。")
        lines.append(
            f" 其余 {admin.placeholder_accounts} 个账号为占位密码，需由该管理员设置临时密码后才能登录。"
            if admin.placeholder_accounts
            else " 没有其他账号需要设置临时密码。"
        )
        return lines
    if admin.promoted:
        lines.append(
            f" V1 里没有任何管理员，已把最早的账号 {identifier} 提升为管理员，密码只在终端显示，不写入本报告。"
        )
    else:
        lines.append(
            f" 已将账号 {identifier} 设为可登录，密码只在终端显示，不写入本报告。"
        )
    if admin.placeholder_accounts:
        lines.append(
            f" 其余 {admin.placeholder_accounts} 个账号为占位密码，需由该管理员设置临时密码后才能登录。"
        )
    else:
        lines.append(" 没有其他账号需要设置临时密码。")
    lines.append(" 用户用临时密码登录后会看到改密横幅，改密时无需输入旧密码。")
    return lines


def _render_groups(report: Report) -> list[str]:
    lines: list[str] = []
    groups = (
        ("user", "跳过的账号"),
        ("content", "跳过的内容"),
        ("user_file", "跳过的用户文件"),
        ("share", "跳过的分享"),
    )
    for kind, heading in groups:
        items = [skip for skip in report.skipped if skip.kind == kind]
        if not items:
            continue
        lines.append(f"{heading}（共 {len(items)} 条）")
        for skip in items[:MAX_LISTED_ITEMS]:
            lines.append(f" {skip.label}  理由: {skip.reason}")
        if len(items) > MAX_LISTED_ITEMS:
            lines.append(f" …另有 {len(items) - MAX_LISTED_ITEMS} 条未列出")
        lines.append("")
    if report.dropped_share_passwords:
        lines.append(f"失效密码的分享（迁移后为无密码，共 {len(report.dropped_share_passwords)} 条）")
        lines.extend(f" code={code}" for code in report.dropped_share_passwords)
        lines.append("")
    if report.quota_overruns:
        lines.append("配额超出上限的用户")
        lines.extend(f" {item}" for item in report.quota_overruns)
        lines.append("")
    if report.settings_fallbacks:
        lines.append("回退为 V2 默认值的设置项")
        lines.extend(f" {item}" for item in report.settings_fallbacks)
        lines.append("")
    if report.dropped_settings:
        lines.append("未迁移的 V1 设置项")
        lines.extend(f" {name}" for name in report.dropped_settings)
        lines.append("")
    return lines


def write_report(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
