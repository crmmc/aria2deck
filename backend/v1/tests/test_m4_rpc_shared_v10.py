"""M4 T13: services/rpc/_shared.py AST boundary tests.

Verifies that the shared private helpers used by multiple RPC handler
methods live in ``app/services/rpc/_shared.py``, and that the legacy
``services/aria2_rpc_handler.py`` no longer defines them at module level.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SHARED_PATH = BACKEND_ROOT / "app" / "services" / "rpc" / "_shared.py"
LEGACY_HANDLER_PATH = BACKEND_ROOT / "app" / "services" / "rpc" / "system.py"

EXPECTED_SHARED_SYMBOLS = {
    "_resolve_owned_row",
    "_check_quota_and_disk",
    "_raise_create_download_error",
    "_gid_for_created_task",
    "_get_user_quota",
    "_validate_uri_list",
    "_validate_submit_options",
    "_with_rpc_mirrors",
    "_resource_kind_for_uri",
    "_resource_key_for_uri",
    "_extract_name_from_uri",
    "_selected_torrent_indexes",
    "_extract_status_keys",
    "_normalize_pagination",
    "_slice_with_offset",
    "_apply_status_keys_to_list",
}


def _defined_names(path: Path, *, module_level_only: bool = False) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    scope = tree.body if module_level_only else ast.walk(tree)
    for node in scope:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def test_rpc_shared_module_exists_and_defines_helpers() -> None:
    """AST boundary: services/rpc/_shared.py defines the shared helpers."""
    assert SHARED_PATH.is_file(), (
        f"services/rpc/_shared.py must exist: {SHARED_PATH}"
    )
    defined = _defined_names(SHARED_PATH)
    missing = EXPECTED_SHARED_SYMBOLS - defined
    assert not missing, (
        f"shared helpers missing from rpc/_shared.py: {sorted(missing)}"
    )


def test_legacy_handler_no_longer_defines_shared_helpers() -> None:
    """AST boundary: aria2_rpc_handler.py delegates to rpc/_shared.py.

    Thin ``self.*`` compatibility wrappers on ``Aria2RpcHandler`` are
    allowed (existing tests call them), but no module-level definition
    of the shared symbols may remain in the legacy file.
    """
    if not LEGACY_HANDLER_PATH.is_file():
        return
    defined = _defined_names(LEGACY_HANDLER_PATH, module_level_only=True)
    leaked = EXPECTED_SHARED_SYMBOLS & defined
    assert not leaked, (
        f"aria2_rpc_handler.py still defines shared helpers locally: "
        f"{sorted(leaked)}"
    )
