"""OpenAPI 契约快照守卫：路由/方法/响应模型与组件 schema 字段集的任何变化都必须显式更新快照。

有意变更契约时，运行：

    REGENERATE_OPENAPI_SNAPSHOT=1 uv run pytest tests/api/test_openapi_snapshot.py

然后在代码评审中核对 diff 并提交新生成的 tests/api/openapi_snapshot.json。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

SNAPSHOT_PATH = Path(__file__).resolve().parent / "openapi_snapshot.json"
_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "patch"})


def _response_model(response: dict[str, Any]) -> str:
    schema = response.get("content", {}).get("application/json", {}).get("schema", {})
    ref = schema.get("$ref")
    if ref is None:
        # 内联 schema（如空响应）以类型名兜底，保证结构变化仍可见。
        return str(schema.get("type", "none"))
    return str(ref).rsplit("/", maxsplit=1)[-1]


def _normalize(schema: dict[str, Any]) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for path, operations in schema["paths"].items():
        methods: dict[str, Any] = {}
        for method, operation in operations.items():
            if method not in _HTTP_METHODS:
                continue
            methods[method] = {
                "parameters": sorted(
                    parameter["name"]
                    for parameter in operation.get("parameters", [])
                    if "name" in parameter
                ),
                "responses": {
                    status: _response_model(response)
                    for status, response in operation["responses"].items()
                },
            }
        paths[path] = methods
    schemas = {
        name: {"fields": sorted(model.get("properties", {})), "required": sorted(model.get("required", []))}
        for name, model in schema["components"]["schemas"].items()
    }
    return {"paths": paths, "schemas": schemas}


def _diff_summary(actual: dict[str, Any], expected: dict[str, Any]) -> str:
    lines: list[str] = []
    for section in ("paths", "schemas"):
        actual_keys, expected_keys = set(actual[section]), set(expected[section])
        for key in sorted(actual_keys - expected_keys):
            lines.append(f"新增 {section}: {key}")
        for key in sorted(expected_keys - actual_keys):
            lines.append(f"移除 {section}: {key}")
        for key in sorted(actual_keys & expected_keys):
            if actual[section][key] != expected[section][key]:
                lines.append(f"变化 {section}: {key}")
    return "\n".join(lines)


def test_openapi_matches_committed_snapshot(app: FastAPI) -> None:
    snapshot = _normalize(app.openapi())

    if os.environ.get("REGENERATE_OPENAPI_SNAPSHOT") == "1":
        SNAPSHOT_PATH.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        pytest.fail("已重新生成 OpenAPI 快照，请核对 diff 后随契约变更一并提交")

    expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    if snapshot != expected:
        pytest.fail(
            "OpenAPI 契约与已提交快照不一致：\n"
            + (_diff_summary(snapshot, expected) or "（差异在字段细节，请对比 JSON）")
            + "\n若变更是有意的，请运行 "
            "REGENERATE_OPENAPI_SNAPSHOT=1 uv run pytest tests/api/test_openapi_snapshot.py "
            "更新快照并提交。"
        )
