from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException, Request, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.core.download_limiter import DownloadLease


def range_file_response(request: Request, file_path: Path, filename: str):
    """Create a file response with Range support."""
    try:
        file_size = file_path.stat().st_size
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在") from None
    encoded_name = quote(filename)
    if encoded_name != filename:
        disposition = f"attachment; filename*=utf-8''{encoded_name}"
    else:
        disposition = f'attachment; filename="{filename}"'

    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(
            path=str(file_path),
            media_type="application/octet-stream",
            headers={"Accept-Ranges": "bytes", "Content-Disposition": disposition},
        )

    try:
        unit, ranges = range_header.split("=", 1)
        if unit.strip() != "bytes":
            raise ValueError
        range_spec = ranges.split(",")[0].strip()
        parts = range_spec.split("-")
        if not parts[0]:
            suffix_length = int(parts[1])
            if suffix_length < 0:
                raise ValueError
            start = max(0, file_size - suffix_length)
            end = file_size - 1
        else:
            start = int(parts[0])
            end = int(parts[1]) if parts[1] else file_size - 1
            if start < 0 or end < 0:
                raise ValueError
    except (ValueError, IndexError):
        raise HTTPException(416, "Invalid Range header") from None

    if start >= file_size or start > end:
        raise HTTPException(
            416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )
    end = min(end, file_size - 1)
    content_length = end - start + 1

    def iter_file():
        try:
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
        except FileNotFoundError:
            return

    return StreamingResponse(
        iter_file(),
        status_code=206,
        media_type="application/octet-stream",
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(content_length),
            "Accept-Ranges": "bytes",
            "Content-Disposition": disposition,
        },
    )


class _LeaseStreamingResponse(StreamingResponse):
    """从 ASGI response 生命周期释放下载 lease。

    Starlette 0.37.2 在 http.disconnect 时取消 stream_response 但不关闭
    body_iterator，async-generator 的 finally 不保证执行，会泄漏 lease。
    """

    def __init__(self, response: StreamingResponse, lease: DownloadLease) -> None:
        super().__init__(
            response.body_iterator,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
            background=response.background,
        )
        self._lease = lease

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._lease.release()


def tracked_response(
    response: FileResponse | StreamingResponse,
    lease: DownloadLease | None,
) -> FileResponse | StreamingResponse:
    """Release a download lease after the response has been sent."""
    if lease is None:
        return response

    if isinstance(response, StreamingResponse):
        return _LeaseStreamingResponse(response, lease)

    async def _release():
        await lease.release()

    if response.background:
        original_bg = response.background

        async def _chained():
            await original_bg()  # type: ignore[misc]
            await _release()

        response.background = BackgroundTask(_chained)
    else:
        response.background = BackgroundTask(_release)
    return response
