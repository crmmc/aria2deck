from __future__ import annotations

import ast
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1] / "app"
P3_MIGRATED_ARIA2_FILES = [
    "aria2/download_ops.py",
]


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
                offenders.append(f"{relative}:{node.lineno} imports {imported}")

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
            offenders.append(f"{relative}:{node.lineno} imports {sorted(blocked)}")

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
                offenders.append(f"{relative}:{node.lineno} imports {imported}")

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
                offenders.append(f"{relative}:{node.lineno} imports {imported}")

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
                    offenders.append(f"{relative}:{node.lineno} calls {name}()")

    assert offenders == []


def test_routers_do_not_import_other_router_modules() -> None:
    offenders: list[str] = []
    for path in _python_files("routers"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = _imports_router(node)
            if imported is not None:
                relative = path.relative_to(APP_ROOT.parent)
                offenders.append(f"{relative}:{node.lineno} imports {imported}")

    assert offenders == []


def test_p3_migrated_aria2_modules_do_not_import_direct_db_or_sqlalchemy() -> None:
    offenders: list[str] = []
    blocked_modules = {
        "app.db.engine",
        "app.db.schema",
        "sqlalchemy",
    }

    for relative_path in P3_MIGRATED_ARIA2_FILES:
        path = APP_ROOT / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = _imports_blocked_module_prefix(node, blocked_modules)
            if imported is not None:
                relative = path.relative_to(APP_ROOT.parent)
                offenders.append(f"{relative}:{node.lineno} imports {imported}")

    assert offenders == []
