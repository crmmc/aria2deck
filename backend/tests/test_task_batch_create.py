"""M24 Task3 数组 REST 契约并存接入（Expand）测试。

真实 FastAPI authenticated client；aria2 client 通过 patch
``app.services.task_service._get_client`` 注入 fake；service callback
行为在 test_task_batch_submission.py 覆盖。
"""

from __future__ import annotations

from typing import Any
from pathlib import Path
import re
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.aria2.client import MulticallOutcome
from app.services.task_batch_submission import (
    BatchCreateResult,
    BatchTaskItemResult,
    BatchSubmissionUndeterminedError,
)


def _ok(value: Any) -> MulticallOutcome:
    return MulticallOutcome(ok=True, result=value)


def _find_planned_gid(params: list[Any]) -> str | None:
    for param in params:
        if isinstance(param, dict) and isinstance(param.get("gid"), str):
            return param["gid"]
        if isinstance(param, list):
            found = _find_planned_gid(param)
            if found is not None:
                return found
    return None


class FakeBatchAria2Client:
    """multicall 返回与 addUri/addTorrent options.gid 一致的 ok outcome。"""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def multicall(self, calls: list[dict]) -> list[MulticallOutcome]:
        self.calls.append(calls)
        return [_ok(_find_planned_gid(call.get("params") or [])) for call in calls]

    async def add_uri(self, *args: Any, **kwargs: Any) -> str:
        return "legacy-gid"

    async def force_remove(self, gid: str) -> None:
        return None

    async def remove(self, gid: str) -> str:
        return gid


def _magnet(info_hash: str) -> str:
    return f"magnet:?xt=urn:btih:{info_hash}"


