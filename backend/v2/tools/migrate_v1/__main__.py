"""命令行入口：前置校验、编排、退出码。

退出码：0 成功；1 迁移中止（冲突、结构不符、目标目录非空、校验不一致）；2 参数错误。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from aria2deck.domain.settings import SystemSettings

from . import MigrationAbort
from .contents import migrate_contents
from .planner import Plan, WritePlan, build_plan, finalize
from .reader import read_snapshot
from .report import (
    BootstrapAdmin,
    Observed,
    Report,
    Summary,
    build_checks,
    quota_overruns,
    render,
    summarize,
    write_report,
)
from .settings_map import SettingsOutcome, map_settings
from .writer import ObservedState, read_back, write_v2

SPACE_MARGIN = 1.05
BYTES_PER_SECOND = 300 * 1024 * 1024
JSON_COLUMNS = {
    "download_connection_limits_json": "download_connection_limits",
    "tracker_fixed_list_json": "tracker_fixed_list",
    "tracker_remote_urls_json": "tracker_remote_urls",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.migrate_v1",
        description=(
            "把 V1 的账号、文件内容、分享与系统设置离线迁移到 V2 目标目录。\n"
            "V1 侧只读，不改动任何 V1 数据；建议先跑 --dry-run 看报告。\n"
            "迁移会给最早的管理员生成一个随机密码并只在终端打印一次，其余账号写为锁定状态，"
            "须由该管理员逐个重置密码后解锁；带密码的分享会变成无密码。"
        ),
    )
    parser.add_argument("--v1-database", required=True, help="V1 的 .db 文件路径")
    parser.add_argument(
        "--v1-download-dir", required=True, help="V1 下载目录（其下有 store/）"
    )
    parser.add_argument(
        "--v2-root", required=True, help="V2 目标根目录；工具在其中建 data/ 与 downloads/"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="完整走一遍流程但不写任何文件，只输出报告",
    )
    parser.add_argument(
        "--link",
        action="store_true",
        help="用硬链接代替复制（零额外空间；要求 V1 下载目录与 --v2-root 在同一文件系统）",
    )
    parser.add_argument("--report", help="把报告写入指定文件；不传则只打印到标准输出")
    return parser


def _check_inputs(database: Path, download_dir: Path) -> None:
    if not database.is_file():
        raise MigrationAbort("V1 数据库文件不存在", (str(database),))
    store = download_dir / "store"
    if not store.is_dir():
        raise MigrationAbort("V1 下载目录下没有 store/ 子目录", (str(store),))


def _check_target(v2_root: Path) -> None:
    if v2_root.exists() and not v2_root.is_dir():
        raise MigrationAbort("V2 目标路径已存在且不是目录", (str(v2_root),))
    if v2_root.is_dir() and any(v2_root.iterdir()):
        raise MigrationAbort("V2 目标目录非空，拒绝覆盖已有部署", (str(v2_root),))


def _check_space(v2_root: Path, required_bytes: int) -> None:
    probe = v2_root
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    needed = int(required_bytes * SPACE_MARGIN)
    if free < needed:
        raise MigrationAbort(
            "目标位置剩余空间不足",
            (f"需要约 {needed} 字节，实际可用 {free} 字节",),
        )


def _settings_view(settings: SystemSettings) -> dict[str, object]:
    """把 V2 设置对象摊平成「列名 → 期望值」，供写入后逐项比对。"""
    view: dict[str, object] = {
        "site_title": settings.site_title,
        "aria2_rpc_url": settings.aria2_rpc_url,
        "pack_format": settings.pack_format,
        "pack_compression_level": settings.pack_compression_level,
        "history_retention_days": settings.history_retention_days,
        "authenticated_api_rate_limit": settings.authenticated_api_rate_limit,
        "rpc_rate_limit": settings.rpc_rate_limit,
        "create_task_rate_limit": settings.create_task_rate_limit,
        "aria2_rpc_secret_ciphertext": None,
    }
    for column, field in JSON_COLUMNS.items():
        view[column] = json.dumps(
            getattr(settings, field), ensure_ascii=False, separators=(",", ":")
        )
    return view


def _to_observed(state: ObservedState) -> Observed:
    return Observed(
        counts=state.counts,
        content_bytes=state.content_bytes,
        quota_used=state.quota_used,
        settings=state.settings,
    )


def _summary(plan: Plan, write: WritePlan, migrated_bytes: int) -> Summary:
    return summarize(plan, write, migrated_bytes)


def _bootstrap_admin(
    write: WritePlan, user_ids: dict[int, int], *, dry_run: bool
) -> BootstrapAdmin | None:
    """把引导管理员折成报告用的视图；没有账号时返回 None。"""
    chosen = next((user for user in write.users if user.accepts_generated_password), None)
    if chosen is None:
        return None
    return BootstrapAdmin(
        username=chosen.username,
        v2_id=user_ids.get(chosen.v1_id),
        promoted=chosen.promoted_to_admin,
        placeholder_accounts=sum(
            1 for user in write.users if not user.accepts_generated_password
        ),
        dry_run=dry_run,
    )


BOOTSTRAP_TITLE = "管理员登录凭据（仅本次显示，请立即保存）"
HORIZONTAL_RULE = "=" * 70

# 只进报告（终端与 --report 文件），预演不打印：还没写库，说「必须去改」会误导。
RPC_SECRET_WARNING = (
    "必须重新配置 aria2 RPC 密钥（否则下载提交会全部失败）\n"  # noqa: S105  # 提示文案，不是密钥
    " V1 的 aria2 RPC 密钥无法迁移：V1 与 V2 使用不同的加密体系，V1 的密文在 V2 里解不开，"
    "所以 V2 系统设置里的 RPC 密钥为空。\n"
    " 部署后请用管理员账号登录，进入「系统设置」→「aria2 后端」，填写「RPC Secret」，"
    "再点「测试连接」确认连接成功。\n"
    " 在完成这一步之前，所有下载任务的提交都会失败。"
)


def render_bootstrap_admin(credentials: tuple[str, str]) -> str:
    """终端专用凭据段；绝不经 `render()`，因此不可能进 `--report` 文件。"""
    username, password = credentials
    return "\n".join(
        (
            f"{HORIZONTAL_RULE[:16]} {BOOTSTRAP_TITLE} {HORIZONTAL_RULE[:16]}",
            f"用户名: {username}",
            f"密  码: {password}",
            "用它登录后，在「用户管理」页为其他账号设置临时密码。",
            HORIZONTAL_RULE,
        )
    ) + "\n"


@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    report: Report
    # 引导管理员的（用户名, 明文密码）；预演与中止时为 None。
    # 只交给 `main` 打到终端，绝不进 `render()` / `write_report()`。
    bootstrap_credentials: tuple[str, str] | None = None


def run_migration(
    *,
    v1_database: Path,
    v1_download_dir: Path,
    v2_root: Path,
    dry_run: bool,
    link: bool = False,
) -> MigrationOutcome:
    """跑完整流程并返回报告与一次性凭据；任何一步失败都抛 `MigrationAbort`。"""
    started = time.monotonic()
    _check_inputs(v1_database, v1_download_dir)
    _check_target(v2_root)
    snapshot = read_snapshot(v1_database)
    plan = build_plan(snapshot, v1_download_dir)
    if not link:
        _check_space(v2_root, plan.planned_bytes)
    mapped: SettingsOutcome = map_settings(snapshot.settings)
    migration = migrate_contents(plan, v2_root, dry_run=dry_run, link=link)
    write = finalize(plan, migration)
    migrated_bytes = sum(content.size for content in write.contents)
    observed = Observed()
    user_ids: dict[int, int] = {}
    notes: tuple[str, ...] = ()
    credentials: tuple[str, str] | None = None
    if not dry_run:
        result = asyncio.run(write_v2(v2_root, write, mapped.settings))
        observed = _to_observed(asyncio.run(read_back(v2_root)))
        user_ids = result.user_ids
        credentials = result.bootstrap_admin
    checks = build_checks(
        plan=plan,
        write=write,
        observed=observed,
        user_ids=user_ids,
        expected_settings=None if dry_run else _settings_view(mapped.settings),
    )
    if not dry_run:
        notes = (
            f"迁移后请把 {v2_root}/data 与 {v2_root}/downloads 放到 V2 部署位置。",
            RPC_SECRET_WARNING,
        )
    mode = "预演（--dry-run）" if dry_run else "正式执行"
    if link:
        mode += "＋硬链接"
    report = Report(
        mode=mode,
        v1_database=str(v1_database),
        v1_version=snapshot.version,
        integrity_check=snapshot.integrity_check,
        v1_download_dir=str(v1_download_dir),
        v2_root=str(v2_root),
        summary=_summary(plan, write, migrated_bytes),
        skipped=write.skipped,
        dropped_share_passwords=write.dropped_share_passwords,
        quota_overruns=quota_overruns(plan, write),
        settings_fallbacks=mapped.fallbacks,
        dropped_settings=mapped.dropped,
        checks=checks,
        elapsed_seconds=elapsed_since(started),
        extra_notes=notes,
        bootstrap_admin=_bootstrap_admin(write, user_ids, dry_run=dry_run),
    )
    return MigrationOutcome(report, credentials)


def elapsed_since(start: float) -> float:
    return max(time.monotonic() - start, 0.0)


def aborting_report(
    *,
    abort: MigrationAbort,
    v1_database: Path,
    v1_download_dir: Path,
    v2_root: Path,
    dry_run: bool,
) -> Report:
    return Report(
        mode="预演（--dry-run）" if dry_run else "正式执行",
        v1_database=str(v1_database),
        v1_version=0,
        integrity_check="未校验",
        v1_download_dir=str(v1_download_dir),
        v2_root=str(v2_root),
        summary=Summary(0, 0, 0, {}, 0, 0, 0, 0, 0, 0),
        skipped=(),
        dropped_share_passwords=(),
        quota_overruns=(),
        settings_fallbacks=(),
        dropped_settings=(),
        checks=(),
        elapsed_seconds=0.0,
        aborted_reason=abort.message,
        aborted_details=abort.details,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)
    v1_database = Path(options.v1_database).expanduser()
    v1_download_dir = Path(options.v1_download_dir).expanduser()
    v2_root = Path(options.v2_root).expanduser()
    dry_run = bool(options.dry_run)
    credentials: tuple[str, str] | None = None
    try:
        outcome = run_migration(
            v1_database=v1_database,
            v1_download_dir=v1_download_dir,
            v2_root=v2_root,
            dry_run=dry_run,
            link=bool(options.link),
        )
        report = outcome.report
        credentials = outcome.bootstrap_credentials
    except MigrationAbort as abort:
        report = aborting_report(
            abort=abort,
            v1_database=v1_database,
            v1_download_dir=v1_download_dir,
            v2_root=v2_root,
            dry_run=dry_run,
        )
    text = render(report)
    sys.stdout.write(text)
    if options.report:
        write_report(Path(options.report).expanduser(), text)
    if credentials is not None:
        # 一旦写入成功就必须打印：密钥已经进库，不打出来就永远丢了，
        # 而目标目录非空又禁止重跑，等于再次把所有人锁在门外。
        sys.stdout.write(render_bootstrap_admin(credentials))
    return 0 if report.succeeded else 1


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
