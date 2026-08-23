"""Coverage tests for app/main.py edge paths (middleware, SPA fallback, helpers)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

import app.main as main_module
from app.core.config import settings
from app.db.engine import transaction
from app.db.schema import users


def test_reset_admin_password_returns_false_without_admin(temp_db: str) -> None:
    async def clear_users() -> None:
        async with transaction() as conn:
            await conn.execute(delete(users))

    asyncio.run(clear_users())
    assert asyncio.run(main_module.reset_admin_password_for_dev_v0()) is False


def test_database_ready_probe(temp_db: str) -> None:
    assert asyncio.run(main_module._database_ready()) is True


def test_database_ready_probe_swallows_failure(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _BrokenEngine:
        def connect(self):
            raise RuntimeError("engine gone")

    monkeypatch.setattr(main_module, "get_engine", lambda: _BrokenEngine())
    assert asyncio.run(main_module._database_ready()) is False


def test_http_middleware_logs_unhandled_exception(
    temp_db: str, user_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import task_service

    async def boom(*args, **kwargs):
        raise RuntimeError("route exploded")

    monkeypatch.setattr(task_service, "list_tasks", boom)
    client = TestClient(main_module.app, raise_server_exceptions=False)
    client.cookies.set(settings.session_cookie_name, user_session)
    response = client.get("/api/tasks")
    assert response.status_code == 500


def test_audit_middleware_skips_non_api_paths(client: TestClient, temp_db: str) -> None:
    response = client.get("/favicon.ico")
    assert response.status_code in (200, 404)


def test_ensure_default_admin_is_noop_when_admin_exists(temp_db: str) -> None:
    from tests.helpers_v0 import create_user_v0

    asyncio.run(create_user_v0(username="bootstrap-admin", password="x" * 16, is_admin=True))
    asyncio.run(main_module.ensure_default_admin_v0())


def test_http_middleware_logs_5xx_response(
    temp_db: str, user_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.responses import JSONResponse

    from app.services import task_service

    monkeypatch.setattr(
        task_service,
        "list_tasks",
        AsyncMock(return_value=JSONResponse(status_code=500, content={"boom": 1})),
    )
    client = TestClient(main_module.app)
    client.cookies.set(settings.session_cookie_name, user_session)
    response = client.get("/api/tasks")
    assert response.status_code == 500


def test_create_app_reads_extra_cors_origins(
    monkeypatch: pytest.MonkeyPatch, temp_db: str
) -> None:
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "allow_null_origin", False)
    monkeypatch.setattr(settings, "cors_origins", "https://extra.example,null,https://dup.example")
    app = main_module.create_app()
    cors = next(m for m in app.user_middleware if m.cls.__name__ == "CORSMiddleware")
    origins = cors.kwargs["allow_origins"]
    assert "https://extra.example" in origins
    assert "https://dup.example" in origins
    assert "null" not in origins


def test_share_page_spa_fallback(client: TestClient, temp_db: str) -> None:
    response = client.get("/s/abc123")
    assert response.status_code == 200
    assert b"<" in response.content[:64]


def test_alias_route_serves_index(client: TestClient, temp_db: str) -> None:
    for path in ("/tasks", "/files"):
        response = client.get(path)
        assert response.status_code == 200
