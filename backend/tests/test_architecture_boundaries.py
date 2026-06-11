from __future__ import annotations

import ast
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1] / "app"


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
