from fastapi import APIRouter


router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("")
async def get_health() -> dict[str, bool]:
    return {"ok": True}
