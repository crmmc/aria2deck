from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from aria2deck.api.health import ready
from aria2deck.bootstrap import _database_is_ready, create_app
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.instance_lock import InstanceLock
from aria2deck.infrastructure.sqlite.bootstrap import initialize_database
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "data" / "v2.db",
        download_directory=tmp_path / "downloads",
        static_directory=tmp_path / "static",
        debug=True,
    )


@pytest.mark.parametrize("path", ["/api/health", "/api/health/live", "/api/health/ready"])
def test_health_endpoints(tmp_path: Path, path: str) -> None:
    with TestClient(create_app(settings=_settings(tmp_path))) as client:
        response = client.get(path)

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_lifespan_releases_the_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[SQLiteDatabase] = []
    original_close = SQLiteDatabase.close

    async def track_close(database: SQLiteDatabase) -> None:
        closed.append(database)
        await original_close(database)

    monkeypatch.setattr(SQLiteDatabase, "close", track_close)
    settings = _settings(tmp_path)
    with TestClient(create_app(settings=settings)) as client:
        assert client.get("/api/health/ready").status_code == 200
        assert settings.database_path.is_file()

    assert [database.path for database in closed] == [settings.database_path]


def test_structural_drift_blocks_application_startup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = SQLiteDatabase(settings.database_path)

    async def create_drift() -> None:
        await initialize_database(database)
        async with database.connection() as connection:
            await connection.execute(text("DROP INDEX uq_download_attempts_live_resource"))
            await connection.commit()
        await database.close()

    asyncio.run(create_drift())
    with pytest.raises(DomainError) as caught:
        with TestClient(create_app(settings=settings)) as client:
            assert client.get("/api/health/ready").status_code != 200
    assert caught.value.code is ErrorCode.SCHEMA_MISMATCH
    lock = InstanceLock(settings.instance_lock_path)
    lock.acquire()
    lock.release()


def test_second_application_instance_is_rejected_and_lock_is_released_on_shutdown(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first = create_app(settings=settings)
    second = create_app(settings=settings)
    with TestClient(first) as client:
        with pytest.raises(DomainError) as caught:
            with TestClient(second):
                pass
        assert caught.value.code is ErrorCode.INVALID_STATE
        assert client.get("/api/health/ready").status_code == 200

    with TestClient(create_app(settings=settings)) as restarted:
        assert restarted.get("/api/health/ready").status_code == 200


def test_corrupt_database_blocks_application_startup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.database_path.parent.mkdir(parents=True)
    settings.database_path.write_bytes(b"not a SQLite database")

    with pytest.raises(SQLAlchemyError):
        with TestClient(create_app(settings=settings)):
            pass


def test_runtime_schema_drift_makes_ready_fail_but_live_stays_up(tmp_path: Path) -> None:
    async def exercise() -> None:
        settings = _settings(tmp_path)
        app = create_app(settings=settings)
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                initial = await client.get("/api/health/ready")
                database = SQLiteDatabase(settings.database_path)
                async with database.connection() as connection:
                    await connection.execute(text("DROP INDEX uq_download_attempts_live_resource"))
                    await connection.commit()
                await database.close()
                ready = await client.get("/api/health/ready")
                live = await client.get("/api/health/live")

        assert initial.status_code == 200
        assert initial.json() == {"ok": True}
        assert ready.status_code == 503
        assert ready.json() == {"ok": False, "errors": ["数据库不可用"]}
        assert live.status_code == 200
        assert live.json() == {"ok": True}
        assert app.state.readiness_check is None

    asyncio.run(exercise())


def test_database_readiness_check_accepts_valid_schema_and_rejects_drift(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        database = SQLiteDatabase(_settings(tmp_path).database_path)
        await initialize_database(database)
        assert await _database_is_ready(database)
        async with database.connection() as connection:
            await connection.execute(text("DROP INDEX uq_download_attempts_live_resource"))
            await connection.commit()
        assert not await _database_is_ready(database)
        await database.close()

    asyncio.run(exercise())


def test_ready_uses_the_registered_readiness_check() -> None:
    app = FastAPI()

    async def check() -> bool:
        return True

    app.state.readiness_check = check
    request = Request(
        {"type": "http", "app": app, "method": "GET", "path": "/", "headers": []}
    )

    response = asyncio.run(ready(request))

    assert response.status_code == 200
    assert response.body == b'{"ok":true}'


def test_ready_before_lifespan_startup_is_unavailable(tmp_path: Path) -> None:
    client = TestClient(create_app(settings=_settings(tmp_path)))

    response = client.get("/api/health/ready")

    assert response.status_code == 503
    assert response.json() == {"ok": False, "errors": ["数据库不可用"]}


def test_unknown_api_is_not_served_as_a_page(tmp_path: Path) -> None:
    with TestClient(create_app(settings=_settings(tmp_path))) as client:
        response = client.get("/api/not-registered")

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "接口不存在"}}
