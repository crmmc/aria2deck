from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from .errors import DomainError, ErrorCode

DEFAULT_QUOTA_BYTES = 100 * 1024**3
CREDENTIAL_INVALIDATION_CONFIRMATION = "INVALIDATE_ALL_CREDENTIALS"


class Role(StrEnum):
    USER = "user"
    ADMIN = "admin"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    FROZEN = "frozen"
    LOCKED = "locked"


# 普通用户编辑接口只能在这两种状态间切换；锁定只由迁移与结构升级写入。
PATCHABLE_ACCOUNT_STATUSES = frozenset({AccountStatus.ACTIVE, AccountStatus.FROZEN})

_LOGIN_STATUS_ERRORS: dict[AccountStatus, tuple[ErrorCode, str]] = {
    AccountStatus.FROZEN: (ErrorCode.ACCOUNT_FROZEN, "该用户已冻结，详情请联系管理员"),
    AccountStatus.LOCKED: (ErrorCode.ACCOUNT_LOCKED, "该用户已锁定，请联系管理员轮换新的密码"),
}


class CredentialKind(StrEnum):
    SESSION = "session"
    API_TOKEN = "api_token"  # noqa: S105  # 凭证类型标识，不是密钥
    RPC_SECRET = "rpc_secret"  # noqa: S105  # 凭证类型标识，不是密钥


class Permission(StrEnum):
    ACCESS_OWN_DATA = "access_own_data"
    MANAGE_USERS = "manage_users"
    MANAGE_SETTINGS = "manage_settings"
    MANAGE_STORAGE = "manage_storage"
    PURGE_HISTORY = "purge_history"
    INVALIDATE_CREDENTIALS = "invalidate_credentials"


_ADMIN_PERMISSIONS = frozenset(
    {
        Permission.MANAGE_USERS,
        Permission.MANAGE_SETTINGS,
        Permission.MANAGE_STORAGE,
        Permission.PURGE_HISTORY,
        Permission.INVALIDATE_CREDENTIALS,
    }
)


def authorize(role: Role, permission: Permission) -> None:
    allowed = permission is Permission.ACCESS_OWN_DATA or (
        role is Role.ADMIN and permission in _ADMIN_PERMISSIONS
    )
    if not allowed:
        raise DomainError(ErrorCode.PERMISSION_DENIED, "没有执行该操作的权限")


@dataclass(frozen=True, slots=True)
class User:
    id: int
    username: str
    username_key: str
    password_hash: str = field(repr=False)
    status: AccountStatus
    is_admin: bool
    quota_bytes: int
    is_initial_password: bool
    pending_delete: bool
    credential_generation: int
    created_at_ms: int
    updated_at_ms: int
    rpc_secret_ciphertext: str | None = field(default=None, repr=False)
    rpc_secret_prefix: str | None = None

    @property
    def role(self) -> Role:
        return Role.ADMIN if self.is_admin else Role.USER


@dataclass(frozen=True, slots=True)
class Session:
    id: str = field(repr=False)
    user_id: int
    credential_generation: int
    created_at_ms: int
    expires_at_ms: int
    revoked_at_ms: int | None


@dataclass(frozen=True, slots=True)
class ApiToken:
    id: int
    user_id: int
    name: str
    prefix: str
    created_at_ms: int
    last_used_at_ms: int | None


@dataclass(frozen=True, slots=True)
class NewUser:
    username: str
    username_key: str
    quota_bytes: int
    is_admin: bool


def validate_new_user(
    username: str, quota_bytes: int | None, is_admin: bool, existing_users: int
) -> NewUser:
    normalized = username.strip()
    if not 1 <= len(normalized) <= 64 or any(
        character in normalized for character in ("/", "\\", "\x00")
    ):
        raise DomainError(ErrorCode.INVALID_INPUT, "用户名长度或内容无效")
    if existing_users < 0:
        raise DomainError(ErrorCode.INVALID_INPUT, "用户数量不能为负数")
    if existing_users == 0 and not is_admin:
        raise DomainError(ErrorCode.PERMISSION_DENIED, "首个用户必须是管理员")
    quota = DEFAULT_QUOTA_BYTES if quota_bytes is None else quota_bytes
    if quota < 0:
        raise DomainError(ErrorCode.INVALID_INPUT, "用户配额不能为负数")
    return NewUser(normalized, normalized.casefold(), quota, is_admin or existing_users == 0)


def ensure_user_delete(
    *,
    actor_id: int,
    target_id: int,
    actor_role: Role,
    target_is_admin: bool = False,
    admin_count: int = 1,
) -> None:
    authorize(actor_role, Permission.MANAGE_USERS)
    if actor_id == target_id:
        raise DomainError(ErrorCode.SELF_DELETE, "不能删除当前登录用户")
    if target_is_admin and admin_count <= 1:
        raise DomainError(ErrorCode.LAST_ADMIN, "不能删除最后一名管理员")


def ensure_user_role_change(
    *,
    actor_id: int,
    target_id: int,
    actor_role: Role,
    current_is_admin: bool,
    new_is_admin: bool,
    admin_count: int,
) -> None:
    authorize(actor_role, Permission.MANAGE_USERS)
    if admin_count < 1:
        raise DomainError(ErrorCode.INVALID_STATE, "管理员数量账目无效")
    if actor_id == target_id and current_is_admin and not new_is_admin:
        raise DomainError(ErrorCode.PERMISSION_DENIED, "不能取消自己的管理员权限")
    if current_is_admin and not new_is_admin and admin_count == 1:
        raise DomainError(ErrorCode.LAST_ADMIN, "不能移除最后一名管理员")


