from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INVALID_STATE = "invalid_state"
    NOT_FOUND = "not_found"
    UNAUTHENTICATED = "unauthenticated"
    ACCOUNT_FROZEN = "account_frozen"
    ACCOUNT_LOCKED = "account_locked"
    PERMISSION_DENIED = "permission_denied"
    DUPLICATE_RESOURCE = "duplicate_resource"
    QUOTA_EXCEEDED = "quota_exceeded"
    PHYSICAL_SPACE_EXCEEDED = "physical_space_exceeded"
    CONTENT_BUSY = "content_busy"
    CONTENT_PENDING_DELETE = "content_pending_delete"
    SHARE_EXPIRED = "share_expired"
    SHARE_REVOKED = "share_revoked"
    SHARE_EXHAUSTED = "share_exhausted"
    SHARE_UNAVAILABLE = "share_unavailable"
    SHARE_LIMIT_EXCEEDED = "share_limit_exceeded"
    MISSING_CREDENTIALS = "password_required"
    INVALID_CREDENTIALS = "password_invalid"
    RANGE_NOT_SATISFIABLE = "range_not_satisfiable"
    STALE_EVENT = "stale_event"
    SUBMISSION_UNCERTAIN = "submission_uncertain"
    RPC_UNAVAILABLE = "rpc_unavailable"
    TASK_TOO_LARGE = "task_too_large"
    DOWNLOAD_UNAVAILABLE = "download_unavailable"
    UPSTREAM_FAILURE = "upstream_failure"
    RECOVERY_UNSAFE = "recovery_unsafe"
    MANIFEST_UNAVAILABLE = "manifest_unavailable"
    LAST_ADMIN = "last_admin"
    SELF_DELETE = "self_delete"
    CREDENTIAL_INVALID = "credential_invalid"
    CONFIGURATION_INVALID = "configuration_invalid"
    SCHEMA_MISMATCH = "schema_mismatch"
    RATE_LIMITED = "rate_limited"
    NOT_IMPLEMENTED = "not_implemented"


class DomainError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds
