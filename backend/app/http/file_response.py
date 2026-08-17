from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException, Request, status
from fastapi.responses import FileResponse
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.core.download_limiter import DownloadLease
from app.services.storage_locks import ContentReadLease


RANGE_READ_CHUNK_SIZE = 256 * 1024


def prepare_range_file_response(
    request: Request,
    file_path: Path,
    filename: str,
) -> tuple[FileResponse | StreamingResponse, bool]:
    """Create a file response and report whether it covers the full entity."""
    try:
        file_size = file_path.stat().st_size
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在") from None
    encoded_name = quote(filename)
    if encoded_name != filename:
        disposition = f"attachment; filename*=utf-8''{encoded_name}"
    else:
        disposition = f'attachment; filename="{filename}"'

    def full_response() -> tuple[FileResponse, bool]:
        return (
            FileResponse(
                path=str(file_path),
                media_type="application/octet-stream",
                headers={"Accept-Ranges": "bytes", "Content-Disposition": disposition},
            ),
            True,
        )

    range_header = request.headers.get("range")
    if not range_header:
        return full_response()

    def range_error() -> HTTPException:
        return HTTPException(
            status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            "Invalid Range header",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    try:
        unit, range_set = range_header.split("=", 1)
    except ValueError:
        raise range_error() from None
    if unit.strip().lower() != "bytes":
        return full_response()

    try:
        parsed_ranges: list[tuple[int | None, int | None]] = []
        for range_spec in range_set.split(","):
            range_spec = range_spec.strip()
            if range_spec.count("-") != 1:
                raise ValueError
            start_text, end_text = range_spec.split("-")
            if not start_text:
                if not end_text.isascii() or not end_text.isdigit():
                    raise ValueError
                suffix_length = int(end_text)
                if suffix_length <= 0:
                    raise ValueError
                parsed_ranges.append((None, suffix_length))
                continue
            if not start_text.isascii() or not start_text.isdigit():
                raise ValueError
            if end_text and (not end_text.isascii() or not end_text.isdigit()):
                raise ValueError
            parsed_start = int(start_text)
            parsed_end = int(end_text) if end_text else None
            if parsed_end is not None and parsed_start > parsed_end:
                raise ValueError
            parsed_ranges.append((parsed_start, parsed_end))
    except ValueError:
        raise range_error() from None

    if len(parsed_ranges) != 1:
        return full_response()
    start_value, end_value = parsed_ranges[0]
    if start_value is None:
        assert end_value is not None
        start = max(0, file_size - end_value)
        end = file_size - 1
    else:
        start = start_value
        end = end_value if end_value is not None else file_size - 1

    if start >= file_size or start > end:
        raise range_error()
    end = min(end, file_size - 1)
    content_length = end - start + 1

    def iter_file():
        try:
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = f.read(min(RANGE_READ_CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
        except FileNotFoundError:
            return

    response = StreamingResponse(
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
    return response, start == 0 and end == file_size - 1


def range_file_response(request: Request, file_path: Path, filename: str):
    response, _covers_full_entity = prepare_range_file_response(
        request, file_path, filename
    )
    return response


async def release_download_lease(lease: DownloadLease | None) -> None:
    if lease is None:
        return
    release_task = asyncio.create_task(lease.release())
    try:
        await asyncio.shield(release_task)
    except asyncio.CancelledError:
        await asyncio.shield(release_task)
        raise


async def release_response_leases(
    download_lease: DownloadLease | None,
    read_lease: ContentReadLease | None,
) -> None:
    releases = [
        lease.release()
        for lease in (download_lease, read_lease)
        if lease is not None
    ]
    if not releases:
        return
    release_task = asyncio.gather(*releases, return_exceptions=True)
    try:
        results = await asyncio.shield(release_task)
    except asyncio.CancelledError:
        await asyncio.shield(release_task)
        raise
    for result in results:
        if isinstance(result, BaseException):
            raise result


class _LeaseFileResponse(FileResponse):
    def __init__(
        self,
        response: FileResponse,
        download_lease: DownloadLease | None,
        read_lease: ContentReadLease | None,
    ) -> None:
        super().__init__(
            response.path,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
            background=response.background,
            filename=response.filename,
            stat_result=response.stat_result,
        )
        self.chunk_size = response.chunk_size
        self._download_lease = download_lease
        self._read_lease = read_lease

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await release_response_leases(self._download_lease, self._read_lease)


class _LeaseStreamingResponse(StreamingResponse):
    def __init__(
        self,
        response: StreamingResponse,
        download_lease: DownloadLease | None,
        read_lease: ContentReadLease | None,
    ) -> None:
        super().__init__(
            response.body_iterator,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
            background=response.background,
        )
        self._download_lease = download_lease
        self._read_lease = read_lease

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await release_response_leases(self._download_lease, self._read_lease)


def tracked_response(
    response: FileResponse | StreamingResponse,
    lease: DownloadLease | None,
    read_lease: ContentReadLease | None = None,
) -> FileResponse | StreamingResponse:
    """Release response leases when the ASGI response call exits."""
    if lease is None and read_lease is None:
        return response
    if isinstance(response, FileResponse):
        return _LeaseFileResponse(response, lease, read_lease)
    return _LeaseStreamingResponse(response, lease, read_lease)
