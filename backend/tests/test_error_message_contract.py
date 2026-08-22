"""M17 合同测试：报错文案带数锁 + 跨路径一致性（AC-4 / AC-2）。

两层锁：
1. 黑名单层：禁止旧静态文案字面量回归到扫描模块。
2. AST 层：比较类报错位置（raise 参数 / message 赋值 / dict value）命中关键词的
   裸字符串常量必须进白名单，强制新报错点经 error_text 函数或带数 f-string。

注意：pack/__init__.py 映射处保留 ``exc.message or "磁盘可用空间不足"`` /
``"空间不足，无法冻结打包输出空间"`` 两条默认文案兜底（两个 raise 点均恒传
message，兜底不可达），故黑名单使用更长的精确串且不含这两个裸串。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads
from app.domain.error_text import fmt_gb, over_limit
from app.modules.backend.port import BackendPort
from app.modules.task_core.policy import Decision, apply_decision
from app.modules.task_core.register import _quota_over_total_message
from app.modules.task_core.states import ERROR_QUOTA_EXCEEDED
from app.services import task_orchestration as orchestration
from app.services.rpc import _shared as rpc_shared
from app.services.gateway import GatewaySizeExceeded, _stream_budget
from tests.helpers_v0 import create_global_download_v0, create_user_v0

BACKEND_DIR = Path(__file__).resolve().parent.parent

SCAN_MODULES = [
    "app/domain/error_text.py",
    "app/modules/task_core/register.py",
    "app/modules/task_core/policy.py",
    "app/repositories/task/downloads.py",
    "app/repositories/pack.py",
    "app/modules/pack/__init__.py",
    "app/services/task_orchestration.py",
    "app/services/gateway.py",
    "app/services/rpc/_shared.py",
    "app/services/user_service.py",
    "app/repositories/auth.py",
    "app/services/settings_service.py",
]

# 旧静态文案精确串（带数改造前的报错原文），出现即 FAIL。
BLACKLISTED_LITERALS = [
    "任务大小超过用户配额",
    "用户配额不足，无法秒传",
    "用户配额不足，无法加入下载",
    "用户配额不足，无法创建任务",
    "文件大小超过用户总配额",
    "空间不足，已取消该订阅任务",
    "打包输出超过预留空间",
    "打包安装副本超过预留空间",
    "磁盘可用空间不足，无法安装打包输出",
    "下载内容超过系统大小限制",
    "Range 起点超过系统大小限制",
    "可用空间不足，无法添加磁力链接",
    "Your quota has been exceeded",
    "Disk space not enough",
    "用户配额不能低于当前已用空间与冻结空间之和",
    # 磁盘可用空间不足（裸串）与 空间不足，无法冻结打包输出空间 不列入：
    # pack 映射默认文案兜底保留（不可达），见模块 docstring。
]

KEYWORD_RE = re.compile(r"配额|磁盘|空间不足|系统限制|大小限制|预留")

# AST 层白名单：存量非比较类裸常量（无数值可带的兜底），逐条注明理由。
AST_WHITELIST = {
    # policy.apply_decision 手工构造 Decision（无 total/quota）时的兜底；
    # 正常路径数值恒填充并经 over_limit 生成。
    "文件大小超过用户配额（数值未知）",
}

MSG_KEYWORDS = ("message", "text", "detail", "error_message")
DICT_MSG_KEYS = ("error_message", "message", "detail", "detail_message")


def _string_value(node: ast.AST) -> str | None:
    """裸字符串常量：Constant，或无 FormattedValue 的 JoinedStr。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values and not any(
        isinstance(v, ast.FormattedValue) for v in node.values
    ):
        parts = [v.value for v in node.values]
        return "".join(p for p in parts if isinstance(p, str))
    return None


def _raise_bare_strings(node: ast.Raise) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    if node.exc is None:
        return hits
    candidates: list[ast.AST] = [node.exc]
    if isinstance(node.exc, ast.Call):
        candidates += list(node.exc.args)
        candidates += [
            kw.value for kw in node.exc.keywords if kw.arg in MSG_KEYWORDS
        ]
    for cand in candidates:
        value = _string_value(cand)
        if value is not None and KEYWORD_RE.search(value):
            hits.append((value, cand.lineno))
    return hits


def _collect_bare_strings(source: str) -> list[tuple[str, int]]:
    """收集 raise / message 赋值 / dict value 中命中关键词的裸字符串常量。"""
    hits: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Raise):
            hits += _raise_bare_strings(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id in ("error_message", "message")
                ):
                    value = _string_value(node.value)
                    if value is not None and KEYWORD_RE.search(value):
                        hits.append((value, target.lineno))
        elif isinstance(node, ast.Dict):
            for key, val in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value in DICT_MSG_KEYS
                ):
                    value = _string_value(val)
                    if value is not None and KEYWORD_RE.search(value):
                        hits.append((value, val.lineno))
    return hits


