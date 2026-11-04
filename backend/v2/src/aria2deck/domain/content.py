from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum

from .errors import DomainError, ErrorCode


class ContentKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"


@dataclass(frozen=True, slots=True)
class ContentIdentity:
    kind: ContentKind
    digest: str

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.digest}"


@dataclass(frozen=True, slots=True)
class ContentEntryIdentity:
    path: str
    kind: ContentKind
    size: int
    digest: str | None = None


def directory_content_digest(entries: list[ContentEntryIdentity]) -> str:
    """Hash the sorted directory manifest; file bytes already own their digests."""
    manifest: list[dict[str, object]] = []
    for entry in sorted(entries, key=lambda item: item.path):
        path = normalize_relative_path(entry.path)
        if entry.size < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "目录内容大小无效")
        if entry.kind is ContentKind.FILE:
            identity = content_identity(entry.kind, entry.digest or "")
            digest: str | None = identity.digest
        elif entry.digest is None:
            digest = None
        else:
            raise DomainError(ErrorCode.INVALID_INPUT, "目录条目不能携带文件摘要")
        manifest.append(
            {"path": path, "kind": entry.kind.value, "size": entry.size, "digest": digest}
        )
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def content_identity(kind: ContentKind, digest: str) -> ContentIdentity:
    normalized = digest.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise DomainError(ErrorCode.INVALID_INPUT, "内容摘要必须是 SHA-256")
    return ContentIdentity(kind, normalized)


def validate_display_name(name: str) -> str:
    if not 1 <= len(name) <= 200 or name in {".", ".."}:
        raise DomainError(ErrorCode.INVALID_INPUT, "文件名长度或内容无效")
    if any(character in name for character in ("/", "\\", "\x00")):
        raise DomainError(ErrorCode.INVALID_INPUT, "文件名不能包含路径分隔符")
    return name


def normalize_relative_path(path: str, *, allow_root: bool = False) -> str:
    if path == "" and allow_root:
        return ""
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise DomainError(ErrorCode.INVALID_INPUT, "内容路径必须是明确的相对路径")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise DomainError(ErrorCode.INVALID_INPUT, "内容路径包含非法层级")
    for part in parts:
        validate_display_name(part)
    return "/".join(parts)


def join_relative_path(parent: str, name: str) -> str:
    normalized_parent = normalize_relative_path(parent, allow_root=True)
    normalized_name = validate_display_name(name)
    return f"{normalized_parent}/{normalized_name}" if normalized_parent else normalized_name


class ContentDeletionState(StrEnum):
    ACTIVE = "active"
    PENDING_DELETE = "pending_delete"
    TOMBSTONED = "tombstoned"


@dataclass(frozen=True, slots=True)
class ReferenceRemovalDecision:
    used_released: bool
    schedule_physical_delete: bool


def plan_reference_removal(
    *, reference_count: int, pending_pack_sources: bool
) -> ReferenceRemovalDecision:
    if reference_count < 1:
        raise DomainError(ErrorCode.INVALID_STATE, "内容引用数量不一致")
    last_reference = reference_count == 1
    if last_reference and pending_pack_sources:
        raise DomainError(ErrorCode.CONTENT_BUSY, "内容仍被打包任务引用")
    return ReferenceRemovalDecision(used_released=True, schedule_physical_delete=last_reference)


def require_physical_deletable(
    state: ContentDeletionState, reference_count: int, read_leases: int
) -> None:
    if state is not ContentDeletionState.PENDING_DELETE:
        raise DomainError(ErrorCode.INVALID_STATE, "内容尚未进入待删除状态")
    if reference_count != 0 or read_leases != 0:
        raise DomainError(ErrorCode.CONTENT_BUSY, "内容仍有引用或读取租约")
