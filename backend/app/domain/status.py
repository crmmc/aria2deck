from __future__ import annotations

from enum import StrEnum


class DownloadStatus(StrEnum):
    QUEUED = "queued"
    ACTIVE = "active"
    WAITING = "waiting"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    ACTIVE = "active"
    WAITING = "waiting"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PackStatus(StrEnum):
    PENDING = "pending"
    PACKING = "packing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ShareStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


GLOBAL_DOWNLOAD_STATUSES = tuple(status.value for status in DownloadStatus)
USER_TASK_STATUSES = tuple(status.value for status in TaskStatus)
PACK_TASK_STATUSES = tuple(status.value for status in PackStatus)
SHARE_STATUSES = tuple(status.value for status in ShareStatus)

ACTIVE_LIKE_DOWNLOAD_STATUSES = (
    DownloadStatus.QUEUED.value,
    DownloadStatus.ACTIVE.value,
    DownloadStatus.WAITING.value,
    DownloadStatus.PAUSED.value,
)
ACTIVE_USER_TASK_STATUSES = ACTIVE_LIKE_DOWNLOAD_STATUSES
ACTIVE_GLOBAL_DOWNLOAD_STATUSES = ACTIVE_LIKE_DOWNLOAD_STATUSES
TERMINAL_DOWNLOAD_STATUSES = (
    DownloadStatus.COMPLETED.value,
    DownloadStatus.FAILED.value,
    DownloadStatus.CANCELLED.value,
)
TERMINAL_USER_TASK_STATUSES = TERMINAL_DOWNLOAD_STATUSES
ERROR_DOWNLOAD_STATUSES = (
    DownloadStatus.FAILED.value,
    DownloadStatus.CANCELLED.value,
)
FAILABLE_GLOBAL_DOWNLOAD_STATUSES = (*ACTIVE_GLOBAL_DOWNLOAD_STATUSES, "completed")
REST_TASK_STATUS_FILTERS = frozenset(("active", "current", "complete", "error"))
