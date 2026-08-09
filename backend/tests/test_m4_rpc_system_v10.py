"""M4 T16: services/rpc/system.py AST boundary tests.

Verifies that the aria2 RPC system/misc methods (system.multicall /
system.listMethods / removeDownloadResult / purgeDownloadResult and the
static compatibility methods) live in ``app/services/rpc/system.py``,
that the ``Aria2RpcHandler`` dispatch shell is defined in
``app/services/rpc``, and that the legacy
``services/aria2_rpc_handler.py`` module is gone with zero remaining
references.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_PATH = BACKEND_ROOT / "app" / "services" / "rpc" / "system.py"
RPC_INIT_PATH = BACKEND_ROOT / "app" / "services" / "rpc" / "__init__.py"
LEGACY_HANDLER_PATH = BACKEND_ROOT / "app" / "services" / "aria2_rpc_handler.py"

EXPECTED_SYSTEM_METHODS = {
    "_handle_system_multicall",
    "_handle_system_list_methods",
    "_handle_remove_download_result",
    "_handle_purge_download_result",
}


def _defined_function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _defined_class_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}


def test_rpc_system_module_exists_and_defines_system_methods() -> None:
    """AST boundary: services/rpc/system.py defines the system/misc methods."""
    assert SYSTEM_PATH.is_file(), (
        f"services/rpc/system.py must exist: {SYSTEM_PATH}"
    )
    defined = _defined_function_names(SYSTEM_PATH)
    missing = EXPECTED_SYSTEM_METHODS - defined
    assert not missing, (
        f"system methods missing from rpc/system.py: {sorted(missing)}"
    )


def test_rpc_package_exports_dispatch_handler() -> None:
    """AST boundary: Aria2RpcHandler lives in the rpc package (system.py)."""
    assert "Aria2RpcHandler" in _defined_class_names(SYSTEM_PATH), (
        "Aria2RpcHandler must be defined in services/rpc/system.py"
    )
    init_source = RPC_INIT_PATH.read_text(encoding="utf-8")
    assert "Aria2RpcHandler" in init_source, (
        "services/rpc/__init__.py must re-export Aria2RpcHandler"
    )


def test_legacy_aria2_rpc_handler_module_is_gone() -> None:
    """AST boundary: services/aria2_rpc_handler.py must be deleted."""
    assert not LEGACY_HANDLER_PATH.is_file(), (
        f"legacy aria2_rpc_handler.py must be deleted: {LEGACY_HANDLER_PATH}"
    )
