from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from .errors import DomainError, ErrorCode


class AttemptStatus(StrEnum):
    QUEUED = "queued"
    ACTIVE = "active"
    WAITING = "waiting"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SubscriptionStatus(StrEnum):
    QUEUED = "queued"
    ACTIVE = "active"
    WAITING = "waiting"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


ADMISSION_PAUSED = "admission_paused"
METADATA_ADMISSION_PAUSED = "metadata_admission_paused"
EXTERNAL_PAUSED = "external_paused"
SYSTEM_PAUSE_CODES = frozenset({ADMISSION_PAUSED, METADATA_ADMISSION_PAUSED})


LIVE_ATTEMPT_STATUSES = frozenset(
    {
        AttemptStatus.QUEUED,
        AttemptStatus.ACTIVE,
        AttemptStatus.WAITING,
        AttemptStatus.PAUSED,
    }
)
LIVE_SUBSCRIPTION_STATUSES = frozenset(
    {
        SubscriptionStatus.QUEUED,
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.WAITING,
        SubscriptionStatus.PAUSED,
    }
)

_ATTEMPT_TRANSITIONS = {
    AttemptStatus.QUEUED: frozenset(
        {
            AttemptStatus.ACTIVE,
            AttemptStatus.WAITING,
            AttemptStatus.PAUSED,
            AttemptStatus.COMPLETED,
            AttemptStatus.FAILED,
            AttemptStatus.CANCELLED,
        }
    ),
    AttemptStatus.ACTIVE: frozenset(
        {
            AttemptStatus.WAITING,
            AttemptStatus.PAUSED,
            AttemptStatus.COMPLETED,
            AttemptStatus.FAILED,
            AttemptStatus.CANCELLED,
        }
    ),
    AttemptStatus.WAITING: frozenset(
        {
            AttemptStatus.QUEUED,
            AttemptStatus.ACTIVE,
            AttemptStatus.PAUSED,
            AttemptStatus.COMPLETED,
            AttemptStatus.FAILED,
            AttemptStatus.CANCELLED,
        }
    ),
    AttemptStatus.PAUSED: frozenset(
        {
            AttemptStatus.QUEUED,
            AttemptStatus.ACTIVE,
            AttemptStatus.WAITING,
            AttemptStatus.COMPLETED,
            AttemptStatus.FAILED,
            AttemptStatus.CANCELLED,
        }
    ),
    AttemptStatus.COMPLETED: frozenset(),
    AttemptStatus.FAILED: frozenset(),
    AttemptStatus.CANCELLED: frozenset(),
}
_SUBSCRIPTION_TRANSITIONS = {
    SubscriptionStatus.QUEUED: frozenset(
        {
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.WAITING,
            SubscriptionStatus.PAUSED,
            SubscriptionStatus.COMPLETED,
            SubscriptionStatus.FAILED,
            SubscriptionStatus.CANCELLED,
            SubscriptionStatus.EXPIRED,
        }
    ),
    SubscriptionStatus.ACTIVE: frozenset(
        {
            SubscriptionStatus.WAITING,
            SubscriptionStatus.PAUSED,
            SubscriptionStatus.COMPLETED,
            SubscriptionStatus.FAILED,
            SubscriptionStatus.CANCELLED,
        }
    ),
    SubscriptionStatus.WAITING: frozenset(
        {
            SubscriptionStatus.QUEUED,
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.PAUSED,
            SubscriptionStatus.COMPLETED,
            SubscriptionStatus.FAILED,
            SubscriptionStatus.CANCELLED,
            SubscriptionStatus.EXPIRED,
        }
    ),
    SubscriptionStatus.PAUSED: frozenset(
        {
            SubscriptionStatus.QUEUED,
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.WAITING,
            SubscriptionStatus.COMPLETED,
            SubscriptionStatus.FAILED,
            SubscriptionStatus.CANCELLED,
            SubscriptionStatus.EXPIRED,
        }
    ),
    SubscriptionStatus.COMPLETED: frozenset({SubscriptionStatus.EXPIRED}),
    SubscriptionStatus.FAILED: frozenset({SubscriptionStatus.EXPIRED}),
    SubscriptionStatus.CANCELLED: frozenset({SubscriptionStatus.EXPIRED}),
    SubscriptionStatus.EXPIRED: frozenset(),
}


