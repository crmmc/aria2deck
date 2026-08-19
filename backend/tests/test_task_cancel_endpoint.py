"""Tests for v0 task cancellation endpoint."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.repositories.task.user_tasks import get_user_task
from app.repositories.task.downloads import get_global_by_resource_key
from app.services.usage_service import get_usage
from tests.create_task_helper import create_download_task, global_download_id_of
from tests.fakes import make_aria2_client


def _create_download_for_user(
    *,
    user: dict,
    resource_key: str,
    uri: str,
    display_name: str,
    total_bytes: int,
    gid: str,
) -> tuple[dict, AsyncMock]:
    client = make_aria2_client(add_uri=gid)
    task = asyncio.run(
        create_download_task(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            uri=uri,
            resource_key=resource_key,
            resource_kind="http",
            display_name=display_name,
            total_bytes=total_bytes,
            aria2_client=client,
        )
    )
    return task, client


class TestCancelTaskEndpoint:
    def test_cancel_active_v0_task_marks_user_task_cancelled(
        self,
        authenticated_client: TestClient,
        test_user: dict,
    ) -> None:
        task, _ = _create_download_for_user(
            user=test_user,
            resource_key="http:cancel-endpoint",
            uri="https://example.com/cancel-endpoint.bin",
            display_name="cancel-endpoint.bin",
            total_bytes=700,
            gid="gid-cancel-endpoint",
        )
        # BackendPort.remove uses client.remove (not force_remove).
        cancel_client = make_aria2_client(remove="gid-cancel-endpoint")

        with patch(
            "app.services.task_service._get_client", return_value=cancel_client
        ):
            response = authenticated_client.post(
                "/api/tasks/cancel", json={"task_ids": [task["id"]]}
            )

        assert response.status_code == 200
        assert response.json() == {
            "accepted_count": 1,
            "failed_count": 0,
            "results": [
                {
                    "task_id": task["id"],
                    "ok": True,
                    "state": "cancelled",
                    "accepted": True,
                    "error": None,
                }
            ],
        }

        global_download = asyncio.run(
            get_global_by_resource_key("http:cancel-endpoint")
        )
        stored_task = asyncio.run(
            get_user_task(test_user["id"], global_download_id_of(task))
        )
        usage = asyncio.run(
            get_usage(test_user["id"], quota_bytes=test_user["quota_bytes"])
        )

        assert stored_task is not None
        assert stored_task["status"] == "cancelled"
        assert stored_task["reserved_bytes"] == 0
        assert stored_task["finished_at_ms"] is not None
        assert usage["reserved_bytes"] == 0
        assert global_download is not None
        assert global_download["status"] == "cancelled"
        assert global_download["aria2_gid"] is None
        cancel_client.remove.assert_awaited_once_with("gid-cancel-endpoint")

    def test_cancel_one_users_shared_task_keeps_global_download_active(
        self,
        authenticated_client: TestClient,
        test_user: dict,
        test_admin: dict,
    ) -> None:
        setup_client = make_aria2_client(add_uri="gid-shared-endpoint")
        first = asyncio.run(
            create_download_task(
                user_id=test_user["id"],
                quota_bytes=test_user["quota_bytes"],
                uri="https://example.com/shared-endpoint.bin",
                resource_key="http:shared-endpoint",
                resource_kind="http",
                display_name="shared-endpoint.bin",
                total_bytes=900,
                aria2_client=setup_client,
            )
        )
        second = asyncio.run(
            create_download_task(
                user_id=test_admin["id"],
                quota_bytes=test_admin["quota_bytes"],
                uri="https://example.com/shared-endpoint.bin",
                resource_key="http:shared-endpoint",
                resource_kind="http",
                display_name="shared-endpoint.bin",
                total_bytes=900,
                aria2_client=setup_client,
            )
        )
        cancel_client = make_aria2_client()

        with patch(
            "app.services.task_service._get_client", return_value=cancel_client
        ):
            response = authenticated_client.post(
                "/api/tasks/cancel", json={"task_ids": [first["id"]]}
            )

        assert response.status_code == 200
        assert response.json()["accepted_count"] == 1
        assert response.json()["failed_count"] == 0

        global_download = asyncio.run(
            get_global_by_resource_key("http:shared-endpoint")
        )
        first_task = asyncio.run(
            get_user_task(test_user["id"], global_download_id_of(first))
        )
        second_task = asyncio.run(
            get_user_task(test_admin["id"], global_download_id_of(second))
        )

        assert first_task is not None
        assert first_task["status"] == "cancelled"
        assert second_task is not None
        assert second_task["status"] == "active"
        assert global_download is not None
        assert global_download["status"] == "active"
        assert global_download["aria2_gid"] == "gid-shared-endpoint"
        cancel_client.remove.assert_not_awaited()

    def test_cancel_missing_v0_task_reports_item_failure(
        self,
        authenticated_client: TestClient,
    ) -> None:
        response = authenticated_client.post(
            "/api/tasks/cancel", json={"task_ids": [99999]}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["accepted_count"] == 0
        assert data["failed_count"] == 1
        assert data["results"][0]["error"] == "任务不存在"

    def test_cancel_backend_remove_failure_still_cancels_task(
        self,
        authenticated_client: TestClient,
        test_user: dict,
    ) -> None:
        """Cleanup RPC failure does not block cancel; task is already cancelled."""
        task, _ = _create_download_for_user(
            user=test_user,
            resource_key="http:cancel-endpoint-failure",
            uri="https://example.com/cancel-endpoint-failure.bin",
            display_name="cancel-endpoint-failure.bin",
            total_bytes=600,
            gid="gid-cancel-endpoint-failure",
        )
        cancel_client = make_aria2_client(remove=OSError("aria2 timeout"))

        with patch(
            "app.services.task_service._get_client", return_value=cancel_client
        ):
            response = authenticated_client.post(
                "/api/tasks/cancel", json={"task_ids": [task["id"]]}
            )

        assert response.status_code == 200
        data = response.json()
        assert data["accepted_count"] == 1
        assert data["failed_count"] == 0
        assert data["results"][0]["ok"] is True

        stored_task = asyncio.run(
            get_user_task(test_user["id"], global_download_id_of(task))
        )
        usage = asyncio.run(
            get_usage(test_user["id"], quota_bytes=test_user["quota_bytes"])
        )

        assert stored_task is not None
        assert stored_task["status"] == "cancelled"
        assert stored_task["reserved_bytes"] == 0
        assert usage["reserved_bytes"] == 0
        # remove may be tried; remove_download_result fallback also possible
        assert (
            cancel_client.remove.await_count
            + cancel_client.remove_download_result.await_count
        ) >= 1


class TestCancelTasksBatchEndpoint:
    def test_cancel_tasks_batch_success_retains_records(
        self,
        authenticated_client: TestClient,
        test_user: dict,
    ) -> None:
        """批量取消成功：响应受理语义，任务记录保留并终态化（AC-7）。"""
        task_a, _ = _create_download_for_user(
            user=test_user,
            resource_key="http:batch-cancel-a",
            uri="https://example.com/batch-cancel-a.bin",
            display_name="batch-cancel-a.bin",
            total_bytes=700,
            gid="gid-batch-cancel-a",
        )
        task_b, _ = _create_download_for_user(
            user=test_user,
            resource_key="http:batch-cancel-b",
            uri="https://example.com/batch-cancel-b.bin",
            display_name="batch-cancel-b.bin",
            total_bytes=800,
            gid="gid-batch-cancel-b",
        )
        cancel_client = make_aria2_client()

        with patch(
            "app.services.task_service._get_client", return_value=cancel_client
        ):
            response = authenticated_client.post(
                "/api/tasks/cancel",
                json={"task_ids": [task_a["id"], task_b["id"]]},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["accepted_count"] == 2
        assert data["failed_count"] == 0
        assert data["results"] == [
            {
                "task_id": task_a["id"],
                "ok": True,
                "state": "cancelled",
                "accepted": True,
                "error": None,
            },
            {
                "task_id": task_b["id"],
                "ok": True,
                "state": "cancelled",
                "accepted": True,
                "error": None,
            },
        ]

        # AC-7：取消 ≠ 删除，任务记录保留且状态终态化，不触发文件删除。
        for task in (task_a, task_b):
            stored_task = asyncio.run(
                get_user_task(test_user["id"], global_download_id_of(task))
            )
            assert stored_task is not None
            assert stored_task["status"] == "cancelled"
            assert stored_task["reserved_bytes"] == 0
            assert stored_task["finished_at_ms"] is not None
            global_download = asyncio.run(
                get_global_by_resource_key(
                    "http:batch-cancel-a"
                    if task is task_a
                    else "http:batch-cancel-b"
                )
            )
            assert global_download is not None
            assert global_download["status"] == "cancelled"
            assert global_download["aria2_gid"] is None
        usage = asyncio.run(
            get_usage(test_user["id"], quota_bytes=test_user["quota_bytes"])
        )
        assert usage["reserved_bytes"] == 0
        assert cancel_client.remove.await_count == 2

    def test_cancel_tasks_partial_failure_returns_chinese_error(
        self,
        authenticated_client: TestClient,
        test_user: dict,
    ) -> None:
        task, _ = _create_download_for_user(
            user=test_user,
            resource_key="http:batch-partial",
            uri="https://example.com/batch-partial.bin",
            display_name="batch-partial.bin",
            total_bytes=500,
            gid="gid-batch-partial",
        )
        cancel_client = make_aria2_client()

        with patch(
            "app.services.task_service._get_client", return_value=cancel_client
        ):
            response = authenticated_client.post(
                "/api/tasks/cancel",
                json={"task_ids": [task["id"], 99999]},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["accepted_count"] == 1
        assert data["failed_count"] == 1
        by_id = {item["task_id"]: item for item in data["results"]}
        assert by_id[task["id"]]["ok"] is True
        assert by_id[99999]["ok"] is False
        assert by_id[99999]["state"] == "failed"
        assert by_id[99999]["accepted"] is False
        assert by_id[99999]["error"] == "任务不存在"

        stored_task = asyncio.run(
            get_user_task(test_user["id"], global_download_id_of(task))
        )
        assert stored_task is not None
        assert stored_task["status"] == "cancelled"

    def test_cancel_tasks_deduplicates_ids(
        self,
        authenticated_client: TestClient,
        test_user: dict,
    ) -> None:
        task, _ = _create_download_for_user(
            user=test_user,
            resource_key="http:batch-dedup",
            uri="https://example.com/batch-dedup.bin",
            display_name="batch-dedup.bin",
            total_bytes=300,
            gid="gid-batch-dedup",
        )
        cancel_client = make_aria2_client()

        with patch(
            "app.services.task_service._get_client", return_value=cancel_client
        ):
            response = authenticated_client.post(
                "/api/tasks/cancel",
                json={"task_ids": [task["id"]] * 3},
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) == 1
        assert data["accepted_count"] == 1
        assert data["failed_count"] == 0
        assert cancel_client.remove.await_count == 1

    def test_cancel_tasks_empty_list_rejected(
        self,
        authenticated_client: TestClient,
    ) -> None:
        response = authenticated_client.post("/api/tasks/cancel", json={"task_ids": []})

        assert response.status_code == 422
        assert response.json()["detail"] == "至少选择一个条目"

    def test_cancel_tasks_over_limit_rejected(
        self,
        authenticated_client: TestClient,
    ) -> None:
        response = authenticated_client.post(
            "/api/tasks/cancel",
            json={"task_ids": list(range(1001))},
        )

        assert response.status_code == 422
        assert response.json()["detail"] == "一次最多操作 1000 个条目"

    def test_cancel_tasks_unauthorized(self, client: TestClient) -> None:
        response = client.post("/api/tasks/cancel", json={"task_ids": [1]})

        assert response.status_code == 401

    def test_cancel_tasks_consumes_single_authenticated_api_unit(
        self,
        authenticated_client: TestClient,
        test_user: dict,
    ) -> None:
        task, _ = _create_download_for_user(
            user=test_user,
            resource_key="http:batch-ratelimit",
            uri="https://example.com/batch-ratelimit.bin",
            display_name="batch-ratelimit.bin",
            total_bytes=400,
            gid="gid-batch-ratelimit",
        )

        from app.core.rate_limit import api_limiter
        from app.core.rate_limit_config import rate_limit_config

        asyncio.run(api_limiter.clear_all())
        original_limit = rate_limit_config.authenticated_api
        rate_limit_config.authenticated_api = 2
        try:
            with patch(
                "app.services.task_service._get_client",
                return_value=make_aria2_client(),
            ):
                response = authenticated_client.post(
                    "/api/tasks/cancel",
                    json={"task_ids": [task["id"], 99999]},
                )
        finally:
            rate_limit_config.authenticated_api = original_limit

        assert response.status_code == 200
        remaining = asyncio.run(
            api_limiter.get_remaining(
                test_user["id"], "authenticated_api", limit=2
            )
        )
        assert remaining == 1

    def test_cancel_tasks_batch_of_100_records_duration(
        self,
        authenticated_client: TestClient,
        test_user: dict,
    ) -> None:
        """spec §5 风险证据：mock aria2 后端下批量取消 100 个任务的耗时。

        仅记录耗时（stdout 打印 perf-evidence 行），不设硬阈值断言。
        实测证据（M13 Task 4, 本机 darwin）: 见报告中 TDD Evidence 一节。
        """
        import time

        tasks = []
        for index in range(100):
            task, _ = _create_download_for_user(
                user=test_user,
                resource_key=f"http:bulk100-{index}",
                uri=f"https://example.com/bulk100-{index}.bin",
                display_name=f"bulk100-{index}.bin",
                total_bytes=100,
                gid=f"gid-bulk100-{index}",
            )
            tasks.append(task)
        cancel_client = make_aria2_client()

        with patch(
            "app.services.task_service._get_client", return_value=cancel_client
        ):
            started = time.perf_counter()
            response = authenticated_client.post(
                "/api/tasks/cancel",
                json={"task_ids": [task["id"] for task in tasks]},
            )
            elapsed = time.perf_counter() - started

        assert response.status_code == 200
        data = response.json()
        assert data["accepted_count"] == 100
        assert data["failed_count"] == 0
        assert cancel_client.remove.await_count == 100
        print(f"\n[perf-evidence] POST /api/tasks/cancel x100 elapsed: {elapsed:.3f}s")
