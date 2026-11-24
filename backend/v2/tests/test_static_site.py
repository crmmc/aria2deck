from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aria2deck.bootstrap import create_app
from aria2deck.main import app


def test_application_entrypoint_is_independent() -> None:
    assert app.title == "Aria2Deck V2"


@pytest.mark.parametrize("path", ["/", "/tasks", "/s/example"])
def test_spa_routes_serve_the_built_index(tmp_path: Path, path: str) -> None:
    (tmp_path / "index.html").write_text("<main>V2 home</main>", encoding="utf-8")
    client = TestClient(create_app(static_dir=tmp_path))

    response = client.get(path)

    assert response.status_code == 200
    assert response.text == "<main>V2 home</main>"


def test_vite_assets_are_served_from_the_v2_build(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("export {};", encoding="utf-8")
    client = TestClient(create_app(static_dir=tmp_path))

    response = client.get("/assets/app.js")

    assert response.status_code == 200
    assert response.text == "export {};"


def test_missing_frontend_build_is_explicit(tmp_path: Path) -> None:
    client = TestClient(create_app(static_dir=tmp_path))

    response = client.get("/")

    assert response.status_code == 503
    assert response.json() == {"error": {"code": "http_error", "message": "前端页面尚未构建"}}
