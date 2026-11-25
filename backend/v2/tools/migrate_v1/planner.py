"""迁移计划：冲突检测、跳过决策与 V2 写入计划（纯函数，不连数据库）。

只读文件系统元数据（`lstat` / 目录遍历），不读文件内容——内容摘要由
`contents.py` 在一次读盘里同时算出，避免同一批数据读两遍。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from aria2deck.domain.content import ContentEntryIdentity, validate_display_name
from aria2deck.domain.errors import DomainError

from . import MigrationAbort
from .reader import V1Snapshot, V1StoredFile

PLACEHOLDER_PASSWORD = "!migrated-placeholder!"  # noqa: S105  # 占位串，不是密钥
SHARE_LIMIT_MIN = 1
SHARE_LIMIT_MAX = 10000


@dataclass(frozen=True, slots=True)
class Skip:
    """一条被跳过的记录：类别、可读标签、理由。"""

    kind: str
    label: str
    reason: str


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    relative_path: str
    is_directory: bool
    size: int


@dataclass(frozen=True, slots=True)
class UserPlan:
    v1_id: int
    username: str
    username_key: str
    is_admin: bool
    quota_bytes: int
    created_at_ms: int
    updated_at_ms: int
    # 只有拿可登录密码的那个管理员为 True；其余账号写占位串，改密后才能真正登录
    accepts_generated_password: bool = False
    # V1 里一个管理员都没有时，被提升为管理员的那个账号
    promoted_to_admin: bool = False


@dataclass(frozen=True, slots=True)
class StoredFilePlan:
    v1_id: int
    source: Path
    is_directory: bool
    directory_entries: tuple[DirectoryEntry, ...]
    estimated_size: int
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class UserFilePlan:
    v1_id: int
    v1_user_id: int
    v1_stored_file_id: int
    display_name: str
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class SharePlan:
    v1_id: int
    code: str
    owner_v1_id: int
    user_file_v1_id: int
    expires_at_ms: int | None
    max_downloads: int | None
    download_count: int
    revoked_at_ms: int | None
    created_at_ms: int
    last_accessed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class Plan:
    users: tuple[UserPlan, ...]
    stored_files: tuple[StoredFilePlan, ...]
    user_files: tuple[UserFilePlan, ...]
    shares: tuple[SharePlan, ...]
    skipped: tuple[Skip, ...]
    dropped_share_passwords: tuple[str, ...]
    unregistered_store_files: int
    planned_bytes: int


@dataclass(frozen=True, slots=True)
class MigratedContent:
    """一份已搬到 V2 磁盘并按 V2 算法算出摘要的内容。"""

    content_key: str
    kind: str
    digest: str
    size: int
    storage_path: str
    entries: tuple[ContentEntryIdentity, ...]
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class ContentMigration:
    objects: tuple[MigratedContent, ...]
    by_stored_file: dict[int, MigratedContent]
    failures: tuple[tuple[int, str], ...]


@dataclass(frozen=True, slots=True)
class V2UserFilePlan:
    v1_id: int
    v1_user_id: int
    display_name: str
    content_key: str
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class V2SharePlan:
    code: str
    owner_v1_id: int
    user_file_v1_id: int
    file_name: str
    file_size: int
    file_kind: str
    content_key: str
    expires_at_ms: int | None
    max_downloads: int | None
    download_count: int
    revoked_at_ms: int | None
    created_at_ms: int
    last_accessed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class WritePlan:
    users: tuple[UserPlan, ...]
    contents: tuple[MigratedContent, ...]
    user_files: tuple[V2UserFilePlan, ...]
    shares: tuple[V2SharePlan, ...]
    used_bytes: dict[int, int]
    skipped: tuple[Skip, ...]
    dropped_share_passwords: tuple[str, ...]


def username_key(username: str) -> str:
    """与 V2 登录查询同一口径（`find_user_by_username` 用 casefold）。"""
    return username.strip().casefold()


def content_key(kind: str, digest: str) -> str:
    return f"{kind}:{digest}"


def storage_path(kind: str, digest: str) -> str:
    return f"objects/{kind}-{digest}"


def _conflict_lines(groups: dict[str, list[str]]) -> list[str]:
    return [
        f"{name}: {', '.join(members)}"
        for name, members in sorted(groups.items())
        if len(members) > 1
    ]


def _plan_users(
    snapshot: V1Snapshot,
) -> tuple[tuple[UserPlan, ...], dict[int, str], list[Skip]]:
    kept: list[UserPlan] = []
    skipped: list[Skip] = []
    reasons: dict[int, str] = {}
    groups: dict[str, list[str]] = {}
    for user in snapshot.users:
        label = f"账号 {user.username} (id={user.id})"
        if user.pending_delete:
            skipped.append(Skip("user", label, "待删除状态"))
            reasons[user.id] = "待删除状态"
            continue
        normalized = user.username.strip()
        if not 1 <= len(normalized) <= 64 or any(
            character in normalized for character in ("/", "\\", "\x00")
        ):
            raise MigrationAbort(
                "存在 V2 用户名规则不接受的老账号，需人工处理后重试", (label,)
            )
        if user.quota_bytes < 0:
            raise MigrationAbort("存在配额为负数的老账号，需人工处理后重试", (label,))
        key = username_key(user.username)
        groups.setdefault(key, []).append(label)
        kept.append(
            UserPlan(
                v1_id=user.id,
                username=normalized,
                username_key=key,
                is_admin=user.is_admin,
                quota_bytes=user.quota_bytes,
                created_at_ms=user.created_at_ms,
                updated_at_ms=user.updated_at_ms,
            )
        )
    conflicts = _conflict_lines(groups)
    if conflicts:
        raise MigrationAbort(
            "用户名大小写冲突：V2 的 username_key 唯一约束无法同时容纳这些账号",
            tuple(conflicts),
        )
    return _with_bootstrap_admin(kept), reasons, skipped


def _with_bootstrap_admin(users: list[UserPlan]) -> tuple[UserPlan, ...]:
    """挑一个管理员拿可登录的随机密码，其余账号保持占位串。

    迁移后 V2 里必须有一个人能登录，否则没人在「用户管理」页给其他账号设
    临时密码——`ensure_initial_admin` 见到库里有用户就直接跳过，没有第二个
    入口。优先取最早的管理员；一个管理员都没有时取最早的账号并提升为管理员。
    """
    if not users:
        return ()
    admins = [user for user in users if user.is_admin]
    chosen = min(admins or users, key=lambda user: user.v1_id)
    promote = not chosen.is_admin
    return tuple(
        UserPlan(
            v1_id=user.v1_id,
            username=user.username,
            username_key=user.username_key,
            is_admin=user.is_admin or (promote and user.v1_id == chosen.v1_id),
            quota_bytes=user.quota_bytes,
            created_at_ms=user.created_at_ms,
            updated_at_ms=user.updated_at_ms,
            accepts_generated_password=user.v1_id == chosen.v1_id,
            promoted_to_admin=promote and user.v1_id == chosen.v1_id,
        )
        for user in users
    )


def _resolve_source(real_path: str, download_directory: Path) -> Path | None:
    """把 V1 的 real_path 解析成绝对路径；越出下载目录即拒绝（防路径穿越）。"""
    if not real_path.strip():
        return None
    candidate = Path(os.path.normpath(os.path.join(str(download_directory), real_path)))
    if candidate == download_directory or not candidate.is_relative_to(download_directory):
        return None
    return candidate


class _DirectoryRejected(Exception):
    pass


def _scan_directory(source: Path) -> tuple[DirectoryEntry, ...]:
    """按 V2 `_directory_entries` 的防线遍历目录，只读元数据不读内容。"""
    entries: list[DirectoryEntry] = []
    for parent, directories, files in os.walk(source, followlinks=False):
        parent_path = Path(parent)
        for name in directories:
            item = parent_path / name
            if item.is_symlink():
                raise _DirectoryRejected("目录内容含符号链接")
            entries.append(DirectoryEntry(item.relative_to(source).as_posix(), True, 0))
        for name in files:
            item = parent_path / name
            if item.is_symlink():
                raise _DirectoryRejected("目录内容含符号链接")
            if not item.is_file():
                raise _DirectoryRejected("目录内有非普通文件")
            if name.endswith(".aria2"):
                raise _DirectoryRejected("目录内残留 .aria2 控制文件")
            entries.append(
                DirectoryEntry(item.relative_to(source).as_posix(), False, item.stat().st_size)
            )
    entries.sort(key=lambda entry: entry.relative_path)
    return tuple(entries)


def _inspect_stored_file(
    stored: V1StoredFile, download_directory: Path
) -> StoredFilePlan | Skip:
    """判定单条 V1 内容能否迁移；能则给出计划，否则给出跳过理由。"""
    label = f"内容 id={stored.id} {stored.real_path}"
    if stored.pending_delete:
        return Skip("content", label, "软删除中")
    source = _resolve_source(stored.real_path, download_directory)
    if source is None:
        return Skip("content", label, "路径不在 V1 下载目录内")
    if source.is_symlink():
        return Skip("content", label, "符号链接，不跟随")
    if not source.exists():
        return Skip("content", label, "磁盘文件缺失")
    if stored.is_directory:
        if not source.is_dir():
            return Skip("content", label, "记录为目录但磁盘上不是目录")
        try:
            entries = _scan_directory(source)
        except _DirectoryRejected as error:
            return Skip("content", label, str(error))
        return StoredFilePlan(
            v1_id=stored.id,
            source=source,
            is_directory=True,
            directory_entries=entries,
            estimated_size=sum(entry.size for entry in entries),
            created_at_ms=stored.created_at_ms,
        )
    if not source.is_file():
        return Skip("content", label, "记录为文件但磁盘上不是普通文件")
    return StoredFilePlan(
        v1_id=stored.id,
        source=source,
        is_directory=False,
        directory_entries=(),
        estimated_size=source.stat().st_size,
        created_at_ms=stored.created_at_ms,
    )


def _plan_stored_files(
    snapshot: V1Snapshot, download_directory: Path
) -> tuple[tuple[StoredFilePlan, ...], dict[int, str], list[Skip], int]:
    kept: list[StoredFilePlan] = []
    skipped: list[Skip] = []
    reasons: dict[int, str] = {}
    planned_bytes = 0
    for stored in snapshot.stored_files:
        inspected = _inspect_stored_file(stored, download_directory)
        if isinstance(inspected, Skip):
            skipped.append(inspected)
            reasons[stored.id] = inspected.reason
            continue
        kept.append(inspected)
        planned_bytes += inspected.estimated_size
    return tuple(kept), reasons, skipped, planned_bytes


def _plan_user_files(
    snapshot: V1Snapshot,
    user_ids: set[int],
    stored_ids: set[int],
    user_reasons: dict[int, str],
    stored_reasons: dict[int, str],
) -> tuple[tuple[UserFilePlan, ...], dict[int, str], list[Skip]]:
    kept: list[UserFilePlan] = []
    skipped: list[Skip] = []
    reasons: dict[int, str] = {}
    groups: dict[str, list[str]] = {}
    for row in snapshot.user_files:
        label = f"用户文件 id={row.id}（{row.display_name}）"
        reason: str | None = None
        if row.user_id in user_reasons:
            reason = f"所属账号已跳过：{user_reasons[row.user_id]}"
        elif row.stored_file_id in stored_reasons:
            reason = f"内容已跳过：{stored_reasons[row.stored_file_id]}"
        elif row.user_id not in user_ids or row.stored_file_id not in stored_ids:
            reason = "引用了不存在的账号或内容"
        if reason is not None:
            skipped.append(Skip("user_file", label, reason))
            reasons[row.id] = reason
            continue
        try:
            display_name = validate_display_name(row.display_name)
        except DomainError:
            raise MigrationAbort(
                "存在 V2 文件命名规则不接受的显示名，需人工处理后重试", (label,)
            ) from None
        groups.setdefault(f"账号 id={row.user_id} 的文件名 {display_name}", []).append(label)
        kept.append(
            UserFilePlan(
                v1_id=row.id,
                v1_user_id=row.user_id,
                v1_stored_file_id=row.stored_file_id,
                display_name=display_name,
                created_at_ms=row.created_at_ms,
            )
        )
    conflicts = _conflict_lines(groups)
    if conflicts:
        raise MigrationAbort(
            "同一账号下文件名重复：V2 的 uq_user_files_user_name 无法容纳", tuple(conflicts)
        )
    return tuple(kept), reasons, skipped


def _plan_shares(
    snapshot: V1Snapshot,
    user_ids: set[int],
    user_files: dict[int, UserFilePlan],
    user_reasons: dict[int, str],
    user_file_reasons: dict[int, str],
) -> tuple[tuple[SharePlan, ...], list[Skip], tuple[str, ...]]:
    kept: list[SharePlan] = []
    skipped: list[Skip] = []
    dropped: list[str] = []
    seen_codes: set[str] = set()
    for share in snapshot.share_links:
        label = f"分享 {share.share_code}"
        reason: str | None = None
        if share.owner_id in user_reasons:
            reason = f"所属账号已跳过：{user_reasons[share.owner_id]}"
        elif share.user_file_id in user_file_reasons:
            reason = "所指文件已跳过"
        elif share.owner_id not in user_ids or share.user_file_id not in user_files:
            reason = "引用了不存在的账号或文件"
        elif user_files[share.user_file_id].v1_user_id != share.owner_id:
            reason = "归属账号与文件归属不一致"
        elif share.max_downloads is not None and not (
            SHARE_LIMIT_MIN <= share.max_downloads <= SHARE_LIMIT_MAX
        ):
            reason = "下载次数上限超出 V2 允许范围（1..10000）"
        elif share.download_count < 0 or (
            share.max_downloads is not None and share.download_count > share.max_downloads
        ):
            reason = "已下载次数超出上限，V2 约束无法表示"
        if reason is not None:
            skipped.append(Skip("share", label, reason))
            continue
        if share.share_code in seen_codes:
            raise MigrationAbort(
                "分享码重复：V2 的 uq_share_links_code 无法容纳", (label,)
            )
        seen_codes.add(share.share_code)
        if share.has_password:
            dropped.append(share.share_code)
        kept.append(
            SharePlan(
                v1_id=share.id,
                code=share.share_code,
                owner_v1_id=share.owner_id,
                user_file_v1_id=share.user_file_id,
                expires_at_ms=share.expires_at_ms,
                max_downloads=share.max_downloads,
                download_count=share.download_count,
                revoked_at_ms=None if share.status == "active" else share.created_at_ms,
                created_at_ms=share.created_at_ms,
                last_accessed_at_ms=share.last_accessed_at_ms,
            )
        )
    return tuple(kept), skipped, tuple(dropped)


def count_unregistered_store_files(
    registered_paths: set[str],
    registered_directories: tuple[str, ...],
    store_directory: Path,
) -> int:
    """统计 `store/` 下未被 V1 记录覆盖的普通文件；只作参考，不迁移。"""
    if not store_directory.is_dir():
        return 0
    unregistered = 0
    for parent, _directories, files in os.walk(store_directory, followlinks=False):
        for name in files:
            path = os.path.normpath(os.path.join(parent, name))
            if path in registered_paths:
                continue
            covered = any(
                path == directory or path.startswith(f"{directory}{os.sep}")
                for directory in registered_directories
            )
            if not covered:
                unregistered += 1
    return unregistered


def build_plan(snapshot: V1Snapshot, download_dir: Path) -> Plan:
    """把 V1 快照折叠成迁移计划；任何冲突都抛 `MigrationAbort`，绝不自动改名。"""
    download_directory = Path(os.path.abspath(download_dir))
    users, user_reasons, user_skips = _plan_users(snapshot)
    stored_files, stored_reasons, stored_skips, planned_bytes = _plan_stored_files(
        snapshot, download_directory
    )
    user_files, user_file_reasons, user_file_skips = _plan_user_files(
        snapshot,
        {user.v1_id for user in users},
        {stored.v1_id for stored in stored_files},
        user_reasons,
        stored_reasons,
    )
    shares, share_skips, dropped = _plan_shares(
        snapshot,
        {user.v1_id for user in users},
        {row.v1_id: row for row in user_files},
        user_reasons,
        user_file_reasons,
    )
    return Plan(
        users=users,
        stored_files=stored_files,
        user_files=user_files,
        shares=shares,
        skipped=(*user_skips, *stored_skips, *user_file_skips, *share_skips),
        dropped_share_passwords=dropped,
        unregistered_store_files=count_unregistered_store_files(
            {
                os.path.normpath(stored.real_path)
                for stored in snapshot.stored_files
                if not stored.is_directory
            },
            tuple(
                os.path.normpath(stored.real_path)
                for stored in snapshot.stored_files
                if stored.is_directory
            ),
            download_directory / "store",
        ),
        planned_bytes=planned_bytes,
    )


def finalize(plan: Plan, migration: ContentMigration) -> WritePlan:
    """把计划与内容搬迁结果对齐：级联跳过、按 V2 口径重算配额用量。"""
    skipped = list(plan.skipped)
    failure_reasons = dict(migration.failures)
    user_files: list[V2UserFilePlan] = []
    used: dict[int, int] = {user.v1_id: 0 for user in plan.users}
    for row in plan.user_files:
        content = migration.by_stored_file.get(row.v1_stored_file_id)
        if content is None:
            skipped.append(
                Skip(
                    "user_file",
                    f"用户文件 id={row.v1_id}（{row.display_name}）",
                    failure_reasons.get(row.v1_stored_file_id, "内容迁移失败"),
                )
            )
            continue
        user_files.append(
            V2UserFilePlan(
                v1_id=row.v1_id,
                v1_user_id=row.v1_user_id,
                display_name=row.display_name,
                content_key=content.content_key,
                created_at_ms=row.created_at_ms,
            )
        )
        used[row.v1_user_id] = used[row.v1_user_id] + content.size
    contents = {item.content_key: item for item in migration.objects}
    by_user_file = {row.v1_id: row for row in user_files}
    shares: list[V2SharePlan] = []
    for share in plan.shares:
        owner_row = by_user_file.get(share.user_file_v1_id)
        if owner_row is None:
            skipped.append(Skip("share", f"分享 {share.code}", "所指文件已跳过"))
            continue
        content = contents[owner_row.content_key]
        shares.append(
            V2SharePlan(
                code=share.code,
                owner_v1_id=share.owner_v1_id,
                user_file_v1_id=share.user_file_v1_id,
                file_name=owner_row.display_name,
                file_size=content.size,
                file_kind=content.kind,
                content_key=content.content_key,
                expires_at_ms=share.expires_at_ms,
                max_downloads=share.max_downloads,
                download_count=share.download_count,
                revoked_at_ms=share.revoked_at_ms,
                created_at_ms=share.created_at_ms,
                last_accessed_at_ms=share.last_accessed_at_ms,
            )
        )
    return WritePlan(
        users=plan.users,
        contents=migration.objects,
        user_files=tuple(user_files),
        shares=tuple(shares),
        used_bytes=used,
        skipped=tuple(skipped),
        dropped_share_passwords=plan.dropped_share_passwords,
    )
