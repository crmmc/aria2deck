from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings, settings
from app.main import create_app, redact_url_for_log
from app.routers import health as health_router


async def _database_ready() -> bool:
    return True


def _worker(done: bool = False) -> SimpleNamespace:
    return SimpleNamespace(done=lambda: done)


def _ready_client() -> TestClient:
    app = FastAPI()
    app.include_router(health_router.router)
    app.state.database_ready = _database_ready
    app.state.background_workers = {
        name: {"task": _worker(), "error": None}
        for name in ("sync", "listener", "deletion", "pack")
    }
    return TestClient(app)


def _preflight(app: FastAPI):
    return TestClient(app).options(
        "/aria2/jsonrpc",
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )


def test_settings_reads_explicit_null_origin_flag(monkeypatch):
    monkeypatch.setenv("ARIA2C_ALLOW_NULL_ORIGIN", "true")
    monkeypatch.setenv("ARIA2C_CORS_ORIGINS", "https://frontend.example")

    configured = Settings()

    assert configured.allow_null_origin is True
    assert configured.cors_origins == "https://frontend.example"


def test_null_origin_requires_debug_or_explicit_setting(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "allow_null_origin", False)
    assert _preflight(create_app()).status_code == 400

    monkeypatch.setattr(settings, "allow_null_origin", True)
    assert _preflight(create_app()).headers["access-control-allow-origin"] == "null"

    monkeypatch.setattr(settings, "allow_null_origin", False)
    monkeypatch.setattr(settings, "debug", True)
    assert _preflight(create_app()).headers["access-control-allow-origin"] == "null"


def test_log_url_redaction_discards_query_and_fragment():
    assert (
        redact_url_for_log("https://api.example/api/tasks?token=secret#fragment")
        == "/api/tasks"
    )


def test_live_and_ready_distinguish_worker_failure_and_recovery(temp_db):
    client = _ready_client()

    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/api/health/live").json() == {"ok": True}
    assert client.get("/api/health/ready").status_code == 200

    client.app.state.background_workers["sync"] = {
        "task": _worker(done=True),
        "error": "sync 异常退出: RuntimeError",
    }
    failed = client.get("/api/health/ready")
    assert failed.status_code == 503
    assert "sync 异常退出: RuntimeError" in failed.json()["errors"]

    client.app.state.background_workers["sync"] = {"task": _worker(), "error": None}
    assert client.get("/api/health/ready").status_code == 200


def test_ready_reports_database_failure_without_affecting_liveness(temp_db):
    async def database_failure() -> bool:
        return False

    client = _ready_client()
    client.app.state.database_ready = database_failure

    assert client.get("/api/health/live").status_code == 200
    response = client.get("/api/health/ready")
    assert response.status_code == 503
    assert "数据库不可用" in response.json()["errors"]
