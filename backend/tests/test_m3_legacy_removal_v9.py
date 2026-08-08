"""M3 T16: legacy create/cancel service façade removal boundary tests.

Verifies that ``download_service.py`` no longer exports the removed
v0 creation and cancellation entry points.
"""

from __future__ import annotations

import ast
from pathlib import Path


def test_download_service_no_longer_exports_legacy_create_cancel() -> None:
    """AST boundary: download_service.py must not define the removed symbols."""
    source = (Path(__file__).resolve().parents[1] / "app" / "services" / "download_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    defined_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined_names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            defined_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined_names.add(target.id)

    forbidden = {
        "create_user_download",
        "create_user_torrent_download",
        "cancel_user_task",
        "_create_user_download_with_submit",
        "_resolve_existing_download",
        "_create_attempt_locked",
        "_submit_attempt_locked",
        "_ensure_download_submitted",
        "_admit_paused_unknown_download",
        "_validate_submit_options",
        "_cleanup_submitted_failure",
        "_cleanup_submitted_failure_safely",
        "DuplicateTaskError",
    }
    assert forbidden.isdisjoint(defined_names), (
        f"legacy symbols still defined: {forbidden & defined_names}"
    )


def test_download_service_retains_completion_path() -> None:
    """AST guard: completion path functions must still exist."""
    source = (Path(__file__).resolve().parents[1] / "app" / "services" / "download_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    defined_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined_names.add(node.name)

    required = {
        "complete_global_download",
        "complete_global_download_locked",
        "get_download_lifecycle_lock",
        "get_disk_available_bytes",
        "candidate_size_from_status",
    }
    assert required.issubset(defined_names), (
        f"missing required symbols: {required - defined_names}"
    )