class TestBatchCreateContract:
    @patch("app.services.task_service._get_client")
    def test_array_body_returns_batch_response(
        self,
        mock_get_client: Any,
        authenticated_client: TestClient,
    ) -> None:
        fake = FakeBatchAria2Client()
        mock_get_client.return_value = fake
        response = authenticated_client.post(
            "/api/tasks",
            json={"tasks": [{"uri": _magnet("0" * 40)}, {"uri": _magnet("1" * 40)}]},
        )
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"accepted_count", "failed_count", "results"}
        assert body["accepted_count"] == 2
        assert body["failed_count"] == 0
        assert len(body["results"]) == 2
        for index, item in enumerate(body["results"]):
            assert set(item) == {"input_index", "accepted", "task_id", "status", "error"}
            assert item["input_index"] == index
            assert item["accepted"] is True
            assert isinstance(item["task_id"], int)
            assert item["status"] == "paused"
            assert item["error"] is None
        assert "uri" not in TestBatchCreateContract._flatten(body)

    @staticmethod
    def _flatten(value: Any) -> list[Any]:
        if isinstance(value, dict):
            out: list[Any] = []
            for key, item in value.items():
                out.append(key)
                out.extend(TestBatchCreateContract._flatten(item))
            return out
        if isinstance(value, list):
            out = []
            for item in value:
                out.extend(TestBatchCreateContract._flatten(item))
            return out
        return [value]

    @patch("app.services.task_service._get_client")
    def test_empty_tasks_rejected_without_side_effects(
        self,
        mock_get_client: Any,
        authenticated_client: TestClient,
    ) -> None:
        fake = FakeBatchAria2Client()
        mock_get_client.return_value = fake
        response = authenticated_client.post("/api/tasks", json={"tasks": []})
        assert response.status_code == 422
        assert "任务" in response.json()["detail"]
        assert fake.calls == []

    @patch("app.services.task_service._get_client")
    def test_over_30_unique_after_trim_dedup_rejected(
        self,
        mock_get_client: Any,
        authenticated_client: TestClient,
    ) -> None:
        fake = FakeBatchAria2Client()
        mock_get_client.return_value = fake
        tasks = [
            {"uri": _magnet(format(index, "040x"))} for index in range(31)
        ]
        response = authenticated_client.post("/api/tasks", json={"tasks": tasks})
        assert response.status_code == 422
        assert "30" in response.json()["detail"]
        assert fake.calls == []

    @patch("app.services.task_service._get_client")
    def test_raw_31_with_duplicate_within_limit_allowed(
        self,
        mock_get_client: Any,
        authenticated_client: TestClient,
    ) -> None:
        fake = FakeBatchAria2Client()
        mock_get_client.return_value = fake
        tasks = [{"uri": _magnet(format(index, "040x"))} for index in range(30)]
        tasks.append({"uri": "  " + _magnet(format(0, "040x")) + "  "})
        response = authenticated_client.post("/api/tasks", json={"tasks": tasks})
        assert response.status_code == 200
        assert len(response.json()["results"]) == 30

    def test_structural_errors_use_pydantic_validation(
        self, authenticated_client: TestClient
    ) -> None:
        response = authenticated_client.post(
            "/api/tasks", json={"tasks": [{"uri": _magnet("0" * 40), "extra": 1}]}
        )
        assert response.status_code == 422
        assert isinstance(response.json()["detail"], list)

        response = authenticated_client.post(
            "/api/tasks",
            json={"tasks": [{"uri": _magnet("0" * 40), "options": None}]},
        )
        assert response.status_code == 422

        response = authenticated_client.post("/api/tasks", json={"tasks": [{}]})
        assert response.status_code == 422

    def test_legacy_object_body_rejected(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": _magnet("2" * 40)},
        )
        assert response.status_code == 422
        assert isinstance(response.json()["detail"], list)

    def test_mixed_uri_and_tasks_rejected(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": _magnet("2" * 40), "tasks": [{"uri": _magnet("3" * 40)}]},
        )
        assert response.status_code == 422
        assert isinstance(response.json()["detail"], list)

    @patch("app.routers.tasks.ensure_authenticated_allowed")
    @patch("app.services.task_service._get_client")
    def test_per_item_rate_limit_reported_in_results(
        self,
        mock_get_client: Any,
        mock_allowed: AsyncMock,
        authenticated_client: TestClient,
    ) -> None:
        fake = FakeBatchAria2Client()
        mock_get_client.return_value = fake

        calls: list[int] = []

        async def allow(*args: Any, **kwargs: Any) -> None:
            calls.append(1)
            if len(calls) >= 2:
                raise HTTPException(429, "操作过于频繁，请稍后再试")

        mock_allowed.side_effect = allow
        response = authenticated_client.post(
            "/api/tasks",
            json={"tasks": [{"uri": _magnet("3" * 40)}, {"uri": _magnet("4" * 40)}]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["accepted_count"] == 1
        assert body["failed_count"] == 1
        failed = body["results"][1]
        assert failed["accepted"] is False
        assert failed["error"] == "操作过于频繁，请稍后再试"
        assert failed["task_id"] is None
        assert len(fake.calls) == 1

    def test_undetermined_maps_to_502(
        self, authenticated_client: TestClient
    ) -> None:
        with patch(
            "app.services.task_service.create_tasks_batch",
            side_effect=BatchSubmissionUndeterminedError("undetermined"),
        ):
            response = authenticated_client.post(
                "/api/tasks", json={"tasks": [{"uri": _magnet("5" * 40)}]}
            )
        assert response.status_code == 502
        assert response.json()["detail"]

    def test_status_mapped_via_legacy_rest_status(
        self, authenticated_client: TestClient
    ) -> None:
        crafted = BatchCreateResult(
            results=[
                BatchTaskItemResult(
                    input_index=0,
                    accepted=True,
                    task_id=1,
                    status="completed",
                ),
                BatchTaskItemResult(
                    input_index=1,
                    accepted=False,
                    error_code="invalid_uri",
                    error_message="无效的磁力链接",
                ),
            ]
        )
        with patch(
            "app.services.task_service.create_tasks_batch",
            return_value=crafted,
        ):
            response = authenticated_client.post(
                "/api/tasks",
                json={"tasks": [{"uri": _magnet("6" * 40)}, {"uri": "ftp://x"}]},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["accepted_count"] == 1
        assert body["failed_count"] == 1
        assert body["results"][0]["status"] == "complete"
        assert body["results"][1]["status"] is None
        assert body["results"][1]["error"] == "无效的磁力链接"


class TestGeneratedOpenAPIContract:
    def _resolve(self, app: Any, schema: dict) -> dict:
        if "$ref" in schema:
            name = schema["$ref"].split("/")[-1]
            return app.openapi()["components"]["schemas"][name]
        return schema

    def test_post_tasks_openapi_matches_delivered_contract(self) -> None:
        from app.main import app

        spec = app.openapi()
        post = spec["paths"]["/api/tasks"]["post"]
        assert post["operationId"] == "createTasks"

        assert set(post["responses"]) == {"200", "401", "422", "429", "502"}

        request_schema = self._resolve(
            app, post["requestBody"]["content"]["application/json"]["schema"]
        )
        assert request_schema.get("additionalProperties") is False
        assert set(request_schema["required"]) == {"tasks"}
        item_schema = self._resolve(app, request_schema["properties"]["tasks"]["items"])
        assert item_schema.get("additionalProperties") is False
        assert set(item_schema["required"]) == {"uri"}
        options_schema = item_schema["properties"]["options"]
        # options 可省略但不可为 null：不允许 anyOf null
        assert "anyOf" not in options_schema
        assert options_schema.get("type") == "object"

        response_schema = self._resolve(
            app, post["responses"]["200"]["content"]["application/json"]["schema"]
        )
        assert response_schema.get("additionalProperties") is False
        assert set(response_schema["required"]) == {"accepted_count", "failed_count", "results"}
        result_schema = self._resolve(app, response_schema["properties"]["results"]["items"])
        assert result_schema.get("additionalProperties") is False
        assert set(result_schema["required"]) == {
            "input_index",
            "accepted",
            "task_id",
            "status",
            "error",
        }
        properties = result_schema["properties"]

        def _nullable_types(schema: dict) -> set[str]:
            if "anyOf" in schema:
                types: set[str] = set()
                for branch in schema["anyOf"]:
                    types.update(_nullable_types(branch))
                return types
            value = schema.get("type")
            if isinstance(value, list):
                return set(value)
            return {value}

        assert _nullable_types(properties["task_id"]) == {"integer", "null"}
        assert _nullable_types(properties["error"]) == {"string", "null"}
        status_schema = properties["status"]
        assert "null" in _nullable_types(status_schema)
        enum_schema = status_schema
        while "enum" not in enum_schema:
            enum_schema = next(
                branch for branch in enum_schema.get("anyOf", []) if "enum" in branch
            )
        assert enum_schema["enum"] == [
            "queued",
            "active",
            "waiting",
            "paused",
            "complete",
            "error",
        ]

    def test_openapi_security_cookie_or_bearer(self) -> None:
        from app.main import app

        spec = app.openapi()
        schemes = spec["components"]["securitySchemes"]
        assert schemes["sessionCookie"] == {
            "type": "apiKey",
            "in": "cookie",
            "name": "aria2_session",
        }
        assert schemes["apiToken"]["type"] == "http"
        assert schemes["apiToken"]["scheme"] == "bearer"
        assert set(schemes) == {"sessionCookie", "apiToken"}

        post = spec["paths"]["/api/tasks"]["post"]
        assert post["security"] == [{"sessionCookie": []}, {"apiToken": []}]

        login = spec["paths"]["/api/auth/login"]["post"]
        assert not login.get("security")

    def test_openapi_error_schemas_closed(self) -> None:
        from app.main import app

        spec = app.openapi()
        post = spec["paths"]["/api/tasks"]["post"]
        for code in ("401", "429", "502"):
            schema = self._resolve(
                app, post["responses"][code]["content"]["application/json"]["schema"]
            )
            assert schema.get("additionalProperties") is False, code
            assert "detail" in schema["properties"]


class TestPathGates:
    """M24 Task5 静态路径门禁：旧路径退场、无 schema 变更。"""

    ROOT = Path(__file__).resolve().parents[2]

    def test_router_has_no_legacy_object_model_or_branch(self) -> None:
        source = (self.ROOT / "backend/app/routers/tasks.py").read_text()
        assert "class TaskCreate(" not in source
        assert "task_service.create_task(" not in source

    def test_schema_version_unchanged(self) -> None:
        source = (self.ROOT / "backend/app/db/schema.py").read_text()
        assert "SCHEMA_VERSION = 17" in source

    def test_no_task_submission_manager_abstraction(self) -> None:
        for path in (self.ROOT / "backend/app").rglob("*.py"):
            assert "TaskSubmissionManager" not in path.read_text(), path

    def test_frontend_has_no_legacy_create_task_call(self) -> None:
        api_source = (self.ROOT / "frontend/lib/api.ts").read_text()
        assert re.search(r"createTask(?!s)", api_source) is None
        assert "createTasks" in api_source
        page_source = (
            self.ROOT / "frontend/app/(authenticated)/tasks/page.tsx"
        ).read_text()
        assert "BATCH_TASK_CONCURRENCY" not in page_source
        assert "new AbortController" not in page_source


class TestBatchAllowanceNon429Rethrow:
    """M24：allowance 限流回调中非 429 HTTPException 必须原样重抛。"""

    def test_non_429_allowance_exception_propagates(
        self, authenticated_client: TestClient
    ) -> None:
        with patch(
            "app.routers.tasks.ensure_authenticated_allowed",
            new=AsyncMock(side_effect=HTTPException(status_code=403, detail="无权创建任务")),
        ):
            response = authenticated_client.post(
                "/api/tasks", json={"tasks": [{"uri": _magnet("3" * 40)}]}
            )
        assert response.status_code == 403
        assert response.json()["detail"] == "无权创建任务"

    def test_429_allowance_maps_to_item_failure(
        self, authenticated_client: TestClient
    ) -> None:
        with patch(
            "app.routers.tasks.ensure_authenticated_allowed",
            new=AsyncMock(side_effect=HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")),
        ):
            response = authenticated_client.post(
                "/api/tasks", json={"tasks": [{"uri": _magnet("4" * 40)}]}
            )
        assert response.status_code == 200
        body = response.json()
        assert body["accepted_count"] == 0
        assert body["failed_count"] == 1
        assert "操作过于频繁" in body["results"][0]["error"]
