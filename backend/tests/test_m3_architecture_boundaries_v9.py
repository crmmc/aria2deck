"""M3 T18: cross-module architecture boundary gates (spec §22.6).

AST-level guards that keep the projection-based architecture intact:

1. ``routers/aria2_rpc.py`` never imports ``get_aria2_client`` / ``Aria2Gateway``
   (the router talks only to ``Aria2RpcHandler``).
2. ``services/aria2_rpc_handler.py`` has no ``self.client`` attribute and no
   direct ``client.tell_status/tell_active/get_files/get_uris/get_peers/
   get_servers`` calls (all reads go through the projection layer).
3. ``services/stats_service.py`` never imports ``get_aria2_client`` and never
   calls ``fetch_active_live_statuses_by_gid``.
4. ``services/deletion_cleanup.py`` never imports ``cancel_user_task`` /
   ``get_aria2_client``.
5. ``services/download_service.py`` does not export the removed legacy
   ``create_user_download`` / ``create_user_torrent_download`` /
   ``cancel_user_task`` entry points.
6. ``services/task_runtime.py`` is gone (or, if resurrected, must not export
   ``fetch_cached_live_status_for_row`` / ``fetch_active_live_statuses_by_gid``).
7. ``app/modules/task_core`` and ``app/modules/user_ref`` never import
   ``app.aria2.*`` (BackendPort type annotations excepted).
8. ``services/task_service.py`` ``list_tasks`` / ``list_tasks_page`` never call
   ``fetch_active_live_statuses_by_gid`` (list reads are projection-only).
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
MODULES_ROOT = APP_ROOT / "modules"


def _tree(relative: str) -> ast.Module:
    return ast.parse(
        (APP_ROOT / relative).read_text(encoding="utf-8"), filename=relative
    )


def _imported_names(tree: ast.Module) -> set[str]:
    """All names brought into module scope by import statements."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                names.add(f"{module}.{alias.name}")
                names.add(alias.name)
    return names


def _imported_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    return modules


def _called_names(tree: ast.Module) -> set[str]:
    """Function names invoked anywhere in the module (Name or Attribute)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _defined_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def test_rpc_router_does_not_import_client_or_gateway() -> None:
    """Rule 1: routers/aria2_rpc.py stays on the handler, not the client."""
    tree = _tree("routers/aria2_rpc.py")
    imported = _imported_names(tree) | _imported_modules(tree)
    forbidden = {"get_aria2_client", "Aria2Gateway"}
    assert forbidden.isdisjoint(imported), (
        f"aria2_rpc.py imports forbidden symbols: {forbidden & imported}"
    )
    modules = _imported_modules(tree)
    assert "app.aria2.gateway" not in modules
    assert "app.aria2.client" not in modules


def test_rpc_handler_has_no_direct_client_calls() -> None:
    """Rule 2: aria2_rpc_handler.py must not hold or call an aria2 client."""
    tree = _tree("services/aria2_rpc_handler.py")
    source = (APP_ROOT / "services/aria2_rpc_handler.py").read_text(encoding="utf-8")
    assert "self.client" not in source, "aria2_rpc_handler still uses self.client"

    imported = _imported_names(tree)
    assert "get_aria2_client" not in imported
    assert "Aria2Gateway" not in imported

    offenders: list[str] = []
    client_methods = {
        "tell_status",
        "tell_active",
        "get_files",
        "get_uris",
        "get_peers",
        "get_servers",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in client_methods
            and isinstance(func.value, ast.Name)
            and func.value.id == "client"
        ):
            offenders.append(f"client.{func.attr}() at line {node.lineno}")
    assert offenders == []


def test_stats_service_uses_projection_only() -> None:
    """Rule 3: stats_service.py never touches the live client/cache path."""
    tree = _tree("services/stats_service.py")
    imported = _imported_names(tree) | _imported_modules(tree)
    assert "get_aria2_client" not in imported
    assert "app.aria2.gateway" not in imported
    assert "app.aria2.client" not in imported
    called = _called_names(tree)
    assert "fetch_active_live_statuses_by_gid" not in called
    assert "fetch_active_live_statuses_by_gid" not in imported


def test_deletion_cleanup_uses_cancel_task_facade_only() -> None:
    """Rule 4: deletion_cleanup.py must not import legacy cancel/client."""
    tree = _tree("services/deletion_cleanup.py")
    imported = _imported_names(tree) | _imported_modules(tree)
    forbidden = {"cancel_user_task", "get_aria2_client"}
    assert forbidden.isdisjoint(imported), (
        f"deletion_cleanup.py imports forbidden symbols: {forbidden & imported}"
    )
    assert "app.aria2.gateway" not in imported
    assert "app.aria2.client" not in imported


def test_download_service_legacy_symbols_removed() -> None:
    """Rule 5: download_service.py no longer exports legacy create/cancel."""
    tree = _tree("services/download_service.py")
    defined = _defined_names(tree)
    forbidden = {
        "create_user_download",
        "create_user_torrent_download",
        "cancel_user_task",
    }
    assert forbidden.isdisjoint(defined), (
        f"download_service.py still defines: {forbidden & defined}"
    )


def test_task_runtime_cache_is_gone() -> None:
    """Rule 6: task_runtime.py must not exist, or must not export live-cache helpers."""
    path = APP_ROOT / "services" / "task_runtime.py"
    if not path.exists():
        return
    tree = _tree("services/task_runtime.py")
    defined = _defined_names(tree)
    forbidden = {
        "fetch_cached_live_status_for_row",
        "fetch_active_live_statuses_by_gid",
    }
    assert forbidden.isdisjoint(defined), (
        f"task_runtime.py still exports live-cache helpers: {forbidden & defined}"
    )


def test_modules_do_not_import_aria2_transport() -> None:
    """Rule 7: task_core / user_ref stay decoupled from app.aria2.*.

    ``BackendPort`` type annotations are the only allowed coupling and they
    live in ``app.modules.backend.port``, not in ``app.aria2``.
    """
    offenders: list[str] = []
    for subdir in ("task_core", "user_ref"):
        root = MODULES_ROOT / subdir
        if not root.exists():
            raise AssertionError(f"app/modules/{subdir} must exist")
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relative = path.relative_to(APP_ROOT.parent)
            for node in ast.walk(tree):
                module: str | None = None
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "app.aria2" or alias.name.startswith(
                            "app.aria2."
                        ):
                            module = alias.name
                elif isinstance(node, ast.ImportFrom):
                    candidate = node.module or ""
                    if candidate == "app.aria2" or candidate.startswith("app.aria2."):
                        module = candidate
                if module is not None:
                    offenders.append(
                        f"{relative}:{getattr(node, 'lineno', 0)} imports {module}"
                    )
    assert offenders == []


def test_task_service_list_paths_are_projection_only() -> None:
    """Rule 8: list_tasks / list_tasks_page never call the live-status cache."""
    tree = _tree("services/task_service.py")
    called = _called_names(tree)
    imported = _imported_names(tree)
    assert "fetch_active_live_statuses_by_gid" not in called
    assert "fetch_active_live_statuses_by_gid" not in imported
