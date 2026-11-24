from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from typing import Protocol

from ...domain.accounts import Permission, authorize_admin_session
from ...domain.errors import DomainError, ErrorCode
from ...domain.settings import SECRET_MASK, SystemSettings, apply_settings_patch
from ..accounts.models import Principal
from ..ports import SettingsRepositoryPort, UnitOfWork

UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]
RepositoryFactory = Callable[[UnitOfWork], SettingsRepositoryPort]


class Aria2VersionPort(Protocol):
    async def version(
        self, *, url: str | None = None, secret: str | None = None
    ) -> tuple[str, list[str]]: ...


@dataclass(frozen=True, slots=True)
class Aria2TestResult:
    connected: bool
    version: str | None = None
    features: tuple[str, ...] = ()


class SettingsService:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        clock_ms: Callable[[], int],
        aria2_version: Aria2VersionPort | None = None,
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.clock_ms = clock_ms
        self.aria2_version = aria2_version
        self._current: SystemSettings | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            async with self.uow_factory() as uow:
                settings = await self.repository_factory(uow).get_settings()
            self._current = settings

    def current(self) -> SystemSettings:
        if self._current is None:
            raise DomainError(ErrorCode.INVALID_STATE, "运行配置尚未加载")
        return self._current

    def public_site_title(self) -> str:
        return self.current().site_title

    def read(self, principal: Principal) -> SystemSettings:
        authorize_admin_session(principal.user.role, principal.kind, Permission.MANAGE_SETTINGS)
        return self.current()

    async def update(
        self, principal: Principal, changes: Mapping[str, object]
    ) -> SystemSettings:
        authorize_admin_session(principal.user.role, principal.kind, Permission.MANAGE_SETTINGS)
        async with self._lock:
            async with self.uow_factory() as uow:
                repository = self.repository_factory(uow)
                current = await repository.get_settings()
                updated = replace(
                    apply_settings_patch(current, changes), updated_at_ms=self.clock_ms()
                )
                await repository.save_settings(updated)
            self._current = updated
            return updated

    async def test_aria2(
        self,
        principal: Principal,
        *,
        url: str | None = None,
        secret: str | None = None,
    ) -> Aria2TestResult:
        """测试 aria2 连接；传入表单值时优先用请求值，不落库（A8）。

        无表单值时直接探测已保存配置；掩码密钥表示复用已保存密钥；
        表单 URL/密钥的合法性由 SystemSettings 校验（replace 复跑 __post_init__）。
        """
        authorize_admin_session(principal.user.role, principal.kind, Permission.MANAGE_SETTINGS)
        if self.aria2_version is None:
            raise DomainError(ErrorCode.INVALID_STATE, "aria2 连接测试端口未装配")
        if url is None and (secret is None or secret == SECRET_MASK):
            try:
                version, features = await self.aria2_version.version()
            except (DomainError, TimeoutError, OSError):
                return Aria2TestResult(connected=False)
            return Aria2TestResult(
                connected=True, version=version, features=tuple(features)
            )
        current = self.current()
        effective_url = url if url is not None else current.aria2_rpc_url
        effective_secret = (
            current.aria2_rpc_secret
            if secret is None or secret == SECRET_MASK
            else secret
        )
        if effective_url != current.aria2_rpc_url or effective_secret != current.aria2_rpc_secret:
            current = replace(
                current, aria2_rpc_url=effective_url, aria2_rpc_secret=effective_secret
            )
        try:
            version, features = await self.aria2_version.version(
                url=current.aria2_rpc_url, secret=current.aria2_rpc_secret
            )
        except (DomainError, TimeoutError, OSError):
            return Aria2TestResult(connected=False)
        return Aria2TestResult(
            connected=True, version=version, features=tuple(features)
        )
