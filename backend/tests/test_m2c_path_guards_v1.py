"""Task C4 — path guards: architecture boundary locks for M2-C.

These tests lock down the structural contracts introduced by C1-C3:

- ``task_service.py`` must not call ``create_user_download`` or
  ``create_user_torrent_download`` (legacy v0 create functions removed by
  the Task Core migration).
- ``task_service.create_task`` / ``create_torrent_task`` must route through
  ``register_and_submit`` (the Task Core admission + submit entry point).
- ``app/aria2/sync.py`` must reference the queue policy entry point
  (``apply_queue_policy``) so that system-queued tasks resume on resource
  recovery.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def _line_number(node: ast.AST) -> int:
    lineno = getattr(node, "lineno", None)
    return lineno if isinstance(lineno, int) else 0


# --------------------------------------------------------------------------- #
# task_service.py must not reference legacy create functions                 #
# --------------------------------------------------------------------------- #


def test_task_service_never_calls_legacy_create() -> None:
    """task_service.py must not reference create_user_download / create_user_torrent_download."""
    source = (APP_ROOT / "services" / "task_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(APP_ROOT / "services" / "task_service.py"))
    forbidden = {"create_user_download", "create_user_torrent_download"}
    offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden:
            offenders.append(
                f"task_service.py:{_line_number(node)} references {node.id}"
            )
        elif isinstance(node, ast.Attribute) and node.attr in forbidden:
            offenders.append(
                f"task_service.py:{_line_number(node)} references .{node.attr}"
            )

    assert not offenders, offenders


# --------------------------------------------------------------------------- #
# task_service create paths must use register_and_submit                       #
# --------------------------------------------------------------------------- #


def test_create_task_uses_register_and_submit() -> None:
    """create_task must end by calling register_and_submit (Task Core path)."""
    source = (APP_ROOT / "services" / "task_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(APP_ROOT / "services" / "task_service.py"))

    create_task_found = False
    calls_register = False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "create_task":
                create_task_found = True
                for child in ast.walk(node):
                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Name)
                        and child.func.id == "register_and_submit"
                    ):
                        calls_register = True

    assert create_task_found, "create_task function not found in task_service.py"
    assert calls_register, "create_task does not call register_and_submit"


def test_create_torrent_task_uses_register_and_submit() -> None:
    """create_torrent_task must end by calling register_and_submit (Task Core path)."""
    source = (APP_ROOT / "services" / "task_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(APP_ROOT / "services" / "task_service.py"))

    func_found = False
    calls_register = False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "create_torrent_task":
                func_found = True
                for child in ast.walk(node):
                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Name)
                        and child.func.id == "register_and_submit"
                    ):
                        calls_register = True

    assert func_found, "create_torrent_task function not found in task_service.py"
    assert calls_register, "create_torrent_task does not call register_and_submit"


# --------------------------------------------------------------------------- #
# aria2/sync.py must reference the queue policy entry point                   #
# --------------------------------------------------------------------------- #


def test_sync_references_queue_policy() -> None:
    """aria2/sync.py must import and call apply_queue_policy from task_core.sync.

    This ensures the production sync loop resumes system-queued tasks
    (quota_queued / disk_queued) when resources recover.
    """
    sync_path = APP_ROOT / "aria2" / "sync.py"
    source = sync_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(sync_path))

    imports_apply_queue_policy = False
    calls_apply_queue_policy = False

    for node in ast.walk(tree):
        # Check for import of apply_queue_policy
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "task_core.sync" in module:
                for alias in node.names:
                    if alias.name == "apply_queue_policy":
                        imports_apply_queue_policy = True

        # Check for a call to apply_queue_policy
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "apply_queue_policy":
                calls_apply_queue_policy = True

    assert imports_apply_queue_policy, (
        "aria2/sync.py does not import apply_queue_policy from task_core.sync"
    )
    assert calls_apply_queue_policy, (
        "aria2/sync.py does not call apply_queue_policy()"
    )
