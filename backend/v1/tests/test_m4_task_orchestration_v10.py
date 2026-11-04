"""M4 T12: task_service 拆薄的 AST 边界测试。

规则：

1. ``services/task_orchestration.py`` 存在，且唯一定义编排业务符号
   （register_and_submit / check_disk_space / _validate_options /
   torrent_preview_response / parse_torrent_or_error /
   check_torrent_network_safety / check_url_safety /
   set_task_backend_override / _get_backend / _TolerantBackend /
   raise_register_error / _resolve_join_submission_uris）。
2. ``services/task_service.py`` 行数 < 300，且不再定义上述编排符号
   （HTTP 端点适配层只允许 re-export）。
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
TASK_SERVICE = APP_ROOT / "services" / "task_service.py"
TASK_ORCHESTRATION = APP_ROOT / "services" / "task_orchestration.py"

ORCHESTRATION_SYMBOLS = {
    "register_and_submit",
    "create_task",
    "preview_torrent_task",
    "create_torrent_task",
    "check_disk_space",
    "_validate_options",
    "torrent_preview_response",
    "parse_torrent_or_error",
    "check_torrent_network_safety",
    "check_url_safety",
    "set_task_backend_override",
    "_get_backend",
    "_TolerantBackend",
    "raise_register_error",
    "_resolve_join_submission_uris",
}

ADAPTER_INJECTION_SYMBOLS = {
    "_get_client",
    "_default_client",
}


def test_injection_symbols_documented() -> None:
    assert ADAPTER_INJECTION_SYMBOLS == {"_get_client", "_default_client"}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _top_level_defs(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def test_task_orchestration_module_exists() -> None:
    assert TASK_ORCHESTRATION.is_file(), "services/task_orchestration.py 不存在"


def test_task_orchestration_defines_orchestration_symbols() -> None:
    defined = _top_level_defs(_tree(TASK_ORCHESTRATION))
    missing = ORCHESTRATION_SYMBOLS - defined
    assert not missing, f"task_orchestration.py 缺少编排符号定义: {sorted(missing)}"


def test_backend_injection_points() -> None:
    """_get_client 注入点保留在 task_service；_default_client 在 orchestration。"""
    assert "_get_client" in _top_level_defs(_tree(TASK_SERVICE))
    assert "_default_client" in _top_level_defs(_tree(TASK_ORCHESTRATION))


def test_task_service_no_longer_defines_orchestration_symbols() -> None:
    defined = _top_level_defs(_tree(TASK_SERVICE))
    leaked = ORCHESTRATION_SYMBOLS & defined
    assert not leaked, (
        f"task_service.py 仍定义编排符号（应下沉到 task_orchestration）: {sorted(leaked)}"
    )


def test_task_service_is_thin_http_adapter() -> None:
    line_count = len(TASK_SERVICE.read_text(encoding="utf-8").splitlines())
    assert line_count < 300, (
        f"task_service.py 应为 < 300 行的 HTTP 适配层，当前 {line_count} 行"
    )