def transition_attempt(current: AttemptStatus, target: AttemptStatus) -> AttemptStatus:
    if current == target:
        return current
    if target not in _ATTEMPT_TRANSITIONS[current]:
        raise DomainError(ErrorCode.INVALID_STATE, "下载尝试不允许该状态转换")
    return target


def transition_subscription(
    current: SubscriptionStatus, target: SubscriptionStatus
) -> SubscriptionStatus:
    if current == target:
        return current
    if target not in _SUBSCRIPTION_TRANSITIONS[current]:
        raise DomainError(ErrorCode.INVALID_STATE, "用户任务不允许该状态转换")
    return target


TERMINAL_SUBSCRIPTION_STATUSES = frozenset(
    {
        SubscriptionStatus.COMPLETED,
        SubscriptionStatus.FAILED,
        SubscriptionStatus.CANCELLED,
        SubscriptionStatus.EXPIRED,
    }
)

_ATTEMPT_TERMINAL_TO_SUBSCRIPTION = {
    AttemptStatus.COMPLETED: SubscriptionStatus.COMPLETED,
    AttemptStatus.FAILED: SubscriptionStatus.FAILED,
    AttemptStatus.CANCELLED: SubscriptionStatus.CANCELLED,
}

_STATUS_LABELS = {
    SubscriptionStatus.QUEUED: "排队中",
    SubscriptionStatus.ACTIVE: "下载中",
    SubscriptionStatus.WAITING: "等待中",
    SubscriptionStatus.PAUSED: "已暂停",
    SubscriptionStatus.COMPLETED: "已完成",
    SubscriptionStatus.FAILED: "已失败",
    SubscriptionStatus.CANCELLED: "已取消",
    SubscriptionStatus.EXPIRED: "已过期",
}


def effective_task_status(
    subscription: SubscriptionStatus, attempt: AttemptStatus
) -> SubscriptionStatus:
    if subscription in TERMINAL_SUBSCRIPTION_STATUSES:
        return subscription
    return _ATTEMPT_TERMINAL_TO_SUBSCRIPTION.get(attempt, subscription)


def task_status_label(
    status: SubscriptionStatus, *, ingestion_pending: bool = False
) -> str:
    if ingestion_pending and status in LIVE_SUBSCRIPTION_STATUSES:
        return "入库中"
    return _STATUS_LABELS[status]


def can_retry(status: SubscriptionStatus) -> bool:
    return status in {SubscriptionStatus.FAILED, SubscriptionStatus.CANCELLED}


def require_retryable(status: SubscriptionStatus) -> None:
    if not can_retry(status):
        raise DomainError(ErrorCode.INVALID_STATE, "只有失败或已取消的任务可以重试")


class RegistrationOutcome(StrEnum):
    CREATED = "created"
    JOINED_LIVE = "joined_live"
    ATTACHED_COMPLETED = "attached_completed"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class RegistrationDecision:
    outcome: RegistrationOutcome
    should_submit: bool


def decide_registration(
    *,
    attempt_status: AttemptStatus | None,
    subscription_status: SubscriptionStatus | None,
    content_available: bool,
) -> RegistrationDecision:
    if subscription_status in LIVE_SUBSCRIPTION_STATUSES | {SubscriptionStatus.COMPLETED}:
        return RegistrationDecision(RegistrationOutcome.DUPLICATE, False)
    if content_available:
        return RegistrationDecision(RegistrationOutcome.ATTACHED_COMPLETED, False)
    if attempt_status in LIVE_ATTEMPT_STATUSES:
        return RegistrationDecision(RegistrationOutcome.JOINED_LIVE, False)
    return RegistrationDecision(RegistrationOutcome.CREATED, True)


@dataclass(frozen=True, slots=True)
class CancellationDecision:
    release_reserved: int
    stop_attempt: bool
    attempt_status: AttemptStatus | None


def plan_cancellation(
    status: SubscriptionStatus,
    *,
    remaining_subscriptions: int,
    reserved_bytes: int,
) -> CancellationDecision:
    if status not in LIVE_SUBSCRIPTION_STATUSES:
        raise DomainError(ErrorCode.INVALID_STATE, "终态任务不能重复取消")
    if remaining_subscriptions < 0 or reserved_bytes < 0:
        raise DomainError(ErrorCode.INVALID_INPUT, "订阅数量或预留空间无效")
    stop = remaining_subscriptions == 0
    return CancellationDecision(
        release_reserved=reserved_bytes,
        stop_attempt=stop,
        attempt_status=AttemptStatus.CANCELLED if stop else None,
    )


