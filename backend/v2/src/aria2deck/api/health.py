from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/health", tags=["system"])


class LiveResponse(BaseModel):
    ok: bool = True


class ReadyResponse(BaseModel):
    ok: bool = True


class ReadyFailureResponse(BaseModel):
    ok: bool = False
    errors: list[str]


@router.get("", response_model=LiveResponse)
@router.get("/live", response_model=LiveResponse)
def live() -> dict[str, bool]:
    return {"ok": True}


@router.get("/ready", response_model=ReadyResponse, responses={503: {"model": ReadyFailureResponse}})
async def ready(request: Request) -> JSONResponse:
    check = cast(
        Callable[[], Awaitable[bool]] | None,
        getattr(request.app.state, "readiness_check", None),
    )
    details = getattr(request.app.state, "readiness_details", None)
    if details is not None:
        errors = await details()
    elif check is not None and await check():
        errors = []
    else:
        errors = ["数据库不可用"]
    if not errors:
        return JSONResponse({"ok": True})
    return JSONResponse(status_code=503, content={"ok": False, "errors": errors})
