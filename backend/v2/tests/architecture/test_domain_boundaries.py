from __future__ import annotations

import ast
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DOMAIN_ROOT = BACKEND_ROOT / "src" / "aria2deck" / "domain"
APPLICATION_ROOT = BACKEND_ROOT / "src" / "aria2deck" / "application"


def _resolved_import_module(path: Path, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    package_parts = list(path.parent.relative_to(BACKEND_ROOT / "src").parts)
    parent_count = node.level - 1
    resolved = package_parts[: max(len(package_parts) - parent_count, 0)]
    if node.module:
        resolved.extend(node.module.split("."))
    return ".".join(resolved)


def _domain_import_violations(path: Path, source: str) -> list[str]:
    violations: list[str] = []
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [_resolved_import_module(path, node)]
        for module in modules:
            root = module.split(".")[0]
            if root not in sys.stdlib_module_names and root != "aria2deck":
                violations.append(f"{path}:{node.lineno}:{module}")
            if module.startswith("aria2deck") and not module.startswith("aria2deck.domain"):
                violations.append(f"{path}:{node.lineno}:{module}")
    return violations


def test_domain_imports_only_standard_library_and_domain_modules() -> None:
    violations = [
        violation
        for path in DOMAIN_ROOT.glob("*.py")
        for violation in _domain_import_violations(path, path.read_text(encoding="utf-8"))
    ]
    assert violations == []


def test_domain_has_no_dynamic_imports() -> None:
    for path in DOMAIN_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert not (isinstance(node.func, ast.Name) and node.func.id == "__import__")
                assert not (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")


def test_domain_checker_resolves_and_rejects_relative_outer_imports() -> None:
    path = DOMAIN_ROOT / "example.py"
    source = "from ..application import downloads"

    assert _resolved_import_module(path, ast.parse(source).body[0]) == "aria2deck.application"
    assert _domain_import_violations(path, source) == [
        f"{path}:1:aria2deck.application"
    ]


def test_domain_checker_resolves_relative_parent_without_a_module_name() -> None:
    path = DOMAIN_ROOT / "example.py"
    source = "from .. import application"

    assert _resolved_import_module(path, ast.parse(source).body[0]) == "aria2deck"
    assert _domain_import_violations(path, source) == [f"{path}:1:aria2deck"]


def test_domain_checker_allows_relative_imports_within_domain() -> None:
    path = DOMAIN_ROOT / "example.py"
    node = ast.parse("from .errors import DomainError").body[0]
    assert _resolved_import_module(path, node) == "aria2deck.domain.errors"
    assert _domain_import_violations(path, "from . import errors") == []


def test_application_imports_stay_inside_application_and_domain() -> None:
    violations: list[str] = []
    for path in APPLICATION_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [_resolved_import_module(path, node)]
            for module in modules:
                root = module.split(".")[0]
                if root in {"fastapi", "sqlalchemy", "pydantic"} or module.startswith(
                    ("aria2deck.api", "aria2deck.infrastructure", "aria2deck.bootstrap")
                ):
                    violations.append(f"{path}:{node.lineno}:{module}")
    assert violations == []
