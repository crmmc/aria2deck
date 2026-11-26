from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from ...domain.accounts import Permission, authorize_admin_session
from ...domain.settings import SystemSettings, merge_trackers, parse_tracker_text
from ..accounts.models import Principal
from ..ports import UnitOfWork

UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]


@dataclass(frozen=True, slots=True)
class TrackerCache:
    merged: tuple[str, ...]
    remote: tuple[str, ...]
    refresh_status: str
    failed_urls: tuple[str, ...]
    updated_at_ms: int | None
    last_refresh_at_ms: int | None


class TrackerRepository(Protocol):
    async def get(self) -> TrackerCache: ...

    async def save(self, cache: TrackerCache) -> None: ...


class TrackerFetch(Protocol):
    async def fetch_text(self, url: str) -> str: ...


RepositoryFactory = Callable[[UnitOfWork], TrackerRepository]


class TrackerService:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        fetcher: TrackerFetch,
        settings: Callable[[], SystemSettings],
        clock_ms: Callable[[], int],
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.fetcher = fetcher
        self.settings = settings
        self.clock_ms = clock_ms
        self._refresh_lock = asyncio.Lock()

    async def current(self) -> tuple[str, ...]:
        async with self.uow_factory() as uow:
            cache = await self.repository_factory(uow).get()
        return merge_trackers(self.settings().tracker_fixed_list, cache.remote)

    async def status(self, principal: Principal) -> TrackerCache:
        authorize_admin_session(principal.user.role, principal.kind, Permission.MANAGE_SETTINGS)
        async with self.uow_factory() as uow:
            cache = await self.repository_factory(uow).get()
        return TrackerCache(
            merged=merge_trackers(self.settings().tracker_fixed_list, cache.remote),
            remote=cache.remote,
            refresh_status=cache.refresh_status,
            failed_urls=cache.failed_urls,
            updated_at_ms=cache.updated_at_ms,
            last_refresh_at_ms=cache.last_refresh_at_ms,
        )

    async def refresh(self) -> TrackerCache:
        async with self._refresh_lock:
            return await self._refresh_locked()

    async def rebuild(self) -> TrackerCache:
        """远程源配置变化后作废旧缓存，再按新配置的源重建一次。

        缓存只保存合并后的 remote 列表，没有「条目来自哪个源」的记录，无法按源裁剪；
        因此先整体作废让已删除源的条目立即失效，再按当前仍配置的源重新抓取。
        """
        async with self._refresh_lock:
            async with self.uow_factory() as uow:
                repository = self.repository_factory(uow)
                cache = await repository.get()
                await repository.save(
                    TrackerCache(
                        merged=merge_trackers(self.settings().tracker_fixed_list),
                        remote=(),
                        refresh_status="never",
                        failed_urls=(),
                        updated_at_ms=self.clock_ms(),
                        last_refresh_at_ms=cache.last_refresh_at_ms,
                    )
                )
            return await self._refresh_locked()

    async def _refresh_locked(self) -> TrackerCache:
        settings = self.settings()
        async with self.uow_factory() as uow:
            previous = await self.repository_factory(uow).get()
        results = await asyncio.gather(
            *(self.fetcher.fetch_text(url) for url in settings.tracker_remote_urls),
            return_exceptions=True,
        )
        successful: list[str] = []
        success_count = 0
        failed_urls: list[str] = []
        for url, result in zip(settings.tracker_remote_urls, results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result
                failed_urls.append(url)
            else:
                success_count += 1
                successful.extend(parse_tracker_text(result))
        if failed_urls and not success_count:
            remote = previous.remote
            refresh_status = "failed"
        else:
            remote = merge_trackers(tuple(successful))
            refresh_status = "partial" if failed_urls else "success"
        now_ms = self.clock_ms()
        cache = TrackerCache(
            merged=merge_trackers(settings.tracker_fixed_list, remote),
            remote=remote,
            refresh_status=refresh_status,
            failed_urls=tuple(failed_urls),
            updated_at_ms=now_ms,
            last_refresh_at_ms=now_ms,
        )
        async with self.uow_factory() as uow:
            await self.repository_factory(uow).save(cache)
        return cache

    async def refresh_for(self, principal: Principal) -> TrackerCache:
        authorize_admin_session(principal.user.role, principal.kind, Permission.MANAGE_SETTINGS)
        return await self.refresh()

    async def run_worker(self) -> None:
        while True:
            minutes = self.settings().tracker_refresh_interval_minutes
            await asyncio.sleep(minutes * 60 if minutes else 60)
            if minutes:
                await self.refresh()
