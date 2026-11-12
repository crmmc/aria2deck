from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ...domain.downloads import LIVE_ATTEMPT_STATUSES
from ...domain.errors import DomainError, ErrorCode
from ...domain.settings import SystemSettings
from ..ports import UnitOfWork
from .models import (
    AttemptRecord,
    GatewayResponse,
    SourceRecord,
    UpstreamResponse,
)

UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]


class GatewayRepository(Protocol):
    async def get_attempt(self, attempt_id: int) -> AttemptRecord: ...

    async def get_source_for_attempt(self, attempt_id: int) -> SourceRecord: ...

    async def admit_size(
        self,
        *,
        attempt_id: int,
        observed_bytes: int,
        known_total: bool,
        disk_free_bytes: int,
        min_free_bytes: int,
        max_task_bytes: int,
        now_ms: int,
    ) -> DomainError | None: ...


class MachineSpacePort(Protocol):
    def machine_free_bytes(self) -> int: ...


class UpstreamPort(Protocol):
    async def open(
        self, *, url: str, headers: Mapping[str, str], range_header: str | None
    ) -> UpstreamResponse: ...


RepositoryFactory = Callable[[UnitOfWork], GatewayRepository]


class DownloadGatewayService:
    def __init__(
        self,
        *,
        uow_factory: UowFactory,
        repository_factory: RepositoryFactory,
        upstream: UpstreamPort,
        machine_space: MachineSpacePort,
        settings: Callable[[], SystemSettings],
        clock_ms: Callable[[], int],
        verify_capability: Callable[[int, int, str], bool],
    ) -> None:
        self.uow_factory = uow_factory
        self.repository_factory = repository_factory
        self.upstream = upstream
        self.machine_space = machine_space
        self.settings = settings
        self.clock_ms = clock_ms
        self.verify_capability = verify_capability

    async def _admit(self, attempt_id: int, size: int, *, known_total: bool) -> None:
        async with self.uow_factory() as uow:
            failure = await self.repository_factory(uow).admit_size(
                attempt_id=attempt_id,
                observed_bytes=size,
                known_total=known_total,
                disk_free_bytes=self.machine_space.machine_free_bytes(),
                min_free_bytes=self.settings().min_free_disk,
                max_task_bytes=self.settings().max_task_size,
                now_ms=self.clock_ms(),
            )
        if failure is not None:
            raise failure

    async def open(
        self,
        *,
        attempt_id: int,
        source_index: int,
        capability: str | None,
        range_header: str | None,
    ) -> GatewayResponse:
        if capability is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "缺少内部下载凭证")
        if not self.verify_capability(attempt_id, 0, capability):
            raise DomainError(ErrorCode.PERMISSION_DENIED, "内部下载凭证无效")
        async with self.uow_factory() as uow:
            repository = self.repository_factory(uow)
            attempt = await repository.get_attempt(attempt_id)
            source = await repository.get_source_for_attempt(attempt_id)
        if attempt.status not in LIVE_ATTEMPT_STATUSES or source.detached_at_ms is not None:
            raise DomainError(ErrorCode.DOWNLOAD_UNAVAILABLE, "下载已结束或来源已清理")
        if source.kind != "http":
            raise DomainError(ErrorCode.PERMISSION_DENIED, "该下载类型不使用内部 HTTP 网关")
        raw_uris = source.payload.get("uris")
        if not isinstance(raw_uris, list) or not all(isinstance(uri, str) for uri in raw_uris):
            raise DomainError(ErrorCode.INVALID_STATE, "HTTP 下载来源数据无效")
        if not 0 <= source_index < len(raw_uris):
            raise DomainError(ErrorCode.NOT_FOUND, "下载来源序号不存在")
        raw_headers = source.options.get("headers", {})
        if not isinstance(raw_headers, Mapping) or not all(
            isinstance(name, str) and isinstance(value, str)
            for name, value in raw_headers.items()
        ):
            raise DomainError(ErrorCode.INVALID_STATE, "HTTP 请求头数据无效")
        upstream_headers = dict(raw_headers)
        # http-user/http-passwd → Basic 认证；用户已给 Authorization 头时不覆盖（与旧版同口径）。
        username = source.options.get("http-user")
        password = source.options.get("http-passwd")
        if isinstance(username, str) and not any(
            name.lower() == "authorization" for name in upstream_headers
        ):
            raw_credentials = f"{username}:{password if isinstance(password, str) else ''}".encode()
            upstream_headers["Authorization"] = (
                f"Basic {base64.b64encode(raw_credentials).decode()}"
            )
        upstream = await self.upstream.open(
            url=raw_uris[source_index],
            headers=upstream_headers,
            range_header=range_header,
        )
        # known-total 的上限检查由 admit_size 统一承担（M1 唯一所有者）；
        # 这里只保留流式防线：响应头到达时已读字节（含区间偏移）超限立即拒绝。
        maximum = self.settings().max_task_size
        if (
            maximum > 0
            and upstream.content_offset + (upstream.body_bytes or 0) > maximum
        ):
            await upstream.close()
            raise DomainError(ErrorCode.TASK_TOO_LARGE, "下载超过系统单任务大小上限")
        try:
            if upstream.total_bytes is not None:
                await self._admit(attempt_id, upstream.total_bytes, known_total=True)
            elif upstream.body_bytes is not None:
                await self._admit(
                    attempt_id,
                    upstream.content_offset + upstream.body_bytes,
                    known_total=False,
                )
        except DomainError:
            await upstream.close()
            raise
        return GatewayResponse(
            status=upstream.status,
            headers={
                key: value
                for key, value in upstream.headers.items()
                if key.lower() in {"content-length", "content-range"}
            }
            | {"Accept-Ranges": "bytes", "Content-Type": "application/octet-stream"},
            body=self._stream(attempt_id=attempt_id, upstream=upstream, maximum=maximum),
        )

    async def _stream(
        self, *, attempt_id: int, upstream: UpstreamResponse, maximum: int
    ) -> AsyncIterator[bytes]:
        received = 0
        try:
            async for chunk in upstream.body:
                if not chunk:
                    continue
                received += len(chunk)
                absolute = upstream.content_offset + received
                if maximum > 0 and absolute > maximum:
                    raise DomainError(ErrorCode.TASK_TOO_LARGE, "下载超过系统单任务大小上限")
                if upstream.body_bytes is not None and received > upstream.body_bytes:
                    raise DomainError(ErrorCode.UPSTREAM_FAILURE, "上游响应超过声明大小")
                if upstream.total_bytes is None:
                    await self._admit(attempt_id, absolute, known_total=False)
                yield chunk
            if upstream.body_bytes is not None and received < upstream.body_bytes:
                raise DomainError(ErrorCode.UPSTREAM_FAILURE, "上游响应少于声明大小")
        finally:
            await upstream.close()
