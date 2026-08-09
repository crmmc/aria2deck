"""M4 T05: services/lifecycle/_shared.py AST boundary tests.

Verifies that the shared private helpers used by multiple lifecycle
modules live in ``app/services/lifecycle/_shared.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SHARED_PATH = (
    BACKEND_ROOT / "app" / "services" / "lifecycle" / "_shared.py"
)

EXPECTED_SHARED_SYMBOLS = {
    "_map_v0_status",
    "_requery_after_control_failure",
    "is_transient_rpc_error",
    "is_missing_gid_error",
    "_exception_message",
    "_broadcast_download_update",
    "get_representative_owner_id",
    "_sanitize_path",
    "PAUSE_SUCCESS_STATUSES",
    "UNPAUSE_SUCCESS_STATUSES",
}


def _defined_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names



def test_lifecycle_shared_module_exists_and_defines_helpers() -> None:
    """AST boundary: services/lifecycle/_shared.py defines the shared helpers."""
    assert SHARED_PATH.is_file(), (
        f"services/lifecycle/_shared.py must exist: {SHARED_PATH}"
    )
    defined = _defined_names(SHARED_PATH)
    missing = EXPECTED_SHARED_SYMBOLS - defined
    assert not missing, (
        f"shared helpers missing from lifecycle/_shared.py: {sorted(missing)}"
    )

