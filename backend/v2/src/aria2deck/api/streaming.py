"""下载流响应的限流连接租约兜底释放。

Starlette 的 StreamingResponse 只在流正常结束后执行 BackgroundTask；
流中途异常（如读盘 OSError）会让异常先于后台任务抛出，连接槽从此泄漏。
这里把释放挂到 ASGI 响应调用的 finally 上：正常结束、中途异常、客户端
断开全部覆盖。释放幂等，与后台任务的正常路径释放任意次序互不冲突。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from typing import Any

from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

Releaser = Callable[[], Awaitable[None]]


async def release_all(releases: tuple[Releaser, ...]) -> None:
    """等全部释放完成后再返回；本协程自身被取消时也先等释放落地再传播取消。"""
    if not releases:
        return
    task = asyncio.gather(*(release() for release in releases), return_exceptions=True)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.shield(task)
        raise
    for result in task.result():
        if isinstance(result, BaseException):
            raise result


class LeaseStreamingResponse(StreamingResponse):
    """ASGI 响应调用退出时兜底释放下载连接占用（对齐旧版 _LeaseStreamingResponse）。"""

    def __init__(
        self,
        content: AsyncIterable[Any],
        *,
        releases: tuple[Releaser, ...] = (),
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
        background: BackgroundTask | None = None,
    ) -> None:
        super().__init__(
            content,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
            background=background,
        )
        self._releases = releases

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await release_all(self._releases)
