"""管理员历史 purge 接口。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.auth import require_limited_admin
from app.domain.errors import DomainError
from app.http.errors import raise_http
from app.services import history_retention

router = APIRouter(prefix="/api/admin/history", tags=["admin-history"])
logger = logging.getLogger(__name__)


class AdminHistoryPurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before_ms: int | None = Field(
        None, description="对 finished 早于该时刻的终态执行软过期+GC"
    )
    older_than_days: int | None = Field(
        None, ge=1, description="对 finished 早于 N 天前的终态执行软过期+GC"
    )
    hard_delete: bool | None = Field(
        None,
        description="可选；本版本忽略。不得因 purge 删除仍可见 pid 或 completed 秒传所需 tid",
    )


@router.post("/purge")
async def purge_history(
    payload: AdminHistoryPurgeRequest,
    admin=Depends(require_limited_admin),
) -> dict:
    try:
        result = await history_retention.purge_by_cutoff(
            before_ms=payload.before_ms,
            older_than_days=payload.older_than_days,
        )
    except DomainError as exc:
        raise_http(exc)
    logger.info(
        "管理员历史 purge 完成 admin_id=%s expired=%s detached=%s gcs=%s skipped_live=%s",
        admin.id,
        result["expired_user_tasks"],
        result["detached_source_tids"],
        result["gcs_sources"],
        result["skipped_live"],
    )
    return result
