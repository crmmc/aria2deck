from __future__ import annotations

import ast
from pathlib import Path

from .test_domain_boundaries import _resolved_import_module

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = BACKEND_ROOT / "src" / "aria2deck"
APPLICATION_ROOT = PACKAGE_ROOT / "application"
API_ROOT = PACKAGE_ROOT / "api"
DOMAIN_ROOT = PACKAGE_ROOT / "domain"
INFRASTRUCTURE_ROOT = PACKAGE_ROOT / "infrastructure"

# 上下文地图：application/<context> 允许依赖的其它上下文。
# 新增上下文或依赖时，必须先更新这里和 design.md，再继续写代码。
CONTEXT_DEPENDENCIES: dict[str, frozenset[str]] = {
    "accounts": frozenset(),
    "downloads": frozenset({"accounts", "settings", "library"}),
    "library": frozenset(),
    "packs": frozenset({"library"}),
    "shares": frozenset({"accounts", "library"}),
    "settings": frozenset({"accounts"}),
    "system": frozenset(),
}

# application.ports 是共享内核，任何上下文都可以依赖。
SHARED_APPLICATION_MODULES = frozenset({"ports"})

# 跨上下文只能依赖 DTO、端口或共享辅助模块，不能直接导入对方的用例服务。
FORBIDDEN_CROSS_CONTEXT_MODULES = frozenset(
    {"service", "commands", "queries", "gateway", "history", "recovery", "cleanup"}
)

FORBIDDEN_API_PREFIXES = (
    "aria2deck.infrastructure",
    "aria2deck.bootstrap",
    "sqlalchemy",
)

FORBIDDEN_INFRASTRUCTURE_PREFIXES = (
    "aria2deck.api",
    "aria2deck.bootstrap",
)

# TrackerCache 目前是应用层 DTO，但和 TrackerService 放在同一模块里。
# 下次改 Tracker 相关代码时把它移到 application/settings/models.py，并删除这个例外。
INFRASTRUCTURE_APPLICATION_EXCEPTIONS = frozenset(
    {"aria2deck.application.settings.trackers"}
)


def _imported_modules(path: Path, source: str) -> list[tuple[int, str]]:
    imported: list[tuple[int, str]] = []
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolved_import_module(path, node)
            if base:
                imported.append((node.lineno, base))
            imported.extend(
                (node.lineno, f"{base}.{alias.name}" if base else alias.name)
                for alias in node.names
                if alias.name != "*"
            )
    return imported


def _source_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _context_directories() -> set[str]:
    return {
        path.name
        for path in APPLICATION_ROOT.iterdir()
        if path.is_dir() and path.name != "__pycache__"
    }


def _file_context(path: Path) -> str | None:
    relative = path.relative_to(APPLICATION_ROOT)
    return relative.parts[0] if len(relative.parts) > 1 else None


def _module_context(module: str) -> str | None:
    prefix = "aria2deck.application."
    if not module.startswith(prefix):
        return None
    parts = module[len(prefix) :].split(".")
    if not parts or parts[0] in SHARED_APPLICATION_MODULES:
        return None
    return parts[0]


def _domain_dependencies() -> dict[str, set[str]]:
    dependencies: dict[str, set[str]] = {}
    for path in _source_files(DOMAIN_ROOT):
        current = path.stem
        dependencies.setdefault(current, set())
        for _, module in _imported_modules(path, path.read_text(encoding="utf-8")):
            if not module.startswith("aria2deck.domain."):
                continue
            parts = module.split(".")
            if len(parts) > 2 and parts[2] != current:
                dependencies[current].add(parts[2])
    return dependencies


