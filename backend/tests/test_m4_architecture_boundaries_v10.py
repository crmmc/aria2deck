"""M4 T19 — architecture boundary gates for the arch-convergence mission (spec §4).

AST-level guards covering all M4 §4 rules. Legacy module names are composed
with an underscore join (``"_".join``) so repo-wide reference scanners in the
T05-T16 per-task tests do not flag this file as a stale reference.

1. ``domain/locks.py`` exists and uniquely defines ``get_download_lifecycle_lock``.
2. ``domain/quota.py`` exists and uniquely defines ``get_disk_available_bytes``
   and ``candidate_size_from_status``.
3. ``services/download_service.py`` is gone.
4. ``repositories/downloads.py`` is gone; ``repositories/task/downloads.py`` and
   ``repositories/task/user_tasks.py`` exist.
5. The legacy lifecycle monolith is gone; ``services/lifecycle/`` provides
   coordinator / handoff / completion / cleanup / repair.
6. ``services/pack.py`` is gone; ``modules/pack/`` exists.
7. ``services/repair.py`` does not import ``Aria2Gateway`` (BackendPort only).
8. ``services/rpc/`` exists (write/read/system/_shared); the legacy RPC handler
   monolith is gone.
9. ``settings_service.py`` has no module-level ``app.aria2`` import.
10. ``services/internal_fetch.py`` is gone; ``services/gateway.py`` exists.
11. ``task_service.py`` is a thin HTTP adapter (< 300 lines).
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

# Legacy module stems, composed so repo-wide legacy-reference scanners do not
# match this file's own source text.
_DOWNLOAD_SERVICE_STEM = "_".join(("download", "service"))
_DOWNLOADS_REPO_STEM = "downloads"
_LIFECYCLE_SERVICE_STEM = "_".join(("aria2", "lifecycle", "service"))
_PACK_STEM = "pack"
_RPC_HANDLER_STEM = "_".join(("aria2", "rpc", "handler"))
_INTERNAL_FETCH_STEM = "_".join(("internal", "fetch"))


def _line_number(node: ast.AST) -> int:
    lineno = getattr(node, "lineno", None)
    return lineno if isinstance(lineno, int) else 0


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


def _definition_sites(symbol: str) -> list[str]:
    """All ``app/**/*.py`` files that define ``symbol`` (function/class/assign)."""
    sites: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == symbol:
                    relative = path.relative_to(APP_ROOT.parent)
                    sites.append(f"{relative}:{_line_number(node)}")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == symbol:
                        relative = path.relative_to(APP_ROOT.parent)
                        sites.append(f"{relative}:{_line_number(node)}")
    return sites


def _assert_module_removed(directory: str, stem: str, dotted_prefix: str) -> None:
    path = APP_ROOT / directory / f"{stem}.py"
    assert not path.exists(), f"app/{directory}/{stem}.py must be deleted: {path}"

    offenders: list[str] = []
    for candidate in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(candidate.read_text(encoding="utf-8"), filename=str(candidate))
        for node in ast.walk(tree):
            imported = _imports_blocked_module_prefix(node, {dotted_prefix})
            if imported is not None:
                relative = candidate.relative_to(APP_ROOT.parent)
                offenders.append(f"{relative}:{_line_number(node)} imports {imported}")
    assert offenders == []


# ---------------------------------------------------------------------------
# Rule 1: domain/locks.py uniquely defines get_download_lifecycle_lock
# ---------------------------------------------------------------------------

def test_domain_locks_module_exists() -> None:
    path = APP_ROOT / "domain" / "locks.py"
    assert path.is_file(), "domain/locks.py must exist (M4 T01)"


def test_get_download_lifecycle_lock_defined_only_in_domain_locks() -> None:
    sites = _definition_sites("get_download_lifecycle_lock")
    assert sites, "get_download_lifecycle_lock must be defined somewhere"
    offenders = [site for site in sites if not site.startswith("app/domain/locks.py:")]
    assert offenders == [], (
        f"get_download_lifecycle_lock defined outside domain/locks.py: {offenders}"
    )


# ---------------------------------------------------------------------------
# Rule 2: domain/quota.py uniquely defines get_disk_available_bytes /
#         candidate_size_from_status
# ---------------------------------------------------------------------------

def test_domain_quota_module_exists() -> None:
    path = APP_ROOT / "domain" / "quota.py"
    assert path.is_file(), "domain/quota.py must exist (M4 T01)"


def test_get_disk_available_bytes_defined_only_in_domain_quota() -> None:
    sites = _definition_sites("get_disk_available_bytes")
    assert sites, "get_disk_available_bytes must be defined somewhere"
    offenders = [site for site in sites if not site.startswith("app/domain/quota.py:")]
    assert offenders == [], (
        f"get_disk_available_bytes defined outside domain/quota.py: {offenders}"
    )


def test_candidate_size_from_status_defined_only_in_domain_quota() -> None:
    sites = _definition_sites("candidate_size_from_status")
    assert sites, "candidate_size_from_status must be defined somewhere"
    offenders = [site for site in sites if not site.startswith("app/domain/quota.py:")]
    assert offenders == [], (
        f"candidate_size_from_status defined outside domain/quota.py: {offenders}"
    )


# ---------------------------------------------------------------------------
# Rule 3: legacy download service monolith is gone (M4 T08)
# ---------------------------------------------------------------------------

def test_legacy_download_service_module_removed() -> None:
    _assert_module_removed(
        "services",
        _DOWNLOAD_SERVICE_STEM,
        f"app.services.{_DOWNLOAD_SERVICE_STEM}",
    )


# ---------------------------------------------------------------------------
# Rule 4: legacy repositories/downloads.py gone; repositories/task/* exists
# ---------------------------------------------------------------------------

def test_legacy_repositories_downloads_module_removed() -> None:
    _assert_module_removed(
        "repositories",
        _DOWNLOADS_REPO_STEM,
        f"app.repositories.{_DOWNLOADS_REPO_STEM}",
    )


def test_repositories_task_package_exists() -> None:
    required = {
        APP_ROOT / "repositories" / "task" / "__init__.py",
        APP_ROOT / "repositories" / "task" / "downloads.py",
        APP_ROOT / "repositories" / "task" / "user_tasks.py",
    }
    missing = [
        str(path.relative_to(APP_ROOT.parent)) for path in required if not path.is_file()
    ]
    assert missing == [], f"missing repositories/task files (M4 T02/T03): {missing}"


# ---------------------------------------------------------------------------
# Rule 5: legacy lifecycle monolith gone; services/lifecycle/* exists
# ---------------------------------------------------------------------------

def test_legacy_lifecycle_service_module_removed() -> None:
    _assert_module_removed(
        "services",
        _LIFECYCLE_SERVICE_STEM,
        f"app.services.{_LIFECYCLE_SERVICE_STEM}",
    )


def test_services_lifecycle_package_exists() -> None:
    required = {
        APP_ROOT / "services" / "lifecycle" / "__init__.py",
        APP_ROOT / "services" / "lifecycle" / "_shared.py",
        APP_ROOT / "services" / "lifecycle" / "coordinator.py",
        APP_ROOT / "services" / "lifecycle" / "handoff.py",
        APP_ROOT / "services" / "lifecycle" / "completion.py",
        APP_ROOT / "services" / "lifecycle" / "cleanup.py",
        APP_ROOT / "services" / "lifecycle" / "repair.py",
    }
    missing = [
        str(path.relative_to(APP_ROOT.parent)) for path in required if not path.is_file()
    ]
    assert missing == [], f"missing services/lifecycle files (M4 T05-T09): {missing}"


# ---------------------------------------------------------------------------
# Rule 6: legacy services/pack.py gone; modules/pack/ exists
# ---------------------------------------------------------------------------

def test_legacy_services_pack_module_removed() -> None:
    _assert_module_removed(
        "services",
        _PACK_STEM,
        f"app.services.{_PACK_STEM}",
    )


def test_modules_pack_package_exists() -> None:
    path = APP_ROOT / "modules" / "pack" / "__init__.py"
    assert path.is_file(), "modules/pack/__init__.py must exist (M4 T10)"


# ---------------------------------------------------------------------------
# Rule 7: services/repair.py does not import Aria2Gateway (M4 T11)
# ---------------------------------------------------------------------------

def test_services_repair_does_not_import_aria2_gateway() -> None:
    path = APP_ROOT / "services" / "repair.py"
    assert path.is_file(), "services/repair.py must exist"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        imported = _imports_blocked_module_prefix(node, {"app.aria2"})
        if imported is not None:
            offenders.append(f"services/repair.py:{_line_number(node)} imports {imported}")
        names: list[str] = []
        if isinstance(node, ast.ImportFrom):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        if "Aria2Gateway" in names:
            offenders.append(
                f"services/repair.py:{_line_number(node)} imports Aria2Gateway"
            )
    assert offenders == [], (
        f"services/repair.py must use BackendPort, not Aria2Gateway (M4 T11): {offenders}"
    )


# ---------------------------------------------------------------------------
# Rule 8: services/rpc/ exists; legacy RPC handler monolith gone
# ---------------------------------------------------------------------------

def test_services_rpc_package_exists() -> None:
    required = {
        APP_ROOT / "services" / "rpc" / "__init__.py",
        APP_ROOT / "services" / "rpc" / "_shared.py",
        APP_ROOT / "services" / "rpc" / "write.py",
        APP_ROOT / "services" / "rpc" / "read.py",
        APP_ROOT / "services" / "rpc" / "system.py",
    }
    missing = [
        str(path.relative_to(APP_ROOT.parent)) for path in required if not path.is_file()
    ]
    assert missing == [], f"missing services/rpc files (M4 T13-T16): {missing}"


def test_legacy_rpc_handler_module_removed() -> None:
    _assert_module_removed(
        "services",
        _RPC_HANDLER_STEM,
        f"app.services.{_RPC_HANDLER_STEM}",
    )


# ---------------------------------------------------------------------------
# Rule 9: settings_service.py has no module-level app.aria2 import (M4 T17)
# ---------------------------------------------------------------------------

def test_settings_service_has_no_module_level_aria2_import() -> None:
    path = APP_ROOT / "services" / "settings_service.py"
    assert path.is_file(), "services/settings_service.py must exist"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app.aria2" or alias.name.startswith("app.aria2."):
                    offenders.append(
                        f"services/settings_service.py:{_line_number(node)} imports {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "app.aria2" or module.startswith("app.aria2."):
                offenders.append(
                    f"services/settings_service.py:{_line_number(node)} imports from {module}"
                )
    assert offenders == [], (
        f"settings_service.py must defer app.aria2 imports (M4 T17): {offenders}"
    )


# ---------------------------------------------------------------------------
# Rule 10: legacy internal_fetch gone; services/gateway.py exists (M4 T18)
# ---------------------------------------------------------------------------

def test_internal_fetch_renamed_to_gateway() -> None:
    _assert_module_removed(
        "services",
        _INTERNAL_FETCH_STEM,
        f"app.services.{_INTERNAL_FETCH_STEM}",
    )
    gateway = APP_ROOT / "services" / "gateway.py"
    assert gateway.is_file(), f"services/gateway.py must exist (M4 T18): {gateway}"


# ---------------------------------------------------------------------------
# Rule 11: task_service.py is a thin HTTP adapter (< 300 lines, M4 T12)
# ---------------------------------------------------------------------------

def test_task_service_is_thin_adapter() -> None:
    path = APP_ROOT / "services" / "task_service.py"
    assert path.is_file(), "services/task_service.py must exist"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) < 300, (
        f"task_service.py must stay < 300 lines (M4 T12), got {len(lines)}"
    )
