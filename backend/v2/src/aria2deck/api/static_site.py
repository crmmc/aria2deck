from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles


def register_static_site(app: FastAPI, *, static_dir: Path) -> None:
    assets_dir = static_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{requested_path:path}", include_in_schema=False)
    def serve_page(requested_path: str) -> Response:
        if requested_path == "api" or requested_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="接口不存在")

        index_file = static_dir / "index.html"
        if not index_file.is_file():
            raise HTTPException(status_code=503, detail="前端页面尚未构建")
        return FileResponse(index_file)
