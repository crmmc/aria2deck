from __future__ import annotations

import logging

from app.core.time_utils import ms_to_iso
from app.domain.errors import DomainError, NotFoundError
from app.domain.status import TERMINAL_USER_TASK_STATUSES
from app.repositories.task.user_tasks import (
    clear_terminal_user_tasks,
    delete_terminal_user_task,
    list_user_tasks,
    list_user_tasks_page,
)
from app.services.history_retention import reclaim_zero_pid_tid

logger = logging.getLogger(__name__)

MSG_HISTORY_EXPIRED = "已过期"
MSG_COMPLETED = "已完成不可重试"
MSG_NOT_RETRYABLE = "不可重试"
RETRYABLE_STATUSES = frozenset({"failed", "cancelled"})


def history_retry_projection(row: dict) -> tuple[bool, str | None]:
    """Return (retryable, retry_blocked_reason) for a history/terminal row."""
    if row.get("history_expired_at_ms") is not None:
        return False, MSG_HISTORY_EXPIRED
    status = str(row.get("status") or "")
    if status == "completed":
        return False, MSG_COMPLETED
    if status in RETRYABLE_STATUSES:
        return True, None
    return False, MSG_NOT_RETRYABLE


def _history_response(row: dict) -> dict:
    retryable, retry_blocked_reason = history_retry_projection(row)
    return {
        "id": row["id"],
        "task_name": row.get("display_name") or row.get("global_display_name") or "未知任务",
        "uri": row.get("source_uri"),
        "total_length": int(row.get("total_bytes") or 0),
        "result": row["status"],
        "reason": row.get("error_message") or row.get("global_error_message"),
        "created_at": ms_to_iso(row.get("created_at_ms")),
        "finished_at": ms_to_iso(row.get("finished_at_ms") or row.get("completed_at_ms")),
        "retryable": retryable,
        "retry_blocked_reason": retry_blocked_reason,
    }


async def list_history(user_id: int) -> list[dict]:
    records = await list_user_tasks(user_id, TERMINAL_USER_TASK_STATUSES)
    logger.debug("查询历史记录 user_id=%s count=%s", user_id, len(records))
    return [_history_response(row) for row in records]


async def list_history_page(*, user_id: int, page: int, page_size: int) -> dict:
    records, total = await list_user_tasks_page(
        user_id,
        page=page,
        page_size=page_size,
        statuses=TERMINAL_USER_TASK_STATUSES,
    )
    return {
        "items": [_history_response(row) for row in records],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


async def delete_history(user_id: int, history_id: int) -> dict:
    tid = await delete_terminal_user_task(user_id, history_id)
    if tid is None:
        logger.warning(
            "删除历史记录失败 user_id=%s history_id=%s reason=not_found",
            user_id,
            history_id,
        )
        raise NotFoundError("历史记录不存在")

    await reclaim_zero_pid_tid(tid)
    logger.info("删除历史记录成功 user_id=%s history_id=%s tid=%s", user_id, history_id, tid)
    return {"ok": True}


async def clear_history(user_id: int) -> dict:
    tids = await clear_terminal_user_tasks(user_id)
    for tid in set(tids):
        await reclaim_zero_pid_tid(tid)
    count = len(tids)
    logger.info("清空历史记录成功 user_id=%s count=%s", user_id, count)
    return {"ok": True, "count": count}


async def bulk_delete_history(user_id: int, history_ids: list[int]) -> dict:
    accepted_count = 0
    failed_count = 0
    results: list[dict] = []
    for history_id in dict.fromkeys(history_ids):
        try:
            await delete_history(user_id, history_id)
            accepted_count += 1
            results.append(
                {
                    "history_id": history_id,
                    "ok": True,
                    "state": "deleted",
                    "accepted": True,
                    "error": None,
                }
            )
        except DomainError as exc:
            failed_count += 1
            results.append(
                {
                    "history_id": history_id,
                    "ok": False,
                    "state": "failed",
                    "accepted": False,
                    "error": exc.detail,
                }
            )
        except Exception:
            failed_count += 1
            results.append(
                {
                    "history_id": history_id,
                    "ok": False,
                    "state": "failed",
                    "accepted": False,
                    "error": "删除历史记录失败",
                }
            )
            logger.exception(
                "批量删除历史记录失败 user_id=%s history_id=%s", user_id, history_id
            )
    logger.info(
        "批量删除历史记录完成 user_id=%s requested=%s accepted=%s failed=%s",
        user_id,
        len(history_ids),
        accepted_count,
        failed_count,
    )
    return {
        "accepted_count": accepted_count,
        "failed_count": failed_count,
        "results": results,
    }
