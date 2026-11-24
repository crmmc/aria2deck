"""健康探针与静态站点兜底的边界契约。

这两处此前没有专门测试：`health.ready` 的三个分支（details / check / 兜底）
与 `static_site.serve_page` 的 404、503、正常返回都缺断言。它们属于运维可见面，
出错时表现为「容器一直不健康」或「首页 503」，值得单独锁定。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria2deck.api.static_site import register_static_site
from aria2deck.bootstrap import create_app
from aria2deck.infrastructure.config import Settings


@pytest.fixture
def bare_app(tmp_path: Path) -> FastAPI:
    settings = Settings(
        database_path=tmp_path / "data" / "v2.db",
        download_directory=tmp_path / "downloads",
        static_directory=tmp_path / "static",
        debug=True,
    )
    return create_app(settings=settings)


def test_readiness_uses_details_when_available(client: TestClient) -> None:
    """详情回调返回错误时，就绪探针必须 503 并带上错误列表。"""

    async def failing() -> list[str]:
        return ["数据库不可用", "aria2 不可达"]

    client.app.state.readiness_details = failing
    response = client.get("/api/health/ready")
    assert response.status_code == 503
    assert response.json() == {"ok": False, "errors": ["数据库不可用", "aria2 不可达"]}


def test_readiness_uses_details_success(client: TestClient) -> None:
    """详情回调返回空列表时视为就绪。"""

    async def healthy() -> list[str]:
        return []

    client.app.state.readiness_details = healthy
    response = client.get("/api/health/ready")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_readiness_falls_back_to_check_when_details_absent(client: TestClient) -> None:
    """没有详情回调时用布尔检查；通过即就绪。"""

    async def ok() -> bool:
        return True

    client.app.state.readiness_details = None
    client.app.state.readiness_check = ok
    response = client.get("/api/health/ready")
    assert response.status_code == 200


def test_readiness_reports_unavailable_when_check_fails(client: TestClient) -> None:
    """布尔检查失败时给出默认错误文案，且必须是 503。"""

    async def not_ready() -> bool:
        return False

    client.app.state.readiness_details = None
    client.app.state.readiness_check = not_ready
    response = client.get("/api/health/ready")
    assert response.status_code == 503
    assert response.json() == {"ok": False, "errors": ["数据库不可用"]}


def test_readiness_reports_unavailable_when_nothing_configured(client: TestClient) -> None:
    """两个回调都没装配时不得误报就绪。"""
    client.app.state.readiness_details = None
    client.app.state.readiness_check = None
    response = client.get("/api/health/ready")
    assert response.status_code == 503
    assert response.json()["errors"] == ["数据库不可用"]


def test_liveness_does_not_depend_on_readiness(client: TestClient) -> None:
    """存活探针不参与就绪判断，始终 200。"""
    client.app.state.readiness_details = None
    client.app.state.readiness_check = None
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/api/health/live").json() == {"ok": True}


def _mount_static(tmp_path: Path) -> tuple[FastAPI, Path]:
    static_dir = tmp_path / "site"
    static_dir.mkdir(exist_ok=True)
    app = FastAPI()
    register_static_site(app, static_dir=static_dir)
    return app, static_dir


def test_static_site_serves_index(tmp_path: Path) -> None:
    """前端产物存在时，任意非 api 路径都返回 index（SPA 路由兜底）。"""
    app, static_dir = _mount_static(tmp_path)
    (static_dir / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    with TestClient(app) as client:
        for path in ("/", "/tasks", "/files/deep/path"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert "ok" in response.text


def test_static_site_rejects_api_paths(tmp_path: Path) -> None:
    """兜底路由不得吞掉 /api 前缀，未注册的接口必须是 404 而不是返回 HTML。"""
    app, static_dir = _mount_static(tmp_path)
    (static_dir / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    with TestClient(app) as client:
        for path in ("/api", "/api/unknown", "/api/v2/nope"):
            response = client.get(path)
            assert response.status_code == 404, path
            assert response.json()["detail"] == "接口不存在"


def test_static_site_reports_missing_build(tmp_path: Path) -> None:
    """前端未构建时首页返回 503 并说明原因，而不是 404 或空响应。"""
    app, _static_dir = _mount_static(tmp_path)

    with TestClient(app) as client:
        response = client.get("/tasks")
        assert response.status_code == 503
        assert response.json()["detail"] == "前端页面尚未构建"


def test_static_site_mounts_assets_when_present(tmp_path: Path) -> None:
    """assets 目录存在时才挂载静态资源；不存在时不挂载也不报错。"""
    app, static_dir = _mount_static(tmp_path)
    mounted = [route.path for route in app.routes if getattr(route, "path", "") == "/assets"]
    assert mounted == []

    (static_dir / "assets").mkdir()
    (static_dir / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    fresh, _ = _mount_static(tmp_path)
    assert any(getattr(route, "path", "") == "/assets" for route in fresh.routes)
