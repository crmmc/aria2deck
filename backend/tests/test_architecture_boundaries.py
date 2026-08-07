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


def test_runtime_services_mark_failed_only_via_fail_download_and_reclaim() -> None:
    """Direct mark_global_download_failed is allowed only inside fail reclaim entry."""
    allowed_files = {
        APP_ROOT / "services" / "aria2_lifecycle_service.py",
        APP_ROOT / "repositories" / "downloads.py",
    }
    allowed_functions = {
        "_fail_download_and_reclaim_operation",
        "_mark_and_cleanup",
        "mark_global_download_failed",
    }
    offenders: list[str] = []

    for path in sorted(APP_ROOT.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "mark_global_download_failed" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "mark_global_download_failed"
                ):
                    if path in allowed_files and node.name in allowed_functions:
                        continue
                    relative = path.relative_to(APP_ROOT.parent)
                    offenders.append(
                        f"{relative}:{_line_number(child)} in {node.name}"
                    )

    assert offenders == []


def test_aria2_listener_sync_do_not_call_lifecycle_terminal_or_cleanup() -> None:
    """listener/sync must not directly call terminalization or cleanup helpers (spec §17.3–§17.4).

    These modules are transport/observation layers. All lifecycle decisions
    go through ``reconcile_attempt_signal`` in the coordinator.
    """
    blocked_calls = {
        "mark_global_download_failed",
        "force_remove",
        "cleanup_task_download_dir",
        "cleanup_failed_task_artifacts",
        "cleanup_terminal_download_generation",
        "cleanup_failed_task_artifacts_unchecked",
        "cleanup_with_claim",
        "fail_download_and_reclaim",
        "claim_attempt_terminal",
        "claim_terminal_reclaim",
    }
    offenders: list[str] = []

    for path in (APP_ROOT / "aria2" / "listener.py", APP_ROOT / "aria2" / "sync.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(APP_ROOT.parent)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else None
                if name in blocked_calls:
                    offenders.append(
                        f"{relative}:{_line_number(node)} calls {name}()"
                    )

    assert offenders == []


def test_files_repository_does_not_write_live_lifecycle() -> None:
    """repositories/files.py must not call lifecycle terminalization or cleanup (spec §16, §17.7).

    Files, shares, and pack modules influence downloads only through
    controlled bridges (completed_file_id, stored file deletion), never
    by directly terminalizing or cleaning up live attempts.
    """
    blocked_calls = {
        "mark_global_download_failed",
        "claim_attempt_terminal",
        "claim_terminal_reclaim",
        "fail_download_and_reclaim",
        "cleanup_with_claim",
        "cleanup_task_download_dir",
        "cleanup_failed_task_artifacts",
        "cleanup_terminal_download_generation",
        "cancel_user_task_and_maybe_claim_attempt",
    }
    offenders: list[str] = []
    files_repo = APP_ROOT / "repositories" / "files.py"
    if not files_repo.exists():
        return
    tree = ast.parse(files_repo.read_text(encoding="utf-8"), filename=str(files_repo))
    relative = files_repo.relative_to(APP_ROOT.parent)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else None
            if name in blocked_calls:
                offenders.append(f"{relative}:{_line_number(node)} calls {name}()")

    assert offenders == []


def test_deprecated_cleanup_apis_removed_from_production() -> None:
    """Deprecated cleanup bypass functions must not exist in production code (spec §17.5, §23).

    ``cleanup_failed_task_artifacts``, ``cleanup_terminal_download_generation``,
    and ``cleanup_failed_task_artifacts_unchecked`` were unauthorized legacy
    paths that accepted arbitrary task_id + gid without a claim. They have
    been deleted; this test prevents re-introduction.
    """
    removed_names = {
        "cleanup_failed_task_artifacts",
        "cleanup_terminal_download_generation",
        "cleanup_failed_task_artifacts_unchecked",
    }
    offenders: list[str] = []

    for path in sorted(APP_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        relative = path.relative_to(APP_ROOT.parent)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in removed_names:
                    offenders.append(
                        f"{relative}:{_line_number(node)} defines {node.name}"
                    )
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else None
                if name in removed_names:
                    offenders.append(
                        f"{relative}:{_line_number(node)} calls {name}()"
                    )

    assert offenders == []


def test_no_skip_status_check_in_production_code() -> None:
    """No production function may define or pass ``skip_status_check`` (spec §17.5, §23).

    The ``skip_status_check`` bypass allowed unauthorized cleanup without a
    terminal claim. It has been removed from all production code.
    """
    offenders: list[str] = []

    for path in sorted(APP_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "skip_status_check" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        relative = path.relative_to(APP_ROOT.parent)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in node.args.args:
                    if arg.arg == "skip_status_check":
                        offenders.append(
                            f"{relative}:{_line_number(node)} defines skip_status_check"
                        )
            if isinstance(node, ast.keyword):
                if node.arg == "skip_status_check":
                    offenders.append(
                        f"{relative}:{_line_number(node)} passes skip_status_check"
                    )

    assert offenders == []


def test_physical_cleanup_only_via_claim_or_coordinator() -> None:
    """cleanup_with_claim must be called only from coordinator/repair/cancel paths (spec §10.3, §17.5).

    Physical reclamation (force_remove + directory deletion) is authorized
    solely by a TerminalizationClaim or RepairClaim. Only the coordinator
    (aria2_lifecycle_service), the cancel facade (download_service),
    startup repair, and the cleanup module itself may invoke it.
    """
    allowed_paths = {
        APP_ROOT / "services" / "failed_task_cleanup.py",
        APP_ROOT / "services" / "aria2_lifecycle_service.py",
        APP_ROOT / "services" / "download_service.py",
        APP_ROOT / "services" / "repair.py",
    }
    offenders: list[str] = []

    for path in sorted(APP_ROOT.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "cleanup_with_claim" not in source:
            continue
        if path in allowed_paths:
            continue
        relative = path.relative_to(APP_ROOT.parent)
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else None
                if name == "cleanup_with_claim":
                    offenders.append(
                        f"{relative}:{_line_number(node)} calls cleanup_with_claim()"
                    )

    assert offenders == []


def test_force_remove_only_in_coordinator_and_cleanup() -> None:
    """force_remove must only be called from coordinator, cleanup module, or download_service (spec §10.3, §17.2).

    Stopping an aria2 writer is a physical reclamation step that must be
    authorized by a terminal or repair claim. It is not a general-purpose
    control primitive for services or routers.
    """
    allowed_paths = {
        APP_ROOT / "services" / "aria2_lifecycle_service.py",
        APP_ROOT / "services" / "failed_task_cleanup.py",
        APP_ROOT / "services" / "download_service.py",
        APP_ROOT / "services" / "repair.py",
    }
    offenders: list[str] = []

    for path in sorted(APP_ROOT.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "force_remove" not in source:
            continue
        if path in allowed_paths:
            continue
        relative = path.relative_to(APP_ROOT.parent)
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "force_remove":
                    offenders.append(
                        f"{relative}:{_line_number(node)} calls .force_remove()"
                    )

    assert offenders == []


def test_cleanup_task_download_dir_only_in_allowed_modules() -> None:
    """cleanup_task_download_dir must not be called from arbitrary services (spec §22.6).

    Directory deletion is restricted to the coordinator (completion cleanup),
    the cleanup module (claim-based reclamation), storage (definition),
    deletion cleanup, and startup repair.
    """
    allowed_paths = {
        APP_ROOT / "services" / "aria2_lifecycle_service.py",
        APP_ROOT / "services" / "failed_task_cleanup.py",
        APP_ROOT / "services" / "repair.py",
        APP_ROOT / "services" / "storage.py",
        APP_ROOT / "services" / "deletion_cleanup.py",
    }
    offenders: list[str] = []

    for path in sorted(APP_ROOT.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "cleanup_task_download_dir" not in source:
            continue
        if path in allowed_paths:
            continue
        relative = path.relative_to(APP_ROOT.parent)
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else None
                if name == "cleanup_task_download_dir":
                    offenders.append(
                        f"{relative}:{_line_number(node)} calls cleanup_task_download_dir()"
                    )

    assert offenders == []
