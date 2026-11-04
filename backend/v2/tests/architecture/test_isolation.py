from __future__ import annotations

import ast
import json
import os
import re
import subprocess  # noqa: S404  # 固定当前 Python 可执行文件，仅用于隔离启动测试
import sys
import tomllib
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parents[1]
FRONTEND_ROOT = REPO_ROOT / "frontend" / "v2"


def _is_forbidden_module(module: str) -> bool:
    normalized = module.replace("\\", "/").strip(".")
    segments = set(normalized.split("/"))
    dotted = normalized.replace("/", ".")
    return (
        "v1" in segments
        or "v1" in dotted.split(".")
        or dotted == "app"
        or dotted.startswith("app.")
    )


def _runtime_files() -> list[Path]:
    backend_files = list((BACKEND_ROOT / "src").rglob("*.py"))
    frontend_files = [
        path
        for extension in ("*.ts", "*.tsx", "*.js", "*.css")
        for path in (FRONTEND_ROOT / "src").rglob(extension)
    ]
    config_files = [
        BACKEND_ROOT / "pyproject.toml",
        FRONTEND_ROOT / "package.json",
        FRONTEND_ROOT / "tsconfig.json",
        FRONTEND_ROOT / "vite.config.ts",
        FRONTEND_ROOT / "jest.config.cjs",
        FRONTEND_ROOT / "eslint.config.js",
    ]
    return [*backend_files, *frontend_files, *config_files]


def test_python_static_and_dynamic_imports_do_not_use_v1() -> None:
    violations: list[str] = []
    for path in (BACKEND_ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules = [node.module]
            elif isinstance(node, ast.Call) and node.args:
                is_dynamic_import = (
                    isinstance(node.func, ast.Name) and node.func.id == "__import__"
                ) or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                )
                if is_dynamic_import and isinstance(node.args[0], ast.Constant):
                    value = node.args[0].value
                    if isinstance(value, str):
                        modules = [value]
            for module in modules:
                if _is_forbidden_module(module):
                    violations.append(f"{path}:{node.lineno}:{module}")
    assert violations == []


def test_frontend_imports_and_aliases_do_not_use_v1() -> None:
    import_pattern = re.compile(
        r"(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)[\"']([^\"']+)[\"']"
    )
    for path in (FRONTEND_ROOT / "src").rglob("*"):
        if path.suffix not in {".ts", ".tsx", ".js"}:
            continue
        for module in import_pattern.findall(path.read_text(encoding="utf-8")):
            assert not _is_forbidden_module(module), f"{path}: {module}"

    tsconfig = json.loads((FRONTEND_ROOT / "tsconfig.json").read_text(encoding="utf-8"))
    aliases = tsconfig["compilerOptions"]["paths"]
    assert all(not _is_forbidden_module(target) for values in aliases.values() for target in values)


def test_runtime_and_project_configs_have_no_archive_path_reference() -> None:
    archive_token = re.compile(r"(?<![a-z0-9])v1(?![a-z0-9])", re.IGNORECASE)
    violations = [
        str(path)
        for path in _runtime_files()
        if archive_token.search(path.read_text(encoding="utf-8"))
    ]
    assert violations == []


def test_active_build_and_release_entries_only_use_v2() -> None:
    active_files = [
        REPO_ROOT / "Makefile",
        REPO_ROOT / "Dockerfile",
        REPO_ROOT / "docker-compose.yml",
        *(REPO_ROOT / ".github" / "workflows").glob("*.yml"),
    ]
    forbidden = (
        "backend/app",
        "backend/tests",
        "backend/v1",
        "frontend/app",
        "frontend/components",
        "frontend/__tests__",
        "frontend/v1",
        "frontend/out",
        "app.main:app",
        "NEXT_PUBLIC",
    )
    violations = [
        f"{path}:{token}"
        for path in active_files
        for token in forbidden
        if token in path.read_text(encoding="utf-8")
    ]
    assert violations == []


