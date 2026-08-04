"""Tests for GET /api/system/status."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services import backend_connectivity as connectivity


@pytest.fixture
def admin_client(client: TestClient, admin_session: str) -> TestClient:
    client.cookies.set(settings.session_cookie_name, admin_session)
    return client


@pytest.fixture(autouse=True)
def _reset_connectivity_state() -> None:
    connectivity.reset_for_tests()
    yield
    connectivity.reset_for_tests()


def test_system_status_requires_auth(client: TestClient) -> None:
    response = client.get("/api/system/status")
    assert response.status_code == 401


def test_system_status_user_ok_payload(authenticated_client: TestClient) -> None:
    response = authenticated_client.get("/api/system/status")
    assert response.status_code == 200
    assert response.json() == {
        "download_backend": {
            "status": "ok",
            "message": connectivity.USER_OK_MESSAGE,
        }
    }


def test_system_status_admin_ok_payload(admin_client: TestClient) -> None:
    response = admin_client.get("/api/system/status")
    assert response.status_code == 200
    assert response.json() == {
        "download_backend": {
            "status": "ok",
            "message": connectivity.ADMIN_OK_MESSAGE,
        }
    }


@pytest.mark.asyncio
async def test_system_status_degraded_role_messages(
    client: TestClient,
    user_session: str,
    admin_session: str,
) -> None:
    await connectivity.mark_fail()
    await connectivity.mark_fail()

    user_client = TestClient(client.app)
    user_client.cookies.set(settings.session_cookie_name, user_session)
    admin_only_client = TestClient(client.app)
    admin_only_client.cookies.set(settings.session_cookie_name, admin_session)

    user_response = user_client.get("/api/system/status")
    admin_response = admin_only_client.get("/api/system/status")

    assert user_response.status_code == 200
    assert user_response.json()["download_backend"] == {
        "status": "degraded",
        "message": connectivity.USER_DEGRADED_MESSAGE,
    }
    assert admin_response.status_code == 200
    assert admin_response.json()["download_backend"] == {
        "status": "degraded",
        "message": connectivity.ADMIN_DEGRADED_MESSAGE,
    }

    # Contract: no extra diagnostic keys for either role.
    assert set(user_response.json()["download_backend"].keys()) == {"status", "message"}
    assert set(admin_response.json()["download_backend"].keys()) == {"status", "message"}
