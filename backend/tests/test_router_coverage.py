"""Coverage tests for config/tasks/users/storage router edge paths and main.py."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db.engine import transaction
from app.domain.errors import NotFoundError
from app.routers import config as config_router
from app.services import user_service


# ---------------------------------------------------------------------------
# config router
# ---------------------------------------------------------------------------


def test_public_site_info(client: TestClient, temp_db: str) -> None:
    response = client.get("/api/config/public/site-info")
    assert response.status_code == 200
    assert "site_title" in response.json()


def test_aria2_test_requires_admin_id(temp_db: str) -> None:
    with pytest.raises(Exception) as exc_info:
        asyncio.run(
            config_router.test_aria2_connection(
                config_router.Aria2TestRequest(
                    aria2_rpc_url="http://127.0.0.1:6800/jsonrpc"
                ),
                SimpleNamespace(id=None),
            )
        )
    assert exc_info.value.status_code == 401


def test_aria2_test_rate_limited_logs_warning(
    client: TestClient, admin_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.cookies.set(settings.session_cookie_name, admin_session)
    from app.core.rate_limit_config import rate_limit_config

    original = rate_limit_config.aria2_test
    rate_limit_config.aria2_test = 1
    try:
        first = client.post(
            "/api/config/aria2/test",
            json={"aria2_rpc_url": "http://127.0.0.1:6800/jsonrpc"},
        )
        second = client.post(
            "/api/config/aria2/test",
            json={"aria2_rpc_url": "http://127.0.0.1:6800/jsonrpc"},
        )
    finally:
        rate_limit_config.aria2_test = original
    assert first.status_code == 200
    assert second.status_code == 429


# ---------------------------------------------------------------------------
# tasks router
# ---------------------------------------------------------------------------


def test_v2_tasks_rejects_unknown_status_filter(authenticated_client: TestClient) -> None:
    response = authenticated_client.get("/api/v2/tasks", params={"status_filter": "bogus"})
    assert response.status_code == 400


def test_retry_failed_task_endpoint(
    authenticated_client: TestClient, failed_task: dict
) -> None:
    from tests.fakes import make_aria2_client

    probe = SimpleNamespace(success=True, final_url="https://example.com/file.zip",
                            content_length=1000, filename="file.zip")
    with (
        patch(
            "app.services.task_service._get_client",
            return_value=make_aria2_client(add_uri="gid-retry-router"),
        ),
        patch(
            "app.services.task_service.probe_url_with_get_fallback",
            new=AsyncMock(return_value=probe),
        ),
        patch(
            "app.core.security.socket.getaddrinfo",
            return_value=[(2, 1, 0, "", ("93.184.216.34", 80))],
        ),
    ):
        response = authenticated_client.post(f"/api/tasks/{failed_task['id']}/retry")
    assert response.status_code == 201


def test_retry_missing_task_endpoint(authenticated_client: TestClient) -> None:
    response = authenticated_client.post("/api/tasks/999999/retry")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# users router
# ---------------------------------------------------------------------------


def test_rpc_access_status_domain_error(
    authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def raising(user_id: int):
        raise NotFoundError("用户不存在")

    monkeypatch.setattr(user_service, "get_rpc_access", raising)
    response = authenticated_client.get("/api/users/me/rpc-access")
    assert response.status_code == 404


def test_rpc_access_toggle_domain_error(
    authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def raising(*, user_id: int, enabled: bool, request_id: str):
        raise NotFoundError("用户不存在")

    monkeypatch.setattr(user_service, "set_rpc_access", raising)
    response = authenticated_client.put(
        "/api/users/me/rpc-access", json={"enabled": True}
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# storage router
# ---------------------------------------------------------------------------


def test_storage_file_users_not_found(
    client: TestClient, admin_session: str
) -> None:
    client.cookies.set(settings.session_cookie_name, admin_session)
    response = client.get("/api/admin/storage/files/999999/users")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# storage router endpoints
# ---------------------------------------------------------------------------


def _admin_client(client: TestClient, admin_session: str) -> TestClient:
    client.cookies.set(settings.session_cookie_name, admin_session)
    return client


def test_storage_list_files_endpoint(
    client: TestClient, admin_session: str, user_file: dict
) -> None:
    client = _admin_client(client, admin_session)
    response = client.get("/api/admin/storage/files", params={"page": 1, "page_size": 10})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    assert body["files"][0]["content_hash"] == user_file["content_hash"]


def test_storage_bulk_delete_endpoint(
    client: TestClient, admin_session: str, user_file: dict
) -> None:
    client = _admin_client(client, admin_session)
    response = client.request(
        "DELETE",
        "/api/admin/storage/files",
        json={"file_ids": [user_file["stored_file_id"]]},
    )
    assert response.status_code in (200, 202)
    assert response.json()["accepted_count"] == 0  # still referenced


def test_storage_scan_andRepair_not_implemented(
    client: TestClient, admin_session: str
) -> None:
    client = _admin_client(client, admin_session)
    assert client.post("/api/admin/storage/scan").status_code == 501
    assert client.post("/api/admin/storage/repair").status_code == 501


# ---------------------------------------------------------------------------
# config router: trackers refresh & credentials invalidate
# ---------------------------------------------------------------------------


def test_refresh_trackers_endpoint(client: TestClient, admin_session: str) -> None:
    client = _admin_client(client, admin_session)
    response = client.post("/api/config/trackers/refresh")
    assert response.status_code == 200
    assert "entry_count" in response.json()


def test_invalidate_credentials_requires_confirmation(
    client: TestClient, admin_session: str
) -> None:
    client = _admin_client(client, admin_session)
    response = client.post(
        "/api/config/credentials/invalidate", json={"confirm": "wrong"}
    )
    assert response.status_code == 400

    response = client.post(
        "/api/config/credentials/invalidate",
        json={"confirm": "INVALIDATE_ALL_CREDENTIALS"},
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# tasks router: batch cancel validation
# ---------------------------------------------------------------------------


def test_cancel_tasks_rejects_empty_and_oversized_batches(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.post("/api/tasks/cancel", json={"task_ids": []})
    assert response.status_code == 422
    assert response.json()["detail"] == "至少选择一个条目"

    response = authenticated_client.post(
        "/api/tasks/cancel", json={"task_ids": list(range(1001))}
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "一次最多操作 1000 个条目"


def test_storage_bulk_delete_accepts_orphan(
    client: TestClient, admin_session: str, user_file: dict, test_user: dict
) -> None:
    from app.db.schema import user_files

    async def detach() -> None:
        async with transaction() as conn:
            await conn.execute(user_files.delete())

    asyncio.run(detach())
    client = _admin_client(client, admin_session)
    response = client.request(
        "DELETE",
        "/api/admin/storage/files",
        json={"file_ids": [user_file["stored_file_id"]]},
    )
    assert response.status_code == 202
    assert response.json()["accepted_count"] == 1