def _module_sources() -> list[tuple[str, str]]:
    return [
        (rel, (BACKEND_DIR / rel).read_text(encoding="utf-8"))
        for rel in SCAN_MODULES
    ]


def test_blacklist_no_legacy_static_literals() -> None:
    """第 1 层：旧静态文案字面量不得回归扫描模块。"""
    offenders = []
    for rel, source in _module_sources():
        for lineno, line in enumerate(source.splitlines(), start=1):
            for literal in BLACKLISTED_LITERALS:
                if literal in line:
                    offenders.append((rel, lineno, literal))
    assert not offenders, f"旧静态文案回归: {offenders}"


def test_ast_bare_keyword_strings_whitelisted() -> None:
    """第 2 层：比较类报错位置的裸常量必须带数或进白名单。"""
    offenders = []
    for rel, source in _module_sources():
        for value, lineno in _collect_bare_strings(source):
            if value not in AST_WHITELIST:
                offenders.append((rel, lineno, value))
    assert not offenders, f"裸常量报错文案未带数: {offenders}"


def test_whitelist_entries_actually_used() -> None:
    """白名单不留死条目：每条必须在扫描模块中真实存在。"""
    joined = "\n".join(source for _, source in _module_sources())
    for entry in AST_WHITELIST:
        assert entry in joined


# --- T5 跨路径一致性（AC-2） -----------------------------------------------

SIZE = 15 * 1024**3
LIMIT = 10 * 1024**3


def test_t5_quota_register_and_policy_paths_agree() -> None:
    """配额：register E-3a 与 policy E-4 同款文案、数值一致。"""
    register_msg = _quota_over_total_message(SIZE, LIMIT)
    expected = over_limit("文件大小", SIZE, "超过用户配额", LIMIT)
    assert register_msg == expected


@pytest.mark.asyncio
async def test_t5_quota_policy_path_writes_same_message(temp_db: str) -> None:
    """policy E-4 实路径写入的 error_message 与 register E-3a 一致。"""
    await create_user_v0(username="contract-quota")
    gd = await create_global_download_v0(
        resource_key="http://example.com/contract.bin",
        source_uri="http://example.com/contract.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid-contract",
        total_bytes=SIZE,
        size_known=True,
    )
    backend = AsyncMock(spec=BackendPort)
    await apply_decision(
        backend,
        gd["id"],
        Decision(
            "terminal_quota_exceeded",
            error_code=ERROR_QUOTA_EXCEEDED,
            terminal=True,
            total_bytes=SIZE,
            quota_bytes=LIMIT,
        ),
    )
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(global_downloads).where(global_downloads.c.id == gd["id"])
            )
        ).mappings().one()
    assert row["error_message"] == _quota_over_total_message(SIZE, LIMIT)


@pytest.mark.asyncio
async def test_t5_disk_orchestration_and_rpc_paths_agree(
    temp_db: str, monkeypatch
) -> None:
    """磁盘：orchestration E-11 与 rpc _shared E-8 磁盘分支同款文案。"""
    free, min_free = 20 * 1024**3, 32 * 1024**3
    expected = f"磁盘空间不足，剩余 {fmt_gb(free)}，低于最小预留 {fmt_gb(min_free)}"

    assert orchestration._disk_insufficient_message(free, min_free) == expected

    monkeypatch.setattr(
        rpc_shared.shutil, "disk_usage", lambda _p: type("U", (), {"free": free})
    )
    monkeypatch.setattr(rpc_shared, "get_min_free_disk", lambda: min_free)
    with pytest.raises(rpc_shared.RpcError) as excinfo:
        await rpc_shared._check_quota_and_disk(user_id=1)
    assert str(excinfo.value.message) == expected


def test_t5_system_limit_labels_share_numeric_format() -> None:
    """系统限制：orchestration"文件大小" 与 gateway E-9"下载内容" 数值格式一致。"""
    class _FakeResponse:
        status = 200
        headers = {"Content-Length": str(SIZE)}

    with pytest.raises(GatewaySizeExceeded) as excinfo:
        _stream_budget(_FakeResponse(), None, LIMIT)  # type: ignore[arg-type]
    gateway_msg = str(excinfo.value)

    orchestration_msg = over_limit("文件大小", SIZE, "超过系统限制", LIMIT)
    # 标签不同（下载内容 vs 文件大小 / 系统大小限制 vs 系统限制），
    # 但两个数值与判定结构必须逐字一致。
    assert gateway_msg == over_limit(
        "下载内容", SIZE, "超过系统大小限制", LIMIT
    )
    assert gateway_msg.replace("下载内容", "文件大小").replace(
        "超过系统大小限制", "超过系统限制"
    ) == orchestration_msg
    assert f"{fmt_gb(SIZE)}" in gateway_msg and f"{fmt_gb(LIMIT)}" in gateway_msg
