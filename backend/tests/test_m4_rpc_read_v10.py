"""M4 T15: services/rpc/read.py AST boundary tests.

Verifies that the aria2 RPC read methods (tellStatus / tellActive /
tellWaiting / tellStopped / getGlobalStat / getFiles / getUris / getPeers /
getServers / getVersion) live in ``app/services/rpc/read.py``, and that the
legacy ``services/aria2_rpc_handler.py`` keeps only thin delegates.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
READ_PATH = BACKEND_ROOT / "app" / "services" / "rpc" / "read.py"
LEGACY_HANDLER_PATH = BACKEND_ROOT / "app" / "services" / "rpc" / "system.py"

EXPECTED_READ_METHODS = {
    "_handle_tell_status",
    "_handle_tell_active",
    "_handle_tell_waiting",
    "_handle_tell_stopped",
    "_handle_get_global_stat",
    "_handle_get_files",
    "_handle_get_uris",
    "_handle_get_peers",
    "_handle_get_servers",
    "_handle_get_version",
}


def _defined_function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _class_method_body_lines(
    path: Path, class_name: str, method_names: set[str]
) -> dict[str, int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name in method_names
                ):
                    result[item.name] = (
                        (item.end_lineno or item.lineno) - item.lineno + 1
                    )
    return result


def test_rpc_read_module_exists_and_defines_read_methods() -> None:
    """AST boundary: services/rpc/read.py defines the read methods."""
    assert READ_PATH.is_file(), (
        f"services/rpc/read.py must exist: {READ_PATH}"
    )
    defined = _defined_function_names(READ_PATH)
    missing = EXPECTED_READ_METHODS - defined
    assert not missing, (
        f"read methods missing from rpc/read.py: {sorted(missing)}"
    )


def test_legacy_handler_read_methods_are_thin_delegates() -> None:
    """AST boundary: legacy handler keeps at most thin delegates.

    ``Aria2RpcHandler`` may keep compatibility wrappers (existing tests
    call ``handler.handle(...)`` which dispatches by attribute lookup),
    but each wrapper must be a thin delegate of at most a few lines;
    the real implementation lives in ``services/rpc/read.py``.
    """
    if not LEGACY_HANDLER_PATH.is_file():
        return
    body_lines = _class_method_body_lines(
        LEGACY_HANDLER_PATH, "Aria2RpcHandler", EXPECTED_READ_METHODS
    )
    for name, lines in body_lines.items():
        assert lines <= 6, (
            f"aria2_rpc_handler.Aria2RpcHandler.{name} is {lines} lines; "
            "implementation must live in services/rpc/read.py "
            "(only a thin delegate is allowed here)"
        )