def can_finalize_user_deletion(*, active_tasks: int, file_references: int, active_packs: int) -> None:
    if min(active_tasks, file_references, active_packs) < 0:
        raise DomainError(ErrorCode.INVALID_INPUT, "用户引用数量不能为负数")
    if active_tasks or file_references or active_packs:
        raise DomainError(ErrorCode.CONTENT_BUSY, "用户仍有未清理的任务、文件或打包")


def api_token_allowed(*, is_admin: bool, pending_delete: bool, status: AccountStatus) -> bool:
    return not is_admin and not pending_delete and status is AccountStatus.ACTIVE


def session_is_active(
    *,
    expires_at_ms: int,
    now_ms: int,
    revoked_at_ms: int | None,
    pending_delete: bool,
    status: AccountStatus,
) -> bool:
    return (
        revoked_at_ms is None
        and not pending_delete
        and status is AccountStatus.ACTIVE
        and now_ms < expires_at_ms
    )


def require_login_allowed(status: AccountStatus) -> None:
    """冻结与锁定在密码校验之前拒绝；普通密码错误仍由调用方给出通用 401。"""
    detail = _LOGIN_STATUS_ERRORS.get(status)
    if detail is not None:
        raise DomainError(*detail)


def status_after_password_reset(current: AccountStatus) -> AccountStatus:
    """改密与随机重置共用的状态转换：锁定解锁、冻结保持冻结、正常保持正常。"""
    return AccountStatus.ACTIVE if current is AccountStatus.LOCKED else current


def ensure_user_status_change(
    *,
    actor_id: int,
    target_id: int,
    actor_role: Role,
    current: AccountStatus,
    new: AccountStatus,
    target_is_admin: bool,
    active_admin_count: int,
) -> None:
    authorize(actor_role, Permission.MANAGE_USERS)
    if new not in PATCHABLE_ACCOUNT_STATUSES:
        raise DomainError(ErrorCode.INVALID_INPUT, "账号状态只能改为正常或冻结")
    if current is AccountStatus.LOCKED:
        raise DomainError(ErrorCode.INVALID_STATE, "锁定账号需要通过重置密码解锁")
    if current is new:
        raise DomainError(ErrorCode.INVALID_STATE, "账号状态未发生变化")
    if new is AccountStatus.ACTIVE:
        return
    if actor_id == target_id:
        raise DomainError(ErrorCode.PERMISSION_DENIED, "不能冻结当前登录用户")
    if target_is_admin and active_admin_count <= 1:
        raise DomainError(ErrorCode.LAST_ADMIN, "不能冻结最后一名可用管理员")


def require_credential_invalidation(confirmation: str) -> None:
    if confirmation != CREDENTIAL_INVALIDATION_CONFIRMATION:
        raise DomainError(ErrorCode.INVALID_INPUT, "凭证作废确认字符串不正确")


def validate_password_change(
    *, is_initial: bool, old_password_matches: bool, new_password_hash: str
) -> None:
    if not is_initial and not old_password_matches:
        raise DomainError(ErrorCode.CREDENTIAL_INVALID, "当前密码不正确")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", new_password_hash):
        raise DomainError(ErrorCode.INVALID_INPUT, "新密码摘要格式无效")


def require_password_generation(*, expected: int, current: int) -> None:
    if expected != current:
        raise DomainError(ErrorCode.INVALID_STATE, "密码已被其他请求修改，请重试")



def require_session(kind: CredentialKind) -> None:
    if kind is not CredentialKind.SESSION:
        raise DomainError(ErrorCode.PERMISSION_DENIED, "该操作仅支持浏览器会话登录")


def authorize_admin_session(role: Role, kind: CredentialKind, permission: Permission) -> None:
    require_session(kind)
    authorize(role, permission)


def normalize_username(username: str) -> tuple[str, str]:
    normalized = username.strip()
    if (
        not 1 <= len(normalized) <= 64
        or "/" in normalized
        or chr(92) in normalized
        or chr(0) in normalized
    ):
        raise DomainError(ErrorCode.INVALID_INPUT, "用户名长度或内容无效")
    return normalized, normalized.casefold()


def ensure_quota_change(*, quota_bytes: int, used_bytes: int, reserved_bytes: int) -> None:
    if quota_bytes < 0:
        raise DomainError(ErrorCode.INVALID_INPUT, "用户配额不能为负数")
    if quota_bytes < used_bytes + reserved_bytes:
        raise DomainError(ErrorCode.QUOTA_EXCEEDED, "用户配额不能小于当前已用和预留空间")


def ensure_username_change(*, username_changed: bool, password_hash_supplied: bool) -> None:
    if username_changed and not password_hash_supplied:
        raise DomainError(ErrorCode.INVALID_INPUT, "修改用户名时必须同时提供新密码")


def validate_password_hash(password_hash: str) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{64}", password_hash):
        raise DomainError(ErrorCode.INVALID_INPUT, "密码摘要格式无效")


def validate_api_token_name(name: str) -> str:
    normalized = name.strip() or "默认 Token"
    if len(normalized) > 100 or chr(0) in normalized:
        raise DomainError(ErrorCode.INVALID_INPUT, "Token 名称长度或内容无效")
    return normalized
