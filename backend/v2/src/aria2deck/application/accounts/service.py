from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import cast

from ...domain.accounts import (
    CredentialKind,
    Permission,
    User,
    api_token_allowed,
    authorize_admin_session,
    ensure_quota_change,
    ensure_user_delete,
    ensure_user_role_change,
    ensure_username_change,
    normalize_username,
    require_credential_invalidation,
    require_session,
    session_is_active,
    validate_password_change,
    validate_password_hash,
)
from ...domain.errors import DomainError, ErrorCode
from ...domain.quota import available_user_bytes
from ..ports import (
    AccountRepositoryPort,
    CredentialPort,
    SessionRevocation,
    SessionRevocationBus,
    UnitOfWork,
    UserCleanupPort,
)
from .models import CleanupReport, LoginResult, Principal, RpcAccess, UserView

logger = logging.getLogger(__name__)

UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]
RepositoryFactory = Callable[[UnitOfWork], AccountRepositoryPort]


def _authentication_failed() -> DomainError:
    return DomainError(ErrorCode.UNAUTHENTICATED, "用户名或密码错误")


def _page(page: int, page_size: int) -> int:
    if page < 1 or not 1 <= page_size <= 100:
        raise DomainError(ErrorCode.INVALID_INPUT, "分页参数无效")
    return (page - 1) * page_size


