from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.services.health_service import readiness_errors

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("")
@router.get("/live")
async def get_health() -> dict[str, bool]:
    """Compatibility endpoint and liveness probe; dependencies are not checked."""
    return {"ok": True}


@router.get("/ready")
async def get_ready(request: Request) -> JSONResponse:
    errors = await readiness_errors(request.app)
    if errors:
        return JSONResponse(status_code=503, content={"ok": False, "errors": errors})
    return JSONResponse(content={"ok": True})
