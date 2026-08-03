from __future__ import annotations

import ast
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def _line_number(node: ast.AST) -> int:
    lineno = getattr(node, "lineno", None)
    return lineno if isinstance(lineno, int) else 0

def _python_files(*relative_dirs: str) -> list[Path]:
    files: list[Path] = []
    for relative_dir in relative_dirs:
        files.extend((APP_ROOT / relative_dir).rglob("*.py"))
    return sorted(files)


def _imports_router(node: ast.AST) -> str | None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "app.routers" or alias.name.startswith("app.routers."):
                return alias.name
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if module == "app.routers" or module.startswith("app.routers."):
            return module
    return None


def _imports_module(node: ast.AST, blocked_modules: set[str]) -> str | None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name in blocked_modules:
                return alias.name
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if module in blocked_modules:
            return module
    return None


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


def test_non_router_layers_do_not_import_router_modules() -> None:
    offenders: list[str] = []
    for path in _python_files("aria2", "services", "core"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = _imports_router(node)
            if imported is not None:
                relative = path.relative_to(APP_ROOT.parent)
                offenders.append(f"{relative}:{_line_number(node)} imports {imported}")

    assert offenders == []


def test_ws_router_does_not_import_connection_helpers_from_aria2_sync() -> None:
    path = APP_ROOT / "routers" / "ws.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module != "app.aria2.sync":
            continue
        imported_names = {alias.name for alias in node.names}
        blocked = imported_names & {"register_ws", "unregister_ws"}
        if blocked:
            relative = path.relative_to(APP_ROOT.parent)
            offenders.append(f"{relative}:{_line_number(node)} imports {sorted(blocked)}")

    assert offenders == []


def test_domain_modules_do_not_import_outer_layers_or_frameworks() -> None:
    domain_root = APP_ROOT / "domain"
    if not domain_root.exists():
        raise AssertionError("backend/app/domain must exist for P2 domain extraction")

    blocked_modules = {
        "app.routers",
        "app.services",
        "app.repositories",
        "app.db",
        "app.aria2",
        "app.core",
        "fastapi",
        "sqlalchemy",
        "pydantic",
    }
    offenders: list[str] = []

    for path in sorted(domain_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = _imports_blocked_module_prefix(node, blocked_modules)
            if imported is not None:
                relative = path.relative_to(APP_ROOT.parent)
                offenders.append(f"{relative}:{_line_number(node)} imports {imported}")

    assert offenders == []


def test_routers_do_not_import_direct_db_or_sqlalchemy_modules() -> None:
    offenders: list[str] = []
    blocked_modules = {
        "app.db",
        "app.db.engine",
        "app.db.schema",
        "sqlalchemy",
        "app.repositories",
        "app.aria2.client",
    }

    for path in _python_files("routers"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = _imports_blocked_module_prefix(node, blocked_modules)
            if imported is not None:
                relative = path.relative_to(APP_ROOT.parent)
                offenders.append(f"{relative}:{_line_number(node)} imports {imported}")

    assert offenders == []


def test_routers_do_not_construct_sqlalchemy_queries_or_transactions() -> None:
    offenders: list[str] = []
    blocked_names = {"select", "insert", "update", "delete", "transaction"}

    for path in _python_files("routers"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else None
                if name in blocked_names:
                    relative = path.relative_to(APP_ROOT.parent)
                    offenders.append(f"{relative}:{_line_number(node)} calls {name}()")

    assert offenders == []


def test_routers_do_not_import_other_router_modules() -> None:
    offenders: list[str] = []
    for path in _python_files("routers"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = _imports_router(node)
            if imported is not None:
                relative = path.relative_to(APP_ROOT.parent)
                offenders.append(f"{relative}:{_line_number(node)} imports {imported}")

    assert offenders == []


def test_aria2_modules_do_not_import_db_repositories_sqlalchemy_or_routers() -> None:
    offenders: list[str] = []
    blocked_modules = {
        "app.db",
        "app.db.engine",
        "app.db.schema",
        "app.repositories",
        "app.routers",
        "sqlalchemy",
    }

    for path in _python_files("aria2"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = _imports_blocked_module_prefix(node, blocked_modules)
            if imported is not None:
                relative = path.relative_to(APP_ROOT.parent)
                offenders.append(f"{relative}:{_line_number(node)} imports {imported}")

    assert offenders == []


def test_aria2_modules_do_not_construct_sqlalchemy_queries_or_transactions() -> None:
    offenders: list[str] = []
    blocked_names = {"select", "insert", "update", "delete", "transaction"}

    for path in _python_files("aria2"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else None
                if name in blocked_names:
                    relative = path.relative_to(APP_ROOT.parent)
                    offenders.append(f"{relative}:{_line_number(node)} calls {name}()")

    assert offenders == []


def test_core_modules_do_not_import_outer_layers_or_persistence() -> None:
    offenders: list[str] = []
    blocked_modules = {
        "app.routers",
        "app.services",
        "app.repositories",
        "app.aria2",
        "app.db",
        "sqlalchemy",
    }

    for path in _python_files("core"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = _imports_blocked_module_prefix(node, blocked_modules)
            if imported is not None:
                relative = path.relative_to(APP_ROOT.parent)
                offenders.append(f"{relative}:{_line_number(node)} imports {imported}")

    assert offenders == []


def test_services_do_not_import_direct_db_or_sqlalchemy_modules() -> None:
    offenders: list[str] = []
    blocked_modules = {
        "app.db",
        "app.db.engine",
        "app.db.schema",
        "sqlalchemy",
        "sqlite3",
    }

    for path in _python_files("services"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = _imports_blocked_module_prefix(node, blocked_modules)
            if imported is not None:
                relative = path.relative_to(APP_ROOT.parent)
                offenders.append(f"{relative}:{_line_number(node)} imports {imported}")

    assert offenders == []


def test_services_do_not_import_core_state() -> None:
    offenders: list[str] = []
    blocked_modules = {"app.core.state"}

    for path in _python_files("services"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = _imports_blocked_module_prefix(node, blocked_modules)
            if imported is not None:
                relative = path.relative_to(APP_ROOT.parent)
                offenders.append(f"{relative}:{_line_number(node)} imports {imported}")

    assert offenders == []


def test_services_do_not_import_http_rate_limit_guard() -> None:
    offenders: list[str] = []
    blocked_modules = {"app.core.request_rate_guard", "fastapi"}

    for path in _python_files("services"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = _imports_blocked_module_prefix(node, blocked_modules)
            if imported is not None:
                relative = path.relative_to(APP_ROOT.parent)
                offenders.append(f"{relative}:{_line_number(node)} imports {imported}")

    assert offenders == []


def test_services_only_import_aria2_gateway_or_protocol() -> None:
    offenders: list[str] = []
    allowed_modules = {"app.aria2.gateway", "app.aria2.protocol"}

    for path in _python_files("services"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = _imports_blocked_module_prefix(node, {"app.aria2"})
            if imported is None:
                continue
            if imported in allowed_modules:
                continue
            relative = path.relative_to(APP_ROOT.parent)
            offenders.append(f"{relative}:{_line_number(node)} imports {imported}")

    assert offenders == []


def test_services_do_not_construct_sqlalchemy_queries_or_transactions() -> None:
    offenders: list[str] = []
    blocked_names = {"select", "insert", "delete", "transaction"}

    for path in _python_files("services"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else None
                if name in blocked_names:
                    relative = path.relative_to(APP_ROOT.parent)
                    offenders.append(f"{relative}:{_line_number(node)} calls {name}()")

    assert offenders == []


def test_routers_do_not_import_task_broadcast_except_ws_registration() -> None:
    offenders: list[str] = []
    allowed_ws_names = {"register_ws", "unregister_ws"}

    for path in _python_files("routers"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(APP_ROOT.parent)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.services.task_broadcast":
                        offenders.append(f"{relative}:{_line_number(node)} imports {alias.name}")
                continue

            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if module == "app.services.task_broadcast":
                names = {alias.name for alias in node.names}
                if path.name == "ws.py" and names <= allowed_ws_names:
                    continue
                offenders.append(
                    f"{relative}:{_line_number(node)} imports {module}.{sorted(names)}"
                )
            elif module == "app.services":
                names = {alias.name for alias in node.names}
                if "task_broadcast" in names:
                    offenders.append(f"{relative}:{_line_number(node)} imports task_broadcast")

    assert offenders == []


def test_repositories_do_not_import_higher_layers_or_aria2() -> None:
    offenders: list[str] = []
    blocked_modules = {
        "app.routers",
        "app.services",
        "app.aria2",
        "app.core",
    }

    for path in _python_files("repositories"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = _imports_blocked_module_prefix(node, blocked_modules)
            if imported is not None:
                relative = path.relative_to(APP_ROOT.parent)
                offenders.append(f"{relative}:{_line_number(node)} imports {imported}")

    assert offenders == []


def test_repositories_do_not_import_raw_sqlite_driver() -> None:
    offenders: list[str] = []
    blocked_modules = {"sqlite3"}

    for path in _python_files("repositories"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = _imports_blocked_module_prefix(node, blocked_modules)
            if imported is not None:
                relative = path.relative_to(APP_ROOT.parent)
                offenders.append(f"{relative}:{_line_number(node)} imports {imported}")

    assert offenders == []


def test_aria2_sync_and_listener_do_not_define_forwarding_wrappers() -> None:
    blocked_names = {
        "_sanitize_path",
        "_status_bool",
        "_exception_message",
        "_is_missing_gid_error",
        "_is_transient_rpc_error",
        "_list_v0_tracked_downloads",
        "_fail_v0_download_and_cleanup",
        "_complete_v0_download_from_sync",
        "_update_v0_download_from_aria2",
        "_repair_inconsistent_completed_downloads_v0",
        "_cleanup_stale_queued_downloads_v0",
        "_list_task_dir_entries",
        "_resolve_complete_source_with_retry",
        "handle_v0_download_complete",
    }
    offenders: list[str] = []

    for path in (APP_ROOT / "aria2" / "sync.py", APP_ROOT / "aria2" / "listener.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(APP_ROOT.parent)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in blocked_names:
                    offenders.append(f"{relative}:{_line_number(node)} defines {node.name}")

    assert offenders == []


def test_aria2_protocol_is_the_only_application_protocol_class() -> None:
    offenders: list[str] = []
    allowed = APP_ROOT / "aria2" / "protocol.py"

    for path in _python_files("aria2", "services"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                if isinstance(base, ast.Name) and base.id == "Protocol":
                    if path != allowed:
                        relative = path.relative_to(APP_ROOT.parent)
                        offenders.append(f"{relative}:{_line_number(node)} defines {node.name}")
                if isinstance(base, ast.Attribute) and base.attr == "Protocol":
                    if path != allowed:
                        relative = path.relative_to(APP_ROOT.parent)
                        offenders.append(f"{relative}:{_line_number(node)} defines {node.name}")

    assert offenders == []


def test_required_domain_modules_exist() -> None:
    required = {
        APP_ROOT / "domain" / "status.py",
        APP_ROOT / "domain" / "quota.py",
        APP_ROOT / "domain" / "task_policy.py",
        APP_ROOT / "domain" / "errors.py",
        APP_ROOT / "domain" / "torrent_metadata.py",
    }
    missing = [str(path.relative_to(APP_ROOT.parent)) for path in required if not path.exists()]

    assert missing == []


def test_removed_layering_compatibility_modules_do_not_exist_or_get_imported() -> None:
    removed_paths = {
        APP_ROOT / "core" / "state.py",
        APP_ROOT / "domain" / "downloads.py",
        APP_ROOT / "services" / "torrent_metadata.py",
    }
    existing = [
        str(path.relative_to(APP_ROOT.parent)) for path in removed_paths if path.exists()
    ]

    blocked_modules = {
        "app.core.state",
        "app.domain.downloads",
        "app.services.torrent_metadata",
    }
    offenders: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = _imports_blocked_module_prefix(node, blocked_modules)
            if imported is not None:
                relative = path.relative_to(APP_ROOT.parent)
                offenders.append(f"{relative}:{_line_number(node)} imports {imported}")

    assert existing == []
    assert offenders == []
