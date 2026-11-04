from __future__ import annotations

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

    async def create_api_token(
        self,
        *,
        user_id: int,
        name: str,
        token_digest: str,
        prefix: str,
        now_ms: int,
    ) -> int: ...

    async def get_api_token_user(self, token_digest: str) -> tuple[ApiToken, User] | None: ...

    async def list_api_tokens(self, *, user_id: int, offset: int, limit: int) -> list[ApiToken]: ...

    async def count_api_tokens(self, user_id: int) -> int: ...

    async def touch_api_token(self, *, token_id: int, now_ms: int) -> None: ...

    async def delete_api_token(self, *, token_id: int, user_id: int) -> None: ...

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
