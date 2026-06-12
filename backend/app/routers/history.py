"""任务历史记录接口

独立于活动任务，记录用户的下载历史。
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import AuthUser, require_user
from app.domain.downloads import TERMINAL_USER_TASK_STATUSES
from app.repositories.downloads import (
    clear_terminal_user_tasks,
    delete_terminal_user_task,
    list_user_tasks,
)

router = APIRouter(prefix="/api/history", tags=["history"])
logger = logging.getLogger(__name__)


def _ms_to_iso(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


@router.get("")
async def list_history(user: AuthUser = Depends(require_user)) -> list[dict]:
    """获取当前用户的任务历史"""
    records = await list_user_tasks(user.id, TERMINAL_USER_TASK_STATUSES)
    logger.debug("查询历史记录 user_id=%s count=%s", user.id, len(records))

    return [
        {
            "id": row["id"],
            "task_name": row.get("display_name")
            or row.get("global_display_name")
            or "未知任务",
            "uri": row.get("source_uri"),
            "total_length": int(row.get("total_bytes") or 0),
            "result": row["status"],
            "reason": row.get("error_message") or row.get("global_error_message"),
            "created_at": _ms_to_iso(row.get("created_at_ms")),
            "finished_at": _ms_to_iso(
                row.get("finished_at_ms") or row.get("completed_at_ms")
            ),
        }
        for row in records
    ]


@router.delete("/{history_id}")
async def delete_history(
    history_id: int,
    user: AuthUser = Depends(require_user),
) -> dict:
    """删除单条历史记录"""
    deleted = await delete_terminal_user_task(user.id, history_id)
    if not deleted:
        logger.warning(
            "删除历史记录失败 user_id=%s history_id=%s reason=not_found",
            user.id,
            history_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="历史记录不存在",
        )

    logger.info("删除历史记录成功 user_id=%s history_id=%s", user.id, history_id)

    return {"ok": True}


@router.delete("")
async def clear_history(user: AuthUser = Depends(require_user)) -> dict:
    """清空当前用户的所有历史记录"""
    count = await clear_terminal_user_tasks(user.id)

    logger.info("清空历史记录成功 user_id=%s count=%s", user.id, count)

    return {"ok": True, "count": count}
