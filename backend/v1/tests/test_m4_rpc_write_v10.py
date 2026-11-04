"""M4 T14: services/rpc/write.py AST boundary tests.

Verifies that the aria2 RPC write methods (addUri / addTorrent / remove /
forceRemove) live in ``app/services/rpc/write.py``, and that the legacy
``services/aria2_rpc_handler.py`` no longer defines the non-trivial
implementations locally.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
WRITE_PATH = BACKEND_ROOT / "app" / "services" / "rpc" / "write.py"
LEGACY_HANDLER_PATH = BACKEND_ROOT / "app" / "services" / "rpc" / "system.py"

EXPECTED_WRITE_METHODS = {
    "_handle_add_uri",
    "_handle_add_torrent",
    "_handle_remove",
    "_handle_force_remove",
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


def test_rpc_write_module_exists_and_defines_write_methods() -> None:
    """AST boundary: services/rpc/write.py defines the write methods."""
    assert WRITE_PATH.is_file(), (
        f"services/rpc/write.py must exist: {WRITE_PATH}"
    )
    defined = _defined_function_names(WRITE_PATH)
    missing = EXPECTED_WRITE_METHODS - defined
    assert not missing, (
        f"write methods missing from rpc/write.py: {sorted(missing)}"
    )


def test_legacy_handler_write_methods_are_thin_delegates() -> None:
    """AST boundary: legacy handler keeps at most thin delegates.

    ``Aria2RpcHandler`` may keep compatibility wrappers (existing tests
    call ``handler.handle(...)`` which dispatches by attribute lookup),
    but each wrapper must be a thin delegate of at most a few lines;
    the real implementation lives in ``services/rpc/write.py``.
    """
    if not LEGACY_HANDLER_PATH.is_file():
        return
    body_lines = _class_method_body_lines(
        LEGACY_HANDLER_PATH, "Aria2RpcHandler", EXPECTED_WRITE_METHODS
    )
    for name, lines in body_lines.items():
        assert lines <= 6, (
            f"aria2_rpc_handler.Aria2RpcHandler.{name} is {lines} lines; "
            "implementation must live in services/rpc/write.py "
            "(only a thin delegate is allowed here)"
        )