def test_package_discovery_and_test_collection_are_scoped_to_v2() -> None:
    config = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/aria2deck"
    ]
    assert config["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]
    assert not (REPO_ROOT / "pyproject.toml").exists()
    root_pytest = (REPO_ROOT / "pytest.ini").read_text(encoding="utf-8")
    assert "testpaths = backend/v2/tests" in root_pytest
    assert "norecursedirs = backend/v1 frontend/v1" in root_pytest

    package = json.loads((FRONTEND_ROOT / "package.json").read_text(encoding="utf-8"))
    dependencies = {**package["dependencies"], **package["devDependencies"]}
    assert "next" not in dependencies
    jest_config = (FRONTEND_ROOT / "jest.config.cjs").read_text(encoding="utf-8")
    assert "roots: ['<rootDir>/tests']" in jest_config


def test_docker_context_excludes_archives_and_runtime_data() -> None:
    ignored = set((REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())
    assert {
        "backend/v1",
        "frontend/v1",
        "backend/data",
        "backend/downloads",
        "backend/v2/tests",
        "frontend/v2/tests",
    } <= ignored



def _matches_dockerignore_rule(path: str, rule: str) -> bool:
    rule = rule.strip("/")
    prefix = ""
    if rule.startswith("**/"):
        prefix = "(?:.*/)?"
        rule = rule[3:]
    expression = prefix + re.escape(rule).replace(r"\*", "[^/]*")
    parts = path.split("/")
    candidates = ("/".join(parts[:end]) for end in range(1, len(parts) + 1))
    return any(re.fullmatch(expression, candidate) for candidate in candidates)


def _is_dockerignored(path: str) -> bool:
    ignored = False
    rules = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for raw_rule in rules:
        rule = raw_rule.strip()
        if not rule or rule.startswith("#"):
            continue
        negated = rule.startswith("!")
        if negated:
            rule = rule[1:]
        if _matches_dockerignore_rule(path, rule):
            ignored = not negated
    return ignored


@pytest.mark.parametrize(
    "path",
    [
        ".venv/bin/python",
        "backend/v2/.venv/bin/python",
        "frontend/v2/node_modules/react/index.js",
        "frontend/v2/tests/helper/node_modules/fake/index.js",
        "aria2.log",
        "backend/aria2/aria2.log",
        "backend/aria2/aria2.session",
        "backend/aria2/aria2.session.lock",
        "backend/aria2/download.aria2",
        "backend/aria2/DHT.dat",
        "backend/aria2/DHT6.dat",
        "backend/v2/aria2/runtime/aria2.log",
        "backend/v2/aria2/runtime/aria2.session",
        "data/app/v2.db",
        "data/downloads/file.bin",
    ],
)
def test_docker_context_excludes_concrete_runtime_paths(path: str) -> None:
    assert _is_dockerignored(path)


@pytest.mark.parametrize(
    "path",
    [
        "backend/aria2/start.sh",
        "backend/v2/aria2/start.sh",
        "backend/v2/pyproject.toml",
        "backend/v2/src/aria2deck/main.py",
        "backend/v2/uv.lock",
        "frontend/v2/bun.lock",
        "frontend/v2/package.json",
        "frontend/v2/src/main.tsx",
    ],
)
def test_docker_context_keeps_concrete_v2_inputs(path: str) -> None:
    assert not _is_dockerignored(path)


def test_v2_aria2_script_runs_in_foreground_with_scoped_paths(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    arguments_file = tmp_path / "arguments.txt"
    fake_aria2 = fake_bin / "aria2c"
    fake_aria2.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"${ARIA2_ARGS_FILE}\"\n",
        encoding="utf-8",
    )
    fake_aria2.chmod(0o700)
    download_dir = tmp_path / "downloads"
    runtime_dir = tmp_path / "runtime"
    environment = os.environ.copy()
    environment.update(
        {
            "ARIA2DECK_ARIA2_RUNTIME_DIR": str(runtime_dir),
            "ARIA2DECK_ARIA2_RPC_PORT": "6899",
            "ARIA2DECK_DOWNLOAD_DIR": str(download_dir),
            "ARIA2_ARGS_FILE": str(arguments_file),
            "PATH": f"{fake_bin}:{environment['PATH']}",
        }
    )

    result = subprocess.run(  # noqa: S603  # 固定调用 V2 脚本，fake aria2c 不执行下载
        ["/bin/bash", str(BACKEND_ROOT / "aria2" / "start.sh")],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    arguments = arguments_file.read_text(encoding="utf-8").splitlines()
    assert "--enable-rpc=true" in arguments
    assert "--rpc-listen-all=false" in arguments
    assert "--rpc-listen-port=6899" in arguments
    assert f"--dir={download_dir}" in arguments
    assert f"--log={runtime_dir / 'aria2.log'}" in arguments
    assert f"--dht-file-path={runtime_dir / 'DHT.dat'}" in arguments
    assert (runtime_dir / "aria2.session").is_file()


def test_v2_aria2_script_reports_missing_binary(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["PATH"] = str(tmp_path)

    result = subprocess.run(  # noqa: S603  # 固定调用 V2 脚本并隔离 PATH
        ["/bin/bash", str(BACKEND_ROOT / "aria2" / "start.sh")],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "aria2c 未安装" in result.stderr


def test_application_boots_with_legacy_imports_blocked(tmp_path: Path) -> None:
    program = """
import builtins

original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == "app" or name.startswith(("app.", "v1", "backend.v1")):
        raise AssertionError(f"forbidden legacy import: {name}")
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
from fastapi.testclient import TestClient
from aria2deck.bootstrap import create_app

with TestClient(create_app()) as client:
    response = client.get("/api/health/ready")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(BACKEND_ROOT / "src")
    environment["ARIA2DECK_DEBUG"] = "true"
    environment["ARIA2DECK_DATABASE_PATH"] = str(tmp_path / "v2.db")
    environment["ARIA2DECK_DOWNLOAD_DIR"] = str(tmp_path / "downloads")
    environment["ARIA2DECK_STATIC_DIR"] = str(tmp_path / "static")
    result = subprocess.run(  # noqa: S603  # 参数固定，无 shell 或外部输入
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_dev_backend_target_explicitly_enables_debug_configuration() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text()
    target = makefile.split("dev-back:\n", 1)[1].split("\n\n", 1)[0]
    assert "cd $(BACKEND_DIR) && ARIA2DECK_DEBUG=true uv run uvicorn" in target
