from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria2deck.bootstrap import create_app
from aria2deck.infrastructure.config import Settings

PASSWORD = "ab" * 32
OTHER_PASSWORD = "cd" * 32
THIRD_PASSWORD = "ef" * 32


@pytest.fixture(autouse=True)
def _skip_startup_orphan_purge(monkeypatch: pytest.MonkeyPatch) -> None:
    """跳过启动恢复里的 aria2 孤儿扫描。

    `DownloadRecoveryService.recover()` 的最后一步 `purge_orphans()` 会调用
    aria2 RPC `tellActive`。测试环境没有 aria2 进程，每次应用启动都要等
    连接超时（约 1.8 秒），而空库下没有孤儿可清，跳过不改变任何断言。

    用 autouse + 类级 patch：本目录下自行 `create_app()` 的测试同样生效，
    不需要逐个改调用点。直接验证该行为的测试在 `tests/application` 与
    `tests/infrastructure`，不受本 fixture 影响。
    """
    from aria2deck.application.downloads.lifecycle import DownloadLifecycleService

    async def no_orphans(self: DownloadLifecycleService) -> None:
        return None

    monkeypatch.setattr(DownloadLifecycleService, "purge_orphans", no_orphans)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "data" / "v2.db",
        download_directory=tmp_path / "downloads",
        static_directory=tmp_path / "static",
        debug=True,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings=settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def create_admin(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/v2/users",
        json={"username": "admin", "password": PASSWORD, "is_admin": True},
    )
    assert response.status_code == 201
    return response.json()


def login(client: TestClient, username: str = "admin", password: str = PASSWORD) -> str:
    response = client.post(
        "/api/v2/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200
    return response.cookies["aria2deck_v2_session"]


def create_user(
    client: TestClient,
    username: str = "user",
    *,
    password: str = OTHER_PASSWORD,
    is_admin: bool = False,
) -> dict[str, object]:
    response = client.post(
        "/api/v2/users",
        json={"username": username, "password": password, "is_admin": is_admin},
    )
    assert response.status_code == 201
    return response.json()