def _assert_acyclic(graph: dict[str, set[str] | frozenset[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        assert node not in visiting, f"依赖存在环: {node}"
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)


def _application_context_violations() -> list[str]:
    violations: list[str] = []
    for path in _source_files(APPLICATION_ROOT):
        current = _file_context(path)
        if current is None:
            continue
        for line, module in _imported_modules(path, path.read_text(encoding="utf-8")):
            imported = _module_context(module)
            if imported is None or imported == current:
                continue
            if imported not in CONTEXT_DEPENDENCIES:
                violations.append(
                    f"{path}:{line}: 未登记的上下文 {imported}（import {module}）"
                )
                continue
            if imported not in CONTEXT_DEPENDENCIES[current]:
                violations.append(
                    f"{path}:{line}: {current} 不允许依赖 {imported}（import {module}）"
                )
                continue
            parts = module.split(".")
            if len(parts) > 3 and parts[3] in FORBIDDEN_CROSS_CONTEXT_MODULES:
                violations.append(
                    f"{path}:{line}: 跨上下文不得直接导入用例模块 {module}"
                )
    return violations


def _import_violations(
    root: Path, forbidden_prefixes: tuple[str, ...]
) -> list[str]:
    violations: list[str] = []
    for path in _source_files(root):
        for line, module in _imported_modules(path, path.read_text(encoding="utf-8")):
            if module.startswith(forbidden_prefixes):
                violations.append(f"{path}:{line}: 禁止导入 {module}")
    return violations


def _infrastructure_application_violations() -> list[str]:
    violations: list[str] = []
    for path in _source_files(INFRASTRUCTURE_ROOT):
        for line, module in _imported_modules(path, path.read_text(encoding="utf-8")):
            if not module.startswith("aria2deck.application."):
                continue
            if any(
                module == exception or module.startswith(f"{exception}.")
                for exception in INFRASTRUCTURE_APPLICATION_EXCEPTIONS
            ):
                continue
            parts = module.split(".")
            if len(parts) > 3 and parts[3] in FORBIDDEN_CROSS_CONTEXT_MODULES:
                violations.append(
                    f"{path}:{line}: 基础设施不得直接导入应用用例模块 {module}"
                )
    return violations


def test_every_application_context_is_registered() -> None:
    unregistered = _context_directories() - set(CONTEXT_DEPENDENCIES)
    assert unregistered == set(), (
        f"application/ 下存在未登记上下文: {sorted(unregistered)}；"
        "请先更新 design.md 和 CONTEXT_DEPENDENCIES"
    )


def test_module_context_ignores_shared_and_non_application_modules() -> None:
    assert _module_context("aria2deck.application.ports") is None
    assert _module_context("aria2deck.application.downloads.commands") == "downloads"
    assert _module_context("aria2deck.domain.downloads") is None


def test_file_context_uses_first_package_after_application() -> None:
    assert _file_context(APPLICATION_ROOT / "downloads" / "commands.py") == "downloads"
    assert _file_context(APPLICATION_ROOT / "ports.py") is None


def test_context_dependencies_reference_known_contexts() -> None:
    known = set(CONTEXT_DEPENDENCIES)
    for context, dependencies in CONTEXT_DEPENDENCIES.items():
        assert context not in dependencies, f"{context} 不能依赖自己"
        assert dependencies <= known, (
            f"{context} 依赖了未登记上下文: {sorted(dependencies - known)}"
        )


def test_context_dependency_graph_is_acyclic() -> None:
    _assert_acyclic(CONTEXT_DEPENDENCIES)


def test_domain_module_dependency_graph_is_acyclic() -> None:
    dependencies = _domain_dependencies()
    assert "errors" in dependencies
    _assert_acyclic(dependencies)


def test_application_cross_context_imports_match_context_map() -> None:
    assert _application_context_violations() == []


def test_application_does_not_import_outer_layers_or_frameworks() -> None:
    assert (
        _import_violations(
            APPLICATION_ROOT,
            (
                "aria2deck.api",
                "aria2deck.infrastructure",
                "aria2deck.bootstrap",
                "fastapi",
                "sqlalchemy",
                "pydantic",
            ),
        )
        == []
    )


def test_api_does_not_import_infrastructure_or_bootstrap() -> None:
    assert _import_violations(API_ROOT, FORBIDDEN_API_PREFIXES) == []


def test_infrastructure_does_not_import_outer_layers_or_use_case_services() -> None:
    assert (
        _import_violations(INFRASTRUCTURE_ROOT, FORBIDDEN_INFRASTRUCTURE_PREFIXES)
        == []
    )
    assert _infrastructure_application_violations() == []


def test_standalone_verification_starts_aria2_and_sets_internal_base_url() -> None:
    script = (BACKEND_ROOT.parents[1] / "scripts" / "verify-v2-standalone.sh").read_text(
        encoding="utf-8"
    )
    assert "aria2/start.sh" in script
    assert "aria2.getVersion" in script
    assert "ARIA2DECK_ARIA2_RPC_URL" in script
    assert "ARIA2DECK_INTERNAL_BASE_URL" in script
