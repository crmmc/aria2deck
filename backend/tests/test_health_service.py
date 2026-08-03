import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import health as health_router
from app.services.health_service import readiness_errors


async def _database_ready() -> bool:
    return True


def _worker(done: bool = False) -> SimpleNamespace:
    return SimpleNamespace(done=lambda: done)


class _NoAria2Access:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"readiness must not access aria2.{name}")


def _ready_client() -> tuple[TestClient, FastAPI]:
    app = FastAPI()
    app.include_router(health_router.router)
    app.state.database_ready = _database_ready
    app.state.background_workers = {
        name: {"task": _worker(), "error": None}
        for name in ("sync", "listener", "deletion", "pack")
    }
    app.state.aria2_client = _NoAria2Access()
    return TestClient(app), app


def test_ready_uses_local_probes_without_aria2(temp_db: str) -> None:
    client, app = _ready_client()

    response = client.get("/api/health/ready")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert app.state.background_workers["sync"]["status"] == "running"


def test_readiness_handles_missing_or_none_application_state(temp_db: str) -> None:
    none_state = SimpleNamespace(state=None)
    invalid_state = SimpleNamespace(
        state=SimpleNamespace(background_workers=None, database_ready=None)
    )

    for application in (object(), none_state, invalid_state):
        errors = asyncio.run(readiness_errors(application))

        assert "sync 未启动" in errors
        assert "listener 未启动" in errors
        assert "deletion 未启动" in errors
        assert "pack 未启动" in errors
        assert "数据库不可用" in errors
