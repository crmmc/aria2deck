from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ..domain.accounts import ApiToken, Session, User
from ..domain.quota import Quota
from ..domain.settings import SystemSettings


class UnitOfWork(Protocol):
    @property
    def connection(self) -> object: ...


class CredentialPort(Protocol):
    def new_session_id(self) -> str: ...

    def new_api_token(self) -> str: ...

    def new_rpc_secret(self) -> str: ...

    def digest_session(self, session_id: str) -> str: ...

    def digest_api_token(self, token: str) -> str: ...

    def derive_password_hash(self, *, username: str, password: str) -> str: ...

    def hash_password(self, password_hash: str) -> str: ...

    def verify_password(self, password_hash: str, stored: str) -> bool: ...

    def encrypt(self, value: str) -> str: ...

    def decrypt(self, value: str) -> str: ...


class AccountRepositoryPort(Protocol):
    async def count_users(self) -> int: ...

    async def count_admins(self) -> int: ...

    async def create_user(
        self,
        *,
        username: str,
        password_hash: str,
        is_admin: bool,
        is_initial_password: bool,
        quota_bytes: int | None,
        now_ms: int,
    ) -> int: ...

    async def get_user(self, user_id: int) -> User: ...

    async def find_user_by_username(self, username: str) -> User | None: ...

    async def list_users(self, *, offset: int, limit: int) -> list[User]: ...

    async def get_quota(self, user_id: int) -> Quota: ...

    async def update_user(
        self,
        *,
        user_id: int,
        username: str,
        username_key: str,
        password_hash: str,
        is_admin: bool,
        quota_bytes: int,
        is_initial_password: bool,
        now_ms: int,
    ) -> None: ...

    async def change_password(
        self,
        *,
        user_id: int,
        expected_generation: int,
        password_hash: str,
        is_initial_password: bool,
        now_ms: int,
    ) -> bool: ...

    async def mark_pending_delete(self, *, user_id: int, now_ms: int) -> None: ...

    async def list_pending_users(self) -> list[int]: ...

    async def hard_delete_user(self, user_id: int) -> bool: ...

    async def create_session(
        self,
        *,
        session_id: str,
        user_id: int,
        credential_generation: int,
        now_ms: int,
        expires_at_ms: int,
    ) -> None: ...

    async def get_session_user(self, session_id: str) -> tuple[Session, User] | None: ...

    async def revoke_session(self, *, session_id: str, now_ms: int) -> None: ...

    async def revoke_user_sessions(self, *, user_id: int, now_ms: int) -> None: ...


    async def get_api_token_user(self, token_digest: str) -> tuple[ApiToken, User] | None: ...


    async def count_api_tokens(self, user_id: int) -> int: ...

    async def touch_api_token(self, *, token_id: int, now_ms: int) -> None: ...


    async def list_rpc_enabled_users(self) -> list[User]: ...

    async def set_rpc_secret(
        self,
        *,
        user_id: int,
        ciphertext: str | None,
        prefix: str | None,
        now_ms: int,
    ) -> None: ...

    async def invalidate_credentials(self) -> tuple[int, int]: ...


class SettingsRepositoryPort(Protocol):
    async def get_settings(self) -> SystemSettings: ...

    async def save_settings(self, settings: SystemSettings) -> None: ...


class UserCleanupPort(Protocol):
    async def cleanup_user(self, user_id: int) -> None: ...


@dataclass(frozen=True, slots=True)
class SessionRevocation:
    """会话撤销通知：user 级（session_id=None）或精确会话。"""

    user_id: int
    session_id: str | None = None


class SessionRevocationBus(Protocol):
    """共享内核端口：accounts 发布撤销，WS 等长连接方订阅后立即关闭。

    shortcut: 进程内同步分发；多实例部署需替换为共享通道（与 WS 限额同一升级点）。
    """

    def publish(self, revocation: SessionRevocation) -> None: ...

    def subscribe(self, callback: Callable[[SessionRevocation], None]) -> None: ...

    def unsubscribe(self, callback: Callable[[SessionRevocation], None]) -> None: ...
