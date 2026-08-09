"""M4 T09: lifecycle coordinator/repair AST boundary tests.

Verifies that the coordinator and repair functions live in
``app/services/lifecycle/coordinator.py`` and
``app/services/lifecycle/repair.py``, and that the former
``aria2_lifecycle_service.py`` module has been deleted.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
COORDINATOR_PATH = (
    BACKEND_ROOT / "app" / "services" / "lifecycle" / "coordinator.py"
)
REPAIR_PATH = (
    BACKEND_ROOT / "app" / "services" / "lifecycle" / "repair.py"
)
LEGACY_PATH = (
    BACKEND_ROOT / "app" / "services" / "aria2_lifecycle_service.py"
)

EXPECTED_COORDINATOR_SYMBOLS = {
    "reconcile_attempt_signal",
    "V0_SYNC_TRACKED_STATUSES",
}

EXPECTED_REPAIR_SYMBOLS = {
    "repair_inconsistent_completed_downloads_v0",
    "cleanup_stale_queued_downloads_v0",
    "reconcile_legacy_http_downloads_v0",
    "_stop_legacy_http_job",
    "list_v0_tracked_downloads",
    "COMPLETE_REPAIR_GRACE_SECONDS",
    "STALE_QUEUED_GRACE_SECONDS",
    "LEGACY_HTTP_STOP_ERROR",
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


def test_lifecycle_coordinator_module_exists_and_defines_functions() -> None:
    """AST boundary: services/lifecycle/coordinator.py defines the coordinator."""
    assert COORDINATOR_PATH.is_file(), (
        f"services/lifecycle/coordinator.py must exist: {COORDINATOR_PATH}"
    )
    defined = _defined_names(COORDINATOR_PATH)
    missing = EXPECTED_COORDINATOR_SYMBOLS - defined
    assert not missing, (
        "coordinator symbols missing from lifecycle/coordinator.py: "
        f"{sorted(missing)}"
    )


def test_lifecycle_repair_module_exists_and_defines_functions() -> None:
    """AST boundary: services/lifecycle/repair.py defines the repair helpers."""
    assert REPAIR_PATH.is_file(), (
        f"services/lifecycle/repair.py must exist: {REPAIR_PATH}"
    )
    defined = _defined_names(REPAIR_PATH)
    missing = EXPECTED_REPAIR_SYMBOLS - defined
    assert not missing, (
        "repair symbols missing from lifecycle/repair.py: "
        f"{sorted(missing)}"
    )


def test_legacy_lifecycle_service_module_removed() -> None:
    """AST boundary: aria2_lifecycle_service.py must not exist after T09."""
    assert not LEGACY_PATH.is_file(), (
        f"services/aria2_lifecycle_service.py must be deleted: {LEGACY_PATH}"
    )


def test_no_references_to_legacy_lifecycle_service() -> None:
    """AST boundary: no source file imports aria2_lifecycle_service."""
    offenders: list[str] = []
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        if path.name == Path(__file__).name:
            continue
        source = path.read_text(encoding="utf-8")
        if (
            "app.services.aria2_lifecycle_service" in source
            or "services import aria2_lifecycle_service" in source
        ):
            offenders.append(str(path.relative_to(BACKEND_ROOT)))
    assert not offenders, (
        f"aria2_lifecycle_service still referenced: {offenders}"
    )
