"""User-facing projection.

Maps (status, error_code) to a short Chinese label. UI remains responsible
for richer messages; this module only stabilises the state vocabulary.
"""

from __future__ import annotations

from app.modules.task_core.states import (
    ERROR_DISK_QUEUED,
    ERROR_QUOTA_QUEUED,
    TidState,
)

_QUEUED_LABEL = "排队中"
_DOWNLOADING_LABEL = "下载中"
_PAUSED_LABEL = "已暂停"
_COMPLETED_LABEL = "已完成"
_FAILED_LABEL = "已失败"
_CANCELLED_LABEL = "已取消"
_UNKNOWN_LABEL = "未知"


def user_visible_label(status: str, error_code: str | None) -> str:
    """Return the user-visible Chinese label for a (status, error_code) pair.

    Rules (v1):
    - queued (either platform-side or us-side) -> 排队中
    - active / waiting (running) -> 下载中
    - paused -> 已暂停 (caller inspects error_code for the "external" reason)
    - completed / failed / cancelled -> 已完成 / 已失败 / 已取消
    - anything else -> 未知 (defensive; should not happen once v1 lands)
    """
    if status == TidState.QUEUED.value:
        return _QUEUED_LABEL
    if status in (TidState.ACTIVE.value, TidState.WAITING.value):
        return _DOWNLOADING_LABEL
    if status == TidState.PAUSED.value:
        return _PAUSED_LABEL
    if status == TidState.COMPLETED.value:
        return _COMPLETED_LABEL
    if status == TidState.FAILED.value:
        return _FAILED_LABEL
    if status == TidState.CANCELLED.value:
        return _CANCELLED_LABEL
    # error_code currently does not change the label; reserved for future
    # nuances (e.g. distinguishing "已暂停" vs "已暂停(外部)"). Referenced
    # here so static analysers see it as part of the contract.
    _ = (error_code, ERROR_QUOTA_QUEUED, ERROR_DISK_QUEUED)
    return _UNKNOWN_LABEL