class AccountService:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        credentials: CredentialPort,
        clock_ms: Callable[[], int],
        session_ttl_seconds: int,
        cleanup_ports: tuple[UserCleanupPort, ...] = (),
        revocation_bus: SessionRevocationBus | None = None,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.credentials = credentials
        self.clock_ms = clock_ms
        self.session_ttl_seconds = session_ttl_seconds
        self.cleanup_ports = cleanup_ports
        self.revocation_bus = revocation_bus

    async def _view(self, repository: AccountRepositoryPort, user: User) -> UserView:
        return UserView(user, await repository.get_quota(user.id))

    @staticmethod
    def _require_admin(principal: Principal, permission: Permission) -> None:
        authorize_admin_session(principal.user.role, principal.kind, permission)

    async def authenticate_session(self, raw_session_id: str) -> Principal:
        session_id = self.credentials.digest_session(raw_session_id)
        async with self.uow_factory() as uow:
            result = await self.repository_factory(uow).get_session_user(session_id)
        if result is None:
            raise _authentication_failed()
        session, user = result
        if session.credential_generation != user.credential_generation or not session_is_active(
            expires_at_ms=session.expires_at_ms,
            now_ms=self.clock_ms(),
            revoked_at_ms=session.revoked_at_ms,
            pending_delete=user.pending_delete,
        ):
            raise _authentication_failed()
        return Principal(user, CredentialKind.SESSION, session_id)

    async def authenticate_api_token(self, raw_token: str) -> Principal:
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            result = await repository.get_api_token_user(
                self.credentials.digest_api_token(raw_token)
            )
            if result is None:
                raise _authentication_failed()
            token, user = result
            if not api_token_allowed(is_admin=user.is_admin, pending_delete=user.pending_delete):
                raise _authentication_failed()
            await repository.touch_api_token(token_id=token.id, now_ms=self.clock_ms())
        return Principal(user, CredentialKind.API_TOKEN)

    async def login(self, *, username: str, password_hash: str) -> LoginResult:
        validate_password_hash(password_hash)
        now_ms = self.clock_ms()
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            user = await repository.find_user_by_username(username.strip())
            if (
                user is None
                or user.pending_delete
                or not self.credentials.verify_password(password_hash, user.password_hash)
            ):
                raise _authentication_failed()
            await repository.revoke_user_sessions(user_id=user.id, now_ms=now_ms)
            self._publish_revocation(SessionRevocation(user_id=user.id))
            raw_session_id = self.credentials.new_session_id()
            await repository.create_session(
                session_id=self.credentials.digest_session(raw_session_id),
                user_id=user.id,
                credential_generation=user.credential_generation,
                now_ms=now_ms,
                expires_at_ms=now_ms + self.session_ttl_seconds * 1000,
            )
            view = await self._view(repository, user)
        self._publish_revocation(SessionRevocation(user_id=user.id))
        return LoginResult(view, raw_session_id)

    async def logout(self, principal: Principal) -> None:
        require_session(principal.kind)
        if principal.session_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "会话不存在")
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).revoke_session(
                session_id=principal.session_id, now_ms=self.clock_ms()
            )
        self._publish_revocation(
            SessionRevocation(user_id=principal.user.id, session_id=principal.session_id)
        )

    async def current_user(self, principal: Principal) -> UserView:
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            return await self._view(repository, await repository.get_user(principal.user.id))


    async def change_password(
        self,
        principal: Principal,
        *,
        old_password_hash: str | None,
        new_password_hash: str,
    ) -> str:
        require_session(principal.kind)
        now_ms = self.clock_ms()
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            user = await repository.get_user(principal.user.id)
            old_matches = old_password_hash is not None and self.credentials.verify_password(
                old_password_hash, user.password_hash
            )
            validate_password_change(
                is_initial=user.is_initial_password,
                old_password_matches=old_matches,
                new_password_hash=new_password_hash,
            )
            changed = await repository.change_password(
                user_id=user.id,
                expected_generation=user.credential_generation,
                password_hash=self.credentials.hash_password(new_password_hash),
                is_initial_password=False,
                now_ms=now_ms,
            )
            if not changed:
                raise DomainError(ErrorCode.INVALID_STATE, "密码已被其他请求修改，请重试")
            await repository.revoke_user_sessions(user_id=user.id, now_ms=now_ms)
            self._publish_revocation(SessionRevocation(user_id=user.id))
            raw_session_id = self.credentials.new_session_id()
            await repository.create_session(
                session_id=self.credentials.digest_session(raw_session_id),
                user_id=user.id,
                credential_generation=user.credential_generation + 1,
                now_ms=now_ms,
                expires_at_ms=now_ms + self.session_ttl_seconds * 1000,
            )
        return raw_session_id

    async def ensure_initial_admin(self, *, password: str | None, required: bool) -> bool:
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            if await repository.count_users():
                return False
            if password is None:
                if required:
                    raise DomainError(
                        ErrorCode.CONFIGURATION_INVALID,
                        "首次启动必须配置至少 16 位的初始管理员密码",
                    )
                return False
            if len(password) < 16:
                raise DomainError(
                    ErrorCode.CONFIGURATION_INVALID, "初始管理员密码至少需要 16 位"
                )
            await repository.create_user(
                username="admin",
                password_hash=self.credentials.hash_password(
                    self.credentials.derive_password_hash(username="admin", password=password)
                ),
                is_admin=True,
                is_initial_password=True,
                quota_bytes=None,
                now_ms=self.clock_ms(),
            )
        return True

    async def create_user(
        self,
        actor: Principal | None,
        *,
        username: str,
        password_hash: str,
        is_admin: bool,
        is_initial_password: bool,
        quota_bytes: int | None,
    ) -> UserView:
        validate_password_hash(password_hash)
        now_ms = self.clock_ms()
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            existing = await repository.count_users()
            if existing:
                if actor is None:
                    raise DomainError(ErrorCode.UNAUTHENTICATED, "请先登录")
                self._require_admin(actor, Permission.MANAGE_USERS)
            user_id = await repository.create_user(
                username=username,
                password_hash=self.credentials.hash_password(password_hash),
                is_admin=is_admin,
                is_initial_password=is_initial_password,
                quota_bytes=quota_bytes,
                now_ms=now_ms,
            )
            return await self._view(repository, await repository.get_user(user_id))

    async def list_users(
        self, principal: Principal, *, page: int, page_size: int
    ) -> tuple[list[UserView], int]:
        self._require_admin(principal, Permission.MANAGE_USERS)
        offset = _page(page, page_size)
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            total = await repository.count_users()
            users = await repository.list_users(offset=min(offset, total), limit=page_size)
            return [await self._view(repository, user) for user in users], total

    async def get_user(self, principal: Principal, user_id: int) -> UserView:
        self._require_admin(principal, Permission.MANAGE_USERS)
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            return await self._view(repository, await repository.get_user(user_id))

    async def update_user(
        self,
        principal: Principal,
        *,
        user_id: int,
        changes: Mapping[str, object],
    ) -> UserView:
        self._require_admin(principal, Permission.MANAGE_USERS)
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            user = await repository.get_user(user_id)
            if user.pending_delete:
                raise DomainError(ErrorCode.INVALID_STATE, "待删除用户不能修改")
            raw_username = changes.get("username")
            username, username_key = (
                normalize_username(str(raw_username))
                if raw_username is not None
                else (user.username, user.username_key)
            )
            raw_password = changes.get("password_hash")
            if raw_password is None and "is_initial_password" in changes:
                raise DomainError(ErrorCode.INVALID_INPUT, "初始密码状态必须随新密码一起修改")
            ensure_username_change(
                username_changed=username_key != user.username_key,
                password_hash_supplied=raw_password is not None,
            )
            if raw_password is not None:
                validate_password_hash(str(raw_password))
            new_is_admin = bool(changes.get("is_admin", user.is_admin))
            ensure_user_role_change(
                actor_id=principal.user.id,
                target_id=user.id,
                actor_role=principal.user.role,
                current_is_admin=user.is_admin,
                new_is_admin=new_is_admin,
                admin_count=await repository.count_admins(),
            )
            quota = await repository.get_quota(user.id)
            new_quota_bytes = int(cast(int, changes.get("quota_bytes", user.quota_bytes)))
            ensure_quota_change(
                quota_bytes=new_quota_bytes,
                used_bytes=quota.used_bytes,
                reserved_bytes=quota.reserved_bytes,
            )
            await repository.update_user(
                user_id=user.id,
                username=username,
                username_key=username_key,
                password_hash=user.password_hash,
                is_admin=new_is_admin,
                quota_bytes=new_quota_bytes,
                is_initial_password=user.is_initial_password,
                now_ms=self.clock_ms(),
            )
            if raw_password is not None:
                changed = await repository.change_password(
                    user_id=user.id,
                    expected_generation=user.credential_generation,
                    password_hash=self.credentials.hash_password(str(raw_password)),
                    is_initial_password=bool(changes.get("is_initial_password", True)),
                    now_ms=self.clock_ms(),
                )
                if not changed:
                    raise DomainError(ErrorCode.INVALID_STATE, "用户凭证已变化，请重试")
                await repository.revoke_user_sessions(user_id=user.id, now_ms=self.clock_ms())
            return await self._view(repository, await repository.get_user(user.id))

    async def delete_user(self, principal: Principal, *, user_id: int) -> None:
        self._require_admin(principal, Permission.MANAGE_USERS)
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            user = await repository.get_user(user_id)
            ensure_user_delete(
                actor_id=principal.user.id,
                target_id=user.id,
                actor_role=principal.user.role,
                target_is_admin=user.is_admin,
                admin_count=await repository.count_admins(),
            )
            await repository.mark_pending_delete(user_id=user.id, now_ms=self.clock_ms())
        self._publish_revocation(SessionRevocation(user_id=user_id))


    async def authenticate_rpc_secret(self, raw_secret: str) -> Principal:
        """External RPC clients authenticate with the per-user RPC secret (ACC-10)."""
        async with self.uow_factory() as uow:
            candidates = await self.repository_factory(uow).list_rpc_enabled_users()
        for user in candidates:
            if user.pending_delete:
                continue
            ciphertext = user.rpc_secret_ciphertext
            if ciphertext is None:
                continue
            if hmac.compare_digest(
                self.credentials.decrypt(ciphertext).encode("utf-8"),
                raw_secret.encode("utf-8"),
            ):
                return Principal(user, CredentialKind.RPC_SECRET)
        raise _authentication_failed()

    async def rpc_access(self, principal: Principal) -> RpcAccess:
        require_session(principal.kind)
        ciphertext = principal.user.rpc_secret_ciphertext
        if ciphertext is None:
            return RpcAccess(False, None, None)
        return RpcAccess(
            True,
            principal.user.rpc_secret_prefix,
            self.credentials.decrypt(ciphertext),
        )

    async def set_rpc_access(self, principal: Principal, *, enabled: bool) -> RpcAccess:
        require_session(principal.kind)
        if enabled and principal.user.rpc_secret_ciphertext is not None:
            return await self.rpc_access(principal)
        if not enabled and principal.user.rpc_secret_ciphertext is None:
            return RpcAccess(False, None, None)
        raw_secret = self.credentials.new_rpc_secret() if enabled else None
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).set_rpc_secret(
                user_id=principal.user.id,
                ciphertext=(
                    self.credentials.encrypt(raw_secret) if raw_secret is not None else None
                ),
                prefix=raw_secret[:12] if raw_secret is not None else None,
                now_ms=self.clock_ms(),
            )
        return RpcAccess(
            enabled,
            raw_secret[:12] if raw_secret is not None else None,
            raw_secret,
        )

    async def refresh_rpc_access(self, principal: Principal) -> RpcAccess:
        require_session(principal.kind)
        if principal.user.rpc_secret_ciphertext is None:
            raise DomainError(ErrorCode.INVALID_STATE, "RPC 访问尚未开启")
        raw_secret = self.credentials.new_rpc_secret()
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).set_rpc_secret(
                user_id=principal.user.id,
                ciphertext=self.credentials.encrypt(raw_secret),
                prefix=raw_secret[:12],
                now_ms=self.clock_ms(),
            )
        return RpcAccess(True, raw_secret[:12], raw_secret)

    async def invalidate_credentials(
        self, principal: Principal, *, confirmation: str
    ) -> tuple[int, int]:
        self._require_admin(principal, Permission.INVALIDATE_CREDENTIALS)
        require_credential_invalidation(confirmation)
        async with self.uow_factory() as uow:
            return await self.repository_factory(uow).invalidate_credentials()

    def _publish_revocation(self, revocation: SessionRevocation) -> None:
        if self.revocation_bus is not None:
            self.revocation_bus.publish(revocation)

    async def cleanup_pending_users(self) -> CleanupReport:
        async with self.uow_factory() as uow:
            pending = await self.repository_factory(uow).list_pending_users()
        deleted = 0
        busy = 0
        failed = 0
        for user_id in pending:
            try:
                for cleanup in self.cleanup_ports:
                    await cleanup.cleanup_user(user_id)
                async with self.uow_factory() as uow:
                    if await self.repository_factory(uow).hard_delete_user(user_id):
                        deleted += 1
            except DomainError as error:
                if error.code is ErrorCode.CONTENT_BUSY:
                    busy += 1
                else:
                    failed += 1
                    logger.warning("用户清理未完成 user_id=%s error_code=%s", user_id, error.code)
            except Exception as error:  # noqa: BLE001  # 单个用户失败不能阻断其他清理
                failed += 1
                logger.warning(
                    "用户清理异常 user_id=%s error_type=%s", user_id, type(error).__name__
                )
        return CleanupReport(deleted, busy, failed)

    async def run_deletion_worker(self) -> None:
        while True:
            await self.cleanup_pending_users()
            await asyncio.sleep(1.0)

    @staticmethod
    def available_bytes(view: UserView) -> int:
        return available_user_bytes(view.quota)
