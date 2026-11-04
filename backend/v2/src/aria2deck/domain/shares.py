from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from enum import StrEnum

from .errors import DomainError, ErrorCode

MAX_SHARE_EXPIRY_SECONDS = 30 * 24 * 60 * 60
MAX_SHARE_DOWNLOADS = 10000
MAX_ACTIVE_SHARES_PER_FILE = 10
MAX_SHARE_PASSWORD_LENGTH = 100


class ShareStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    EXHAUSTED = "exhausted"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class Share:
    expires_at_ms: int | None
    revoked_at_ms: int | None
    max_downloads: int | None
    download_count: int
    file_pending_delete: bool = False


def share_status(share: Share, now_ms: int) -> ShareStatus:
    if share.download_count < 0 or share.max_downloads is not None and share.max_downloads < 0:
        raise DomainError(ErrorCode.INVALID_INPUT, "分享下载计数无效")
    if share.file_pending_delete:
        return ShareStatus.UNAVAILABLE
    if share.revoked_at_ms is not None:
        return ShareStatus.REVOKED
    if share.expires_at_ms is not None and now_ms >= share.expires_at_ms:
        return ShareStatus.EXPIRED
    if share.max_downloads is not None and share.download_count >= share.max_downloads:
        return ShareStatus.EXHAUSTED
    return ShareStatus.ACTIVE


def validate_share_options(
    expires_in_seconds: int | None, max_downloads: int | None, password: str | None
) -> None:
    if expires_in_seconds is not None and not 1 <= expires_in_seconds <= MAX_SHARE_EXPIRY_SECONDS:
        raise DomainError(ErrorCode.INVALID_INPUT, "分享有效期必须在 1 秒到 30 天之间")
    if max_downloads is not None and not 1 <= max_downloads <= MAX_SHARE_DOWNLOADS:
        raise DomainError(ErrorCode.INVALID_INPUT, "分享下载次数必须在 1 到 10000 之间")
    if password is not None and not 1 <= len(password) <= MAX_SHARE_PASSWORD_LENGTH:
        raise DomainError(ErrorCode.INVALID_INPUT, "分享密码长度必须在 1 到 100 个字符之间")


def ensure_share_capacity(active_count: int) -> None:
    if active_count < 0:
        raise DomainError(ErrorCode.INVALID_INPUT, "活跃分享数量不能为负数")
    if active_count >= MAX_ACTIVE_SHARES_PER_FILE:
        raise DomainError(ErrorCode.SHARE_LIMIT_EXCEEDED, "每个文件最多创建 10 个有效分享")


def hash_share_password(share_code: str, password: str) -> str:
    return hashlib.sha256(f"{share_code}\0{password}".encode()).hexdigest()


def verify_share_password(share_code: str, password: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_share_password(share_code, password), expected_hash)


def require_password(expected_hash: str | None, share_code: str, password: str | None) -> None:
    if expected_hash is None:
        raise DomainError(ErrorCode.INVALID_INPUT, "该分享无需密码")
    if password is None:
        raise DomainError(ErrorCode.MISSING_CREDENTIALS, "请输入分享密码")
    if not verify_share_password(share_code, password, expected_hash):
        raise DomainError(ErrorCode.INVALID_CREDENTIALS, "分享密码错误")


def require_share_available(share: Share, now_ms: int) -> None:
    status = share_status(share, now_ms)
    errors = {
        ShareStatus.REVOKED: (ErrorCode.SHARE_REVOKED, "分享已撤销"),
        ShareStatus.EXPIRED: (ErrorCode.SHARE_EXPIRED, "分享已过期"),
        ShareStatus.EXHAUSTED: (ErrorCode.SHARE_EXHAUSTED, "分享下载次数已用尽"),
        ShareStatus.UNAVAILABLE: (ErrorCode.SHARE_UNAVAILABLE, "分享文件已删除"),
    }
    if status is not ShareStatus.ACTIVE:
        code, message = errors[status]
        raise DomainError(code, message)


@dataclass(frozen=True, slots=True)
class DownloadDecision:
    start: int
    end: int
    status_code: int
    count_download: bool

    @property
    def content_length(self) -> int:
        return self.end - self.start + 1


def _unsatisfiable() -> DomainError:
    return DomainError(ErrorCode.RANGE_NOT_SATISFIABLE, "请求的文件范围无效")


def decide_download(size: int, range_header: str | None) -> DownloadDecision:
    if size < 0:
        raise DomainError(ErrorCode.INVALID_INPUT, "文件大小不能为负数")
    if range_header is None:
        return DownloadDecision(0, size - 1, 200, True)
    unit, separator, value = range_header.strip().partition("=")
    if unit.lower() != "bytes":
        return DownloadDecision(0, size - 1, 200, True)
    if "," in value:
        if value.count(",") == 1 and all(value.split(",")):
            return DownloadDecision(0, size - 1, 200, True)
        raise _unsatisfiable()
    if not separator or size == 0:
        raise _unsatisfiable()
    start_text, dash, end_text = value.partition("-")
    if not dash or not start_text.isdigit() and not end_text.isdigit():
        raise _unsatisfiable()
    if not start_text:
        suffix = int(end_text)
        if suffix == 0:
            raise _unsatisfiable()
        start, end = max(size - suffix, 0), size - 1
    else:
        start = int(start_text)
        if start >= size:
            raise _unsatisfiable()
        if end_text:
            if not end_text.isdigit():
                raise _unsatisfiable()
            end = min(int(end_text), size - 1)
            if end < start:
                raise _unsatisfiable()
        else:
            end = size - 1
    full_entity = start == 0 and end == size - 1
    return DownloadDecision(start, end, 206, full_entity)


def plan_share_download(
    share: Share, *, now_ms: int, size: int, range_header: str | None
) -> DownloadDecision:
    require_share_available(share, now_ms)
    return decide_download(size, range_header)
