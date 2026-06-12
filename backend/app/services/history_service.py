from __future__ import annotations

import logging

from app.core.time_utils import ms_to_iso
from app.domain.downloads import TERMINAL_USER_TASK_STATUSES
from app.domain.errors import NotFoundError
from app.repositories.downloads import (
    clear_terminal_user_tasks,
    delete_terminal_user_task,
    list_user_tasks,
)

logger = logging.getLogger(__name__)


async def list_history(user_id: int) -> list[dict]:
    records = await list_user_tasks(user_id, TERMINAL_USER_TASK_STATUSES)
    logger.debug("查询历史记录 user_id=%s count=%s", user_id, len(records))

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
            "created_at": ms_to_iso(row.get("created_at_ms")),
            "finished_at": ms_to_iso(
                row.get("finished_at_ms") or row.get("completed_at_ms")
            ),
        }
        for row in records
    ]


async def delete_history(user_id: int, history_id: int) -> dict:
    deleted = await delete_terminal_user_task(user_id, history_id)
    if not deleted:
        logger.warning(
            "删除历史记录失败 user_id=%s history_id=%s reason=not_found",
            user_id,
            history_id,
        )
        raise NotFoundError("历史记录不存在")

    logger.info("删除历史记录成功 user_id=%s history_id=%s", user_id, history_id)
    return {"ok": True}


async def clear_history(user_id: int) -> dict:
    count = await clear_terminal_user_tasks(user_id)
    logger.info("清空历史记录成功 user_id=%s count=%s", user_id, count)
    return {"ok": True, "count": count}
