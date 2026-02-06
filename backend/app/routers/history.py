"""任务历史记录接口

独立于活动任务，记录用户的下载历史。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import select

from app.auth import require_user
from app.database import get_session
from app.models import TaskHistory, User

router = APIRouter(prefix="/api/history", tags=["history"])
logger = logging.getLogger(__name__)


@router.get("")
async def list_history(user: User = Depends(require_user)) -> list[dict]:
    """获取当前用户的任务历史"""
    async with get_session() as db:
        result = await db.exec(
            select(TaskHistory)
            .where(TaskHistory.owner_id == user.id)
            .order_by(TaskHistory.id.desc())
        )
        records = result.all()

    logger.debug("查询历史记录 user_id=%s count=%s", user.id, len(records))

    return [
        {
            "id": r.id,
            "task_name": r.task_name,
            "uri": r.uri,
            "total_length": r.total_length,
            "result": r.result,
            "reason": r.reason,
            "created_at": r.created_at,
            "finished_at": r.finished_at,
        }
        for r in records
    ]


@router.delete("/{history_id}")
async def delete_history(
    history_id: int,
    user: User = Depends(require_user),
) -> dict:
    """删除单条历史记录"""
    async with get_session() as db:
        result = await db.exec(
            select(TaskHistory).where(
                TaskHistory.id == history_id,
                TaskHistory.owner_id == user.id,
            )
        )
        record = result.first()

        if not record:
            logger.warning("删除历史记录失败 user_id=%s history_id=%s reason=not_found", user.id, history_id)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="历史记录不存在"
            )

        await db.delete(record)

    logger.info("删除历史记录成功 user_id=%s history_id=%s", user.id, history_id)

    return {"ok": True}


@router.delete("")
async def clear_history(user: User = Depends(require_user)) -> dict:
    """清空当前用户的所有历史记录"""
    async with get_session() as db:
        result = await db.exec(
            select(TaskHistory).where(TaskHistory.owner_id == user.id)
        )
        records = result.all()

        count = len(records)
        for r in records:
            await db.delete(r)

    logger.info("清空历史记录成功 user_id=%s count=%s", user.id, count)

    return {"ok": True, "count": count}
