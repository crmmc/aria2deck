import pytest
from fastapi.testclient import TestClient

from app.core.config import settings


@pytest.fixture
def admin_client(client: TestClient, admin_session: str) -> TestClient:
    client.cookies.set(settings.session_cookie_name, admin_session)
    return client
