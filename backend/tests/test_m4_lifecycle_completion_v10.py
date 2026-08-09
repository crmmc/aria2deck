"""M4 T08: services/lifecycle/completion.py AST boundary tests.

Verifies that the completion path functions live in
``app/services/lifecycle/completion.py``.  Also confirms the
``download_service.py`` module no longer exists.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
COMPLETION_PATH = (
    BACKEND_ROOT / "app" / "services" / "lifecycle" / "completion.py"
)
DOWNLOAD_SERVICE_PATH = (
    BACKEND_ROOT / "app" / "services" / "download_service.py"
)

EXPECTED_COMPLETION_SYMBOLS = {
    "handle_v0_download_complete",
    "resolve_complete_source_with_retry",
    "complete_global_download",
    "complete_global_download_locked",
    "_move_to_content_store",
    "_delete_download_source",
    "_restore_moved_source",
    "_compensate_incomplete_completion",
    "_compensate_completion_safely",
    "_scan_completed_source",
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



def test_lifecycle_completion_module_exists_and_defines_functions() -> None:
    """AST boundary: services/lifecycle/completion.py defines the completion path."""
    assert COMPLETION_PATH.is_file(), (
        f"services/lifecycle/completion.py must exist: {COMPLETION_PATH}"
    )
    defined = _defined_names(COMPLETION_PATH)
    missing = EXPECTED_COMPLETION_SYMBOLS - defined
    assert not missing, (
        f"completion functions missing from lifecycle/completion.py: {sorted(missing)}"
    )



def test_download_service_module_removed() -> None:
    """AST boundary: download_service.py must not exist after the contract."""
    assert not DOWNLOAD_SERVICE_PATH.is_file(), (
        f"services/download_service.py must be deleted: {DOWNLOAD_SERVICE_PATH}"
    )
