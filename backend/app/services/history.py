"""任务历史记录服务"""
from app.database import get_session
from app.models import TaskHistory, utc_now_str


async def add_task_history(
    owner_id: int,
    task_name: str,
    result: str,
    reason: str | None = None,
    uri: str | None = None,
    total_length: int = 0,
    created_at: str | None = None,
) -> TaskHistory:
    """添加任务历史记录

    Args:
        owner_id: 用户 ID
        task_name: 任务名称
        result: 结果状态 (completed, cancelled, failed)
        reason: 原因说明
        uri: 下载链接
        total_length: 文件大小
        created_at: 任务创建时间（可选，默认当前时间）
    """
    async with get_session() as db:
        history = TaskHistory(
            owner_id=owner_id,
            task_name=task_name,
            uri=uri,
            total_length=total_length,
            result=result,
            reason=reason,
            created_at=created_at or utc_now_str(),
            finished_at=utc_now_str(),
        )
        db.add(history)
        await db.commit()
        await db.refresh(history)
        return history
