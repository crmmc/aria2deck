from __future__ import annotations

import asyncio

from ..domain.errors import DomainError, ErrorCode


class PackWriterControl:
    def __init__(self) -> None:
        self._writers: dict[int, asyncio.Task[None]] = {}

    def register(self, pack_id: int, writer: asyncio.Task[None]) -> None:
        current = self._writers.get(pack_id)
        if current is not None and not current.done():
            raise DomainError(ErrorCode.INVALID_STATE, "打包写入者仍在运行")
        self._writers[pack_id] = writer

    async def stop_and_wait(self, pack_id: int) -> None:
        writer = self._writers.get(pack_id)
        if writer is None:
            return
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        if self._writers.get(pack_id) is writer:
            self._writers.pop(pack_id, None)
