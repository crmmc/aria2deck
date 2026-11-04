from __future__ import annotations

import json

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from ....application.settings.trackers import TrackerCache
from ....domain.errors import DomainError, ErrorCode
from ..schema import tracker_cache


def _strings(value: object) -> tuple[str, ...]:
    try:
        parsed: object = json.loads(str(value))
    except json.JSONDecodeError:
        raise DomainError(ErrorCode.INVALID_STATE, "Tracker 缓存数据无效") from None
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise DomainError(ErrorCode.INVALID_STATE, "Tracker 缓存数据无效")
    return tuple(parsed)


class TrackerRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self.connection = connection

    async def get(self) -> TrackerCache:
        row = (
            await self.connection.execute(select(tracker_cache).where(tracker_cache.c.id == 1))
        ).mappings().first()
        if row is None:
            raise DomainError(ErrorCode.INVALID_STATE, "Tracker 缓存不存在")
        return TrackerCache(
            merged=_strings(row["merged_json"]),
            remote=_strings(row["remote_json"]),
            refresh_status=str(row["refresh_status"]),
            failed_urls=_strings(row["failed_urls_json"]),
            updated_at_ms=row["updated_at_ms"],
            last_refresh_at_ms=row["last_refresh_at_ms"],
        )

    async def save(self, cache: TrackerCache) -> None:
        result = await self.connection.execute(
            update(tracker_cache)
            .where(tracker_cache.c.id == 1)
            .values(
                merged_json=self._json(cache.merged),
                remote_json=self._json(cache.remote),
                refresh_status=cache.refresh_status,
                failed_urls_json=self._json(cache.failed_urls),
                updated_at_ms=cache.updated_at_ms,
                last_refresh_at_ms=cache.last_refresh_at_ms,
            )
        )
        if result.rowcount != 1:
            raise DomainError(ErrorCode.INVALID_STATE, "Tracker 缓存保存失败")

    @staticmethod
    def _json(values: tuple[str, ...]) -> str:
        return json.dumps(values, ensure_ascii=False, separators=(",", ":"))
