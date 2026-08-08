"""Task 7 — architecture boundary tests for the new task modules (v1).

Extends ``test_architecture_boundaries.py`` with rules specific to
``app.modules`` (task_core / backend / user_ref) and the retired lifecycle
auto-resume path:

- routers never call ``.force_remove(...)`` and never import
  ``app.aria2.client`` (Aria2Client implementation detail).
- ``app.modules.user_ref`` stays pure: no aria2 / db / repositories /
  services imports.
- ``app.modules.task_core`` never imports the aria2 transport layer
  (``app.aria2.*``); it talks to backends only via ``BackendPort``.
- ``.force_remove(...)`` is not called anywhere under ``app.modules`` —
  physical reclaim stays in the coordinator/cleanup modules
  (the adapter deliberately uses ``remove``/``remove_download_result``).
- the legacy size-known auto-unpause path is gone for good:
  ``_auto_resume_size_admitted_pause`` must not be defined or called.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
MODULES_ROOT = APP_ROOT / "modules"


def _line_number(node: ast.AST) -> int:
    lineno = getattr(node, "lineno", None)
    return lineno if isinstance(lineno, int) else 0


def _python_files(*relative_dirs: str) -> list[Path]:
    files: list[Path] = []
    for relative_dir in relative_dirs:
        files.extend((APP_ROOT / relative_dir).rglob("*.py"))
    return sorted(files)


def _imports_blocked_module_prefix(
    node: ast.AST, blocked_modules: set[str]
) -> str | None:
    def blocked(name: str) -> str | None:
        for module in blocked_modules:
            if name == module or name.startswith(f"{module}."):
                return module
        return None

    if isinstance(node, ast.Import):
        for alias in node.names:
            match = blocked(alias.name)
            if match is not None:
                return alias.name
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        match = blocked(module)
        if match is not None:
            return module
    return None


def _force_remove_call_lineno(node: ast.AST) -> int | None:
    """Return the lineno of a ``.force_remove(...)`` call, else None."""
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "force_remove":
            return _line_number(node)
        if isinstance(func, ast.Name) and func.id == "force_remove":
            return _line_number(node)
    return None


def test_new_module_skeleton_exists() -> None:
    """The v1 three-layer module tree must exist."""
    required = {
        MODULES_ROOT / "task_core" / "states.py",
        MODULES_ROOT / "task_core" / "register.py",
        MODULES_ROOT / "task_core" / "submit.py",
        MODULES_ROOT / "task_core" / "sync.py",
        MODULES_ROOT / "task_core" / "unref.py",
        MODULES_ROOT / "backend" / "port.py",
        MODULES_ROOT / "backend" / "aria2_adapter.py",
        MODULES_ROOT / "user_ref" / "projection.py",
    }
    missing = [
        str(path.relative_to(APP_ROOT.parent)) for path in required if not path.exists()
    ]
    assert missing == []


def test_routers_never_call_force_remove() -> None:
    """Routers are the HTTP surface; physical reclamation is never their job."""
    offenders: list[str] = []
    for path in _python_files("routers"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(APP_ROOT.parent)
        for node in ast.walk(tree):
            lineno = _force_remove_call_lineno(node)
            if lineno is not None:
                offenders.append(f"{relative}:{lineno} calls force_remove()")
    assert offenders == []


def test_routers_do_not_import_aria2_client_implementation() -> None:
    """Routers must not depend on the Aria2Client implementation detail.

    ``app.aria2.client`` is the concrete RPC client; routers go through
    services which in turn use the gateway/protocol or BackendPort.
    """
    blocked = {"app.aria2.client"}
    offenders: list[str] = []
    for path in _python_files("routers"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(APP_ROOT.parent)
        for node in ast.walk(tree):
            imported = _imports_blocked_module_prefix(node, blocked)
            if imported is not None:
                offenders.append(
                    f"{relative}:{_line_number(node)} imports {imported}"
                )
    assert offenders == []


def test_user_ref_stays_pure() -> None:
    """app.modules.user_ref is a pure projection layer.

    It maps (status, error_code) to user-facing labels and must not touch
    aria2, the database, repositories, or services.
    """
    blocked = {
        "app.aria2",
        "app.db",
        "app.repositories",
        "app.services",
        "app.routers",
        "sqlalchemy",
        "fastapi",
    }
    user_ref_root = MODULES_ROOT / "user_ref"
    if not user_ref_root.exists():
        raise AssertionError("app/modules/user_ref must exist")

    offenders: list[str] = []
    for path in sorted(user_ref_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(APP_ROOT.parent)
        for node in ast.walk(tree):
            imported = _imports_blocked_module_prefix(node, blocked)
            if imported is not None:
                offenders.append(
                    f"{relative}:{_line_number(node)} imports {imported}"
                )
    assert offenders == []


def test_task_core_does_not_import_aria2_transport() -> None:
    """Task Core talks to the backend only via ``BackendPort``.

    Importing ``app.aria2.*`` from ``app.modules.task_core`` would bypass
    the port and re-couple the core to the aria2 transport.
    """
    blocked = {"app.aria2"}
    task_core_root = MODULES_ROOT / "task_core"
    if not task_core_root.exists():
        raise AssertionError("app/modules/task_core must exist")

    offenders: list[str] = []
    for path in sorted(task_core_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(APP_ROOT.parent)
        for node in ast.walk(tree):
            imported = _imports_blocked_module_prefix(node, blocked)
            if imported is not None:
                offenders.append(
                    f"{relative}:{_line_number(node)} imports {imported}"
                )
    assert offenders == []


def test_modules_never_call_force_remove() -> None:
    """No ``.force_remove()`` under ``app.modules``.

    The BackendPort adapter uses ``remove`` / ``remove_download_result``;
    ``force_remove`` stays in the coordinator (aria2_lifecycle_service),
    the cleanup module (failed_task_cleanup), and the legacy cancel facade
    (download_service) per the allowed list in
    ``test_architecture_boundaries.test_force_remove_only_in_coordinator_and_cleanup``.
    """
    offenders: list[str] = []
    for path in sorted(MODULES_ROOT.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "force_remove" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        relative = path.relative_to(APP_ROOT.parent)
        for node in ast.walk(tree):
            lineno = _force_remove_call_lineno(node)
            if lineno is not None:
                offenders.append(f"{relative}:{lineno} calls force_remove()")
    assert offenders == []


def test_legacy_size_known_auto_unpause_removed() -> None:
    """The a554c30-era auto-unpause path must not come back.

    ``_auto_resume_size_admitted_pause`` (resume merely because
    ``size_known`` became true) was deleted; size admission owns its own
    pause/unpause lifecycle now. This test forbids re-introduction under
    any name containing ``auto_resume`` / ``auto_unpause``.
    """
    offenders: list[str] = []
    banned_fragments = ("auto_resume", "auto_unpause")
    explicit_names = {"_auto_resume_size_admitted_pause"}

    for path in sorted(APP_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not any(fragment in source for fragment in banned_fragments):
            continue
        tree = ast.parse(source, filename=str(path))
        relative = path.relative_to(APP_ROOT.parent)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(fragment in node.name for fragment in banned_fragments):
                    offenders.append(
                        f"{relative}:{_line_number(node)} defines {node.name}"
                    )
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else None
                if name in explicit_names or (
                    name is not None
                    and any(fragment in name for fragment in banned_fragments)
                ):
                    offenders.append(
                        f"{relative}:{_line_number(node)} calls {name}()"
                    )
    assert offenders == []
