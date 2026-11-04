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
