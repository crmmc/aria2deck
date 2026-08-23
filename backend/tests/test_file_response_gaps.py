"""Coverage gaps for app/http/file_response.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from app.http.file_response import (
    prepare_range_file_response,
    range_file_response,
    release_response_leases,
    tracked_response,
)
from app.main import app


def _request(headers: dict[str, str] | None = None) -> Request:
    client = TestClient(app)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "query_string": b"",
    }
    return Request(scope)


@pytest.fixture
def file_path(tmp_path: Path) -> Path:
    p = tmp_path / "f.bin"
    p.write_bytes(b"0123456789")
    return p


class TestPrepareRange:
    def test_missing_file(self, tmp_path):
        with pytest.raises(HTTPException) as exc:
            prepare_range_file_response(
                _request(), tmp_path / "nope", "f.bin"
            )
        assert exc.value.status_code == 404

    def test_no_range(self, file_path):
        response, full = prepare_range_file_response(_request(), file_path, "f.bin")
        assert full is True

    def test_non_bytes_unit(self, file_path):
        response, full = prepare_range_file_response(
            _request({"range": "items=0-1"}), file_path, "f.bin"
        )
        assert full is True

    def test_malformed_range_header(self, file_path):
        with pytest.raises(HTTPException) as exc:
            prepare_range_file_response(
                _request({"range": "bytes"}), file_path, "f.bin"
            )
        assert exc.value.status_code == 416

    def test_bad_suffix(self, file_path):
        with pytest.raises(HTTPException):
            prepare_range_file_response(
                _request({"range": "bytes=-x"}), file_path, "f.bin"
            )

    def test_bad_start(self, file_path):
        with pytest.raises(HTTPException):
            prepare_range_file_response(
                _request({"range": "bytes=a-5"}), file_path, "f.bin"
            )

    def test_bad_end(self, file_path):
        with pytest.raises(HTTPException):
            prepare_range_file_response(
                _request({"range": "bytes=0-x"}), file_path, "f.bin"
            )

    def test_start_over_end(self, file_path):
        with pytest.raises(HTTPException):
            prepare_range_file_response(
                _request({"range": "bytes=5-2"}), file_path, "f.bin"
            )

    def test_multi_range_full(self, file_path):
        response, full = prepare_range_file_response(
            _request({"range": "bytes=0-1,3-4"}), file_path, "f.bin"
        )
        assert full is True

    def test_suffix_range(self, file_path):
        response, full = prepare_range_file_response(
            _request({"range": "bytes=-3"}), file_path, "f.bin"
        )
        assert full is False
        assert response.status_code == 206

    def test_open_end_range(self, file_path):
        response, full = prepare_range_file_response(
            _request({"range": "bytes=2-"}), file_path, "f.bin"
        )
        assert full is False

    def test_range_beyond_size(self, file_path):
        with pytest.raises(HTTPException):
            prepare_range_file_response(
                _request({"range": "bytes=100-"}), file_path, "f.bin"
            )

    def test_unicode_filename(self, file_path):
        response, full = prepare_range_file_response(
            _request(), file_path, "文件.zip"
        )
        assert full is True


def test_range_file_response_wrapper(file_path):
    response = range_file_response(_request({"range": "bytes=0-1"}), file_path, "f.bin")
    assert response is not None


@pytest.mark.asyncio
async def test_release_response_leases_none():
    await release_response_leases(None, None)


@pytest.mark.asyncio
async def test_release_response_leases_raises():
    class BadLease:
        async def release(self):
            raise RuntimeError("release failed")

    with pytest.raises(RuntimeError):
        await release_response_leases(BadLease(), None)


def test_tracked_response_passthrough():
    from fastapi.responses import FileResponse, StreamingResponse

    class Resp(FileResponse):
        pass

    response = Resp(path="/dev/null")
    assert tracked_response(response, None, None) is response


def test_tracked_response_wraps():
    from fastapi.responses import FileResponse, StreamingResponse

    class Lease:
        async def release(self):
            return None

    class ReadLease:
        async def release(self):
            return None

    file_resp = FileResponse(path="/dev/null")
    wrapped = tracked_response(file_resp, Lease(), ReadLease())
    assert wrapped is not file_resp

    streaming = StreamingResponse(content=iter([b"x"]))
    wrapped_stream = tracked_response(streaming, Lease())
    assert wrapped_stream is not streaming


class TestMoreRangeCases:
    def test_bad_spec_count(self, file_path):
        with pytest.raises(HTTPException):
            prepare_range_file_response(
                _request({"range": "bytes=1-2-3"}), file_path, "f.bin"
            )

    def test_empty_suffix(self, file_path):
        with pytest.raises(HTTPException):
            prepare_range_file_response(
                _request({"range": "bytes=-"}), file_path, "f.bin"
            )

    def test_stream_content(self, file_path):
        import asyncio

        response, full = prepare_range_file_response(
            _request({"range": "bytes=2-5"}), file_path, "f.bin"
        )
        assert full is False

        async def consume():
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            return b"".join(chunks)

        body = asyncio.run(consume())
        assert body == b"2345"

    def test_stream_file_missing_midway(self, file_path):
        import asyncio

        response, full = prepare_range_file_response(
            _request({"range": "bytes=0-9"}), file_path, "f.bin"
        )
        file_path.unlink()

        async def consume():
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            return b"".join(chunks)

        assert asyncio.run(consume()) == b""


@pytest.mark.asyncio
async def test_release_response_leases_cancelled():
    class SlowLease:
        def __init__(self):
            self.released = False

        async def release(self):
            try:
                await __import__("asyncio").sleep(10)
            except __import__("asyncio").CancelledError:
                self.released = True
                raise

    import asyncio as _aio

    lease = SlowLease()
    task = _aio.ensure_future(release_response_leases(lease, None))
    await _aio.sleep(0.01)
    task.cancel()
    with pytest.raises(_aio.CancelledError):
        await task