@dataclass(frozen=True, slots=True)
class NormalizedResource:
    kind: str
    canonical_uri: str
    resource_key: str
    display_uri: str


def _invalid_resource(message: str = "下载地址无效") -> DomainError:
    return DomainError(ErrorCode.INVALID_INPUT, message)


def _resource_key(kind: str, canonical_uri: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{canonical_uri}".encode()).hexdigest()
    return f"{kind}:{digest}"


def _normalize_http(uri: str) -> NormalizedResource:
    try:
        parsed = urlsplit(uri)
        host = parsed.hostname
        _ = parsed.port
    except ValueError:
        raise _invalid_resource() from None
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise _invalid_resource("仅支持 HTTP(S) 或 Magnet 下载地址")
    canonical = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
    safe_netloc = f"[{host}]" if ":" in host else host
    if parsed.port is not None:
        safe_netloc = f"{safe_netloc}:{parsed.port}"
    display = urlunsplit((parsed.scheme, safe_netloc, parsed.path or "/", "", ""))
    return NormalizedResource("http", canonical, _resource_key("http", canonical), display)


def _normalize_magnet(uri: str) -> NormalizedResource:
    values = parse_qs(urlsplit(uri).query).get("xt", [])
    info_hash = next(
        (value[9:] for value in values if value.lower().startswith("urn:btih:")),
        None,
    )
    if info_hash is None:
        raise _invalid_resource("Magnet 缺少有效的 info-hash")
    if re.fullmatch(r"[0-9a-fA-F]{40}", info_hash):
        normalized_hash = info_hash.lower()
    elif re.fullmatch(r"[0-9A-Z2-7]{32}", info_hash):
        try:
            normalized_hash = base64.b32decode(info_hash).hex()
        except binascii.Error:
            raise _invalid_resource("Magnet info-hash 无效") from None
    else:
        raise _invalid_resource("Magnet info-hash 无效")
    canonical = f"magnet:?xt=urn:btih:{normalized_hash}"
    return NormalizedResource("magnet", canonical, _resource_key("magnet", canonical), canonical)


def normalize_resource(uri: str) -> NormalizedResource:
    stripped = uri.strip()
    if not stripped or any(ord(character) < 32 for character in stripped):
        raise _invalid_resource()
    try:
        scheme = urlsplit(stripped).scheme.lower()
    except ValueError:
        raise _invalid_resource() from None
    if scheme == "magnet":
        return _normalize_magnet(stripped)
    if scheme == "ftp":
        raise _invalid_resource("不支持 FTP 下载")
    if scheme in {"http", "https"}:
        return _normalize_http(stripped)
    raise _invalid_resource("仅支持 HTTP(S)、Magnet 或种子下载")


def torrent_resource_key(info_hash: str, selected_file_indexes: object) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", info_hash):
        raise DomainError(ErrorCode.INVALID_INPUT, "种子 info-hash 无效")
    normalized_hash = info_hash.lower()
    if selected_file_indexes is None:
        return f"torrent:{normalized_hash}:all"
    if not isinstance(selected_file_indexes, list | tuple):
        raise DomainError(ErrorCode.INVALID_INPUT, "种子文件选择无效")
    indexes = list(selected_file_indexes)
    if not indexes or any(not isinstance(index, int) or index < 1 for index in indexes):
        raise DomainError(ErrorCode.INVALID_INPUT, "种子文件选择无效")
    if len(set(indexes)) != len(indexes):
        raise DomainError(ErrorCode.INVALID_INPUT, "种子文件选择不能重复")
    selection = ",".join(str(index) for index in sorted(indexes))
    return f"torrent:{normalized_hash}:selected:{selection}"


def derive_planned_gid(attempt_id: int, *, pepper: str) -> str:
    if attempt_id <= 0 or not pepper:
        raise DomainError(ErrorCode.INVALID_INPUT, "尝试 ID 或凭证密钥无效")
    return hmac.new(
        pepper.encode(), f"download-attempt:{attempt_id}".encode(), hashlib.sha256
    ).hexdigest()[:16]


def require_gid_match(attempt_id: int, *, expected: str, observed: str) -> None:
    if attempt_id <= 0 or not hmac.compare_digest(expected, observed):
        raise DomainError(ErrorCode.STALE_EVENT, "下载事件属于旧尝试，已忽略")


def reconciled_total_length(current: int | None, observed: int | None) -> int | None:
    if current is not None and current < 0 or observed is not None and observed < 0:
        raise DomainError(ErrorCode.INVALID_INPUT, "下载大小不能为负数")
    if current is None:
        return observed
    if observed is None:
        return current
    return max(current, observed)


def require_orphan_cleanup_safe(*, candidates_complete: bool) -> None:
    if not candidates_complete:
        raise DomainError(ErrorCode.RECOVERY_UNSAFE, "恢复候选不完整，禁止清理孤儿下载")


def submission_pause_options(kind: str) -> dict[str, str]:
    """Return the only admission-pause policy allowed for each source kind."""
    if kind == "http":
        return {"pause": "true", "pause-metadata": "true"}
    if kind == "magnet":
        return {"pause-metadata": "true"}
    if kind == "torrent":
        return {"pause": "true"}
    raise DomainError(ErrorCode.INVALID_INPUT, "下载类型无效")


def normalize_download_options(
    resource: NormalizedResource, options: Mapping[str, object]
) -> tuple[dict[str, object], str]:
    """Validate the small write-option contract and include it in HTTP identity."""
    if any(key not in {"headers", "mirrors"} for key in options):
        raise DomainError(ErrorCode.INVALID_INPUT, "下载选项包含未知字段")
    raw_headers = options.get("headers", {})
    raw_mirrors = options.get("mirrors", [])
    if not isinstance(raw_headers, Mapping) or not isinstance(raw_mirrors, list | tuple):
        raise DomainError(ErrorCode.INVALID_INPUT, "下载请求头或镜像格式无效")
    if resource.kind != "http" and (raw_headers or raw_mirrors):
        raise DomainError(ErrorCode.INVALID_INPUT, "仅 HTTP(S) 下载支持请求头和镜像")
    if len(raw_headers) > 50 or len(raw_mirrors) > 10:
        raise DomainError(ErrorCode.INVALID_INPUT, "下载请求头或镜像数量过多")
    headers: dict[str, str] = {}
    forbidden = {"host", "content-length", "connection", "transfer-encoding", "x-aria2deck-fetch-capability"}
    for raw_name, raw_value in raw_headers.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise DomainError(ErrorCode.INVALID_INPUT, "下载请求头格式无效")
        name = raw_name.strip().lower()
        if (
            not re.fullmatch(r"[a-z0-9!#$%&'*+.^_`|~-]+", name)
            or name in forbidden
            or len(raw_value) > 8192
            or any(ord(character) < 32 or ord(character) == 127 for character in raw_value)
        ):
            raise DomainError(ErrorCode.INVALID_INPUT, "下载请求头无效")
        headers[name] = raw_value
    mirrors: list[str] = []
    for raw in raw_mirrors:
        if not isinstance(raw, str):
            raise DomainError(ErrorCode.INVALID_INPUT, "镜像地址无效")
        mirror = normalize_resource(raw)
        if mirror.kind != "http":
            raise DomainError(ErrorCode.INVALID_INPUT, "镜像仅支持 HTTP(S) 地址")
        if mirror.canonical_uri not in mirrors:
            mirrors.append(mirror.canonical_uri)
    normalized: dict[str, object] = {"headers": headers, "mirrors": mirrors}
    if resource.kind != "http":
        return normalized, resource.resource_key
    identity = json.dumps(
        {"uri": resource.canonical_uri, **normalized},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return normalized, f"http:{hashlib.sha256(identity.encode()).hexdigest()}"


def resource_display_name(resource: NormalizedResource, original_uri: str) -> str:
    if resource.kind == "http":
        candidate = unquote(urlsplit(resource.canonical_uri).path).rsplit("/", 1)[-1]
    elif resource.kind == "magnet":
        values = parse_qs(urlsplit(original_uri).query).get("dn", [])
        candidate = values[0] if values else resource.canonical_uri.rsplit(":", 1)[-1]
    else:
        candidate = "download"
    candidate = candidate.strip() or "download"
    if len(candidate) > 200 or any(
        character in candidate for character in ("/", chr(92), chr(0))
    ) or any(ord(character) < 32 for character in candidate):
        raise DomainError(ErrorCode.INVALID_INPUT, "下载名称无效")
    return candidate
