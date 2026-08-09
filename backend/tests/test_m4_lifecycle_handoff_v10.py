"""M4 T07: services/lifecycle/handoff.py AST boundary tests.

Verifies that the handoff functions live in
``app/services/lifecycle/handoff.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
HANDOFF_PATH = (
    BACKEND_ROOT / "app" / "services" / "lifecycle" / "handoff.py"
)

EXPECTED_HANDOFF_SYMBOLS = {
    "_handoff_locked",
    "switch_to_followed_download",
    "resolve_download_for_gid",
    "_find_followed_gid_by_following",
    "_refresh_followed_gid",
    "switch_to_late_followed_download_if_supported",
    "defer_metadata_completion_if_handoff_pending",
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



def test_lifecycle_handoff_module_exists_and_defines_functions() -> None:
    """AST boundary: services/lifecycle/handoff.py defines the handoff functions."""
    assert HANDOFF_PATH.is_file(), (
        f"services/lifecycle/handoff.py must exist: {HANDOFF_PATH}"
    )
    defined = _defined_names(HANDOFF_PATH)
    missing = EXPECTED_HANDOFF_SYMBOLS - defined
    assert not missing, (
        f"handoff functions missing from lifecycle/handoff.py: {sorted(missing)}"
    )

