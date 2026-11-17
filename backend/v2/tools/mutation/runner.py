"""变异测试运行器：逐个注入行为缺陷，检查现有测试能否发现。

用法：

    cd backend/v2 && uv run python -m tools.mutation.runner
    cd backend/v2 && uv run python -m tools.mutation.runner --only files分页上界放开
    cd backend/v2 && uv run python -m tools.mutation.runner --json /tmp/mutation.json

安全约束（优先于功能完整）：

- 启动时要求工作树干净，否则拒绝运行，避免与未提交改动冲突。
- 每条变异在 try/finally 中还原，`atexit` 兜底，中断也会写回原文。
- 同一 `old` 在文件里出现多次且未指定 `line` 时拒绝执行该条，避免改错地方。

结果分类：

- KILLED   测试变红，说明该缺陷会被发现（期望结果）
- SURVIVED 测试仍绿，说明存在测试缺口
- SKIP     原文未匹配或定位不唯一，变异未注入（不算成功也不算失败）
- TIMEOUT  超时，保守视为未能发现
"""

from __future__ import annotations

import argparse
import atexit
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .catalog import EXCLUDED, MUTANTS, Mutant

BACKEND_ROOT = Path(__file__).resolve().parents[2]

# 正在被改写的文件：进程异常退出时也要写回原文。
_PENDING: dict[Path, str] = {}


def _restore_all() -> None:
    for path, original in list(_PENDING.items()):
        try:
            path.write_text(original, encoding="utf-8")
        except OSError:
            print(f"还原失败：{path}", file=sys.stderr)
        finally:
            _PENDING.pop(path, None)


atexit.register(_restore_all)


@dataclass(slots=True)
class Outcome:
    name: str
    verdict: str
    detail: str
    seconds: float


def _git_status_clean() -> bool:
    result = subprocess.run(  # noqa: S603  # 参数固定，仅读取 git 状态
        ["git", "status", "--porcelain", "--", "src"],  # noqa: S607
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and not result.stdout.strip()


def _apply(mutant: Mutant) -> tuple[bool, str]:
    """注入变异。返回 (是否成功, 说明)。"""
    path = BACKEND_ROOT / mutant.path
    if not path.exists():
        return False, f"文件不存在：{mutant.path}"
    original = path.read_text(encoding="utf-8")
    occurrences = original.count(mutant.old)
    if occurrences == 0:
        return False, "原文未匹配"
    if mutant.line is not None:
        lines = original.splitlines(keepends=True)
        start = mutant.line - 1
        span = mutant.old.count("\n") + 1
        end = start + span
        if start < 0 or end > len(lines):
            return False, f"第 {mutant.line} 行起的 {span} 行超出文件范围"
        block = "".join(lines[start:end])
        if mutant.old not in block:
            return False, f"第 {mutant.line} 行起的 {span} 行不含目标原文"
        replacement = block.replace(mutant.old, mutant.new, 1)
        mutated = "".join(lines[:start]) + replacement + "".join(lines[end:])
    elif occurrences == 1:
        mutated = original.replace(mutant.old, mutant.new, 1)
    else:
        return False, f"原文出现 {occurrences} 次且未指定 line，拒绝注入"
    _PENDING[path] = original
    path.write_text(mutated, encoding="utf-8")
    return True, "已注入"


def _revert(mutant: Mutant) -> None:
    path = BACKEND_ROOT / mutant.path
    original = _PENDING.pop(path, None)
    if original is not None:
        path.write_text(original, encoding="utf-8")


def _run_tests(mutant: Mutant, timeout: float) -> Outcome:
    started = time.monotonic()
    applied, detail = _apply(mutant)
    if not applied:
        return Outcome(mutant.name, "SKIP", detail, 0.0)
    try:
        completed = subprocess.run(  # noqa: S603,S607  # 命令与测试路径均来自仓库内清单
            ["uv", "run", "pytest", *mutant.tests, "-q", "--no-header", "-p", "no:cacheprovider"],  # noqa: S607
            cwd=BACKEND_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.monotonic() - started
        summary = ""
        for line in reversed((completed.stdout or "").splitlines()):
            if "passed" in line or "failed" in line or "error" in line:
                summary = line.strip()
                break
        if completed.returncode == 0:
            return Outcome(mutant.name, "SURVIVED", summary, elapsed)
        return Outcome(mutant.name, "KILLED", summary, elapsed)
    except subprocess.TimeoutExpired:
        return Outcome(mutant.name, "TIMEOUT", f"超过 {timeout:.0f}s", time.monotonic() - started)
    finally:
        _revert(mutant)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.mutation.runner",
        description="变异测试：逐个注入行为缺陷，检查现有测试能否发现。",
        epilog=(
            "前置条件：工作树中 src/ 无未提交改动；aria2 无需运行；不访问网络。\n"
            "全量约 15-20 分钟。不进入 make check 与 CI，按需人工触发。\n"
            "退出码：0=全部 KILLED，1=存在 SURVIVED/SKIP/TIMEOUT。"
        ),
    )
    parser.add_argument("--only", action="append", default=[], help="只跑名称包含该子串的变异，可重复")
    parser.add_argument("--json", type=Path, help="把结果写入 JSON 文件")
    parser.add_argument("--timeout", type=float, default=300.0, help="单条变异超时秒数，默认 300")
    parser.add_argument("--list", action="store_true", help="只列出变异清单不执行")
    parser.add_argument("--allow-dirty", action="store_true", help="跳过工作树干净的检查（不推荐）")
    arguments = parser.parse_args(argv)

    if arguments.list:
        for mutant in MUTANTS:
            print(f"{mutant.name:32} {mutant.path}:{mutant.line or '-'}")
            print(f"    {mutant.rationale}")
        if EXCLUDED:
            print("\n=== 已排除（不收录进清单）===")
            for name, reason in EXCLUDED:
                print(f"{name:32} {reason}")
        return 0

    if not arguments.allow_dirty and not _git_status_clean():
        print("src/ 存在未提交改动，拒绝运行：变异会改写源码，需先提交或暂存。", file=sys.stderr)
        print("确认无风险可用 --allow-dirty 跳过该检查。", file=sys.stderr)
        return 2

    selected = [
        mutant
        for mutant in MUTANTS
        if not arguments.only or any(token in mutant.name for token in arguments.only)
    ]
    if not selected:
        print("没有匹配的变异。", file=sys.stderr)
        return 2

    print(f"共 {len(selected)} 条变异，单条超时 {arguments.timeout:.0f}s\n")
    outcomes: list[Outcome] = []
    for mutant in selected:
        print(f"[{len(outcomes) + 1}/{len(selected)}] {mutant.name} ... ", end="", flush=True)
        outcome = _run_tests(mutant, arguments.timeout)
        outcomes.append(outcome)
        print(f"{outcome.verdict}  {outcome.detail}", flush=True)

    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.verdict] = counts.get(outcome.verdict, 0) + 1
    print("\n=== 汇总 ===")
    for verdict in ("KILLED", "SURVIVED", "SKIP", "TIMEOUT"):
        if verdict in counts:
            print(f"{verdict:9} {counts[verdict]}")

    bad = [outcome for outcome in outcomes if outcome.verdict != "KILLED"]
    if bad:
        print("\n=== 需要处理 ===")
        for outcome in bad:
            print(f"{outcome.verdict:9} {outcome.name}: {outcome.detail}")

    if arguments.json is not None:
        arguments.json.parent.mkdir(parents=True, exist_ok=True)
        arguments.json.write_text(
            json.dumps([asdict(outcome) for outcome in outcomes], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\n结果已写入 {arguments.json}")

    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
