"""Coverage gaps for app/services/task_orchestration.py error paths."""

from __future__ import annotations

import pytest

from app.domain.errors import BadRequestError, ConflictError, ForbiddenError
from app.modules.task_core.register import RegisterError, ResourceSpec
from app.services import task_service
from app.services import task_orchestration as to


class TestRaiseRegisterError:
    def test_duplicate(self):
        with pytest.raises(ConflictError):
            to.raise_register_error(RegisterError("duplicate_task", "dup"))

    def test_quota(self):
        with pytest.raises(ForbiddenError):
            to.raise_register_error(RegisterError("quota_exceeded", "q"))

    def test_stale(self):
        with pytest.raises(ConflictError):
            to.raise_register_error(RegisterError("stale", "s"))

    def test_other(self):
        with pytest.raises(ConflictError):
            to.raise_register_error(RegisterError("conflict", "c"))


class TestTolerantBackend:
    @pytest.mark.asyncio
    async def test_remove_swallows(self, monkeypatch):
        class Inner:
            async def remove(self, tid):
                raise RuntimeError("rpc down")

            def other(self):
                return "x"

        tolerant = to._TolerantBackend(Inner())
        await tolerant.remove(1)
        assert tolerant.other() == "x"


class TestCreateTaskErrors:
    @pytest.fixture(autouse=True)
    def _healthy_env(self, monkeypatch, temp_db, test_user):
        async def ok(url):
            return None

        monkeypatch.setattr(task_service, "check_url_safety", ok)
        monkeypatch.setattr(
            task_service, "check_disk_space", lambda: (True, 10**12, 1)
        )

        async def usage(user_id, quota_bytes):
            return {"available_bytes": 10**9}

        monkeypatch.setattr(task_service, "get_usage", usage)

    @pytest.mark.asyncio
    async def test_unsupported_uri(self, test_user):
        with pytest.raises(BadRequestError, match="仅支持磁力链接"):
            await to._impl_create_task(
                user_id=test_user["id"],
                quota_bytes=10**9,
                uri="ftp://x/f",
                options=None,
            )

    @pytest.mark.asyncio
    async def test_task_too_large_http(self, test_user, monkeypatch):
        from app.services.http_probe import ProbeResult

        monkeypatch.setattr(task_service, "get_max_task_size", lambda: 10)
        monkeypatch.setattr(
            task_service,
            "probe_url_with_get_fallback",
            _probe_result(content_length=100),
        )
        with pytest.raises(ForbiddenError):
            await to._impl_create_task(
                user_id=test_user["id"],
                quota_bytes=10**9,
                uri="http://127.0.0.1:1/f.zip",
                options=None,
            )

    @pytest.mark.asyncio
    async def test_user_space_insufficient_http(self, test_user, monkeypatch):
        monkeypatch.setattr(
            task_service,
            "probe_url_with_get_fallback",
            _probe_result(content_length=100),
        )

        async def usage(user_id, quota_bytes):
            return {"available_bytes": 10}

        monkeypatch.setattr(task_service, "get_usage", usage)
        with pytest.raises(ForbiddenError):
            await to._impl_create_task(
                user_id=test_user["id"],
                quota_bytes=10**9,
                uri="http://127.0.0.1:1/f.zip",
                options=None,
            )

    @pytest.mark.asyncio
    async def test_probe_failure(self, test_user, monkeypatch):
        monkeypatch.setattr(
            task_service,
            "probe_url_with_get_fallback",
            _probe_result_failure(),
        )
        with pytest.raises(BadRequestError):
            await to._impl_create_task(
                user_id=test_user["id"],
                quota_bytes=10**9,
                uri="http://127.0.0.1:1/f.zip",
                options=None,
            )

    @pytest.mark.asyncio
    async def test_magnet_min_space(self, test_user, monkeypatch):
        async def usage(user_id, quota_bytes):
            return {"available_bytes": 0}

        monkeypatch.setattr(task_service, "get_usage", usage)
        with pytest.raises(ForbiddenError):
            await to._impl_create_task(
                user_id=test_user["id"],
                quota_bytes=10**9,
                uri="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
                options=None,
            )

    @pytest.mark.asyncio
    async def test_disk_insufficient(self, test_user, monkeypatch):
        monkeypatch.setattr(
            task_service, "check_disk_space", lambda: (False, 0, 100)
        )
        with pytest.raises(ForbiddenError):
            await to._impl_create_task(
                user_id=test_user["id"],
                quota_bytes=10**9,
                uri="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
                options=None,
            )


class TestTorrentErrors:
    @pytest.mark.asyncio
    async def test_preview_too_large(self, test_user):
        from app.domain.errors import PayloadTooLargeError

        with pytest.raises(PayloadTooLargeError):
            await to._impl_preview_torrent_task(
                user_id=test_user["id"], torrent="x" * (to.MAX_TORRENT_BASE64_LENGTH + 1)
            )

    @pytest.mark.asyncio
    async def test_create_too_large(self, test_user):
        from app.domain.errors import PayloadTooLargeError

        with pytest.raises(PayloadTooLargeError):
            await to._impl_create_torrent_task(
                user_id=test_user["id"],
                quota_bytes=10**9,
                torrent="x" * (to.MAX_TORRENT_BASE64_LENGTH + 1),
                selected_file_indexes=None,
                options=None,
            )

    @pytest.mark.asyncio
    async def test_create_torrent_task_too_large(self, temp_db, test_user, monkeypatch):
        # 用最小合法 torrent + 超小 max_task_size 触发 task_too_large
        torrent_b64 = _minimal_torrent_b64()
        monkeypatch.setattr(task_service, "check_disk_space", lambda: (True, 10**12, 1))

        async def usage(user_id, quota_bytes):
            return {"available_bytes": 10**9}

        monkeypatch.setattr(task_service, "get_usage", usage)
        monkeypatch.setattr(task_service, "get_max_task_size", lambda: 1)

        async def safe_meta(metadata):
            return None

        monkeypatch.setattr(task_service, "check_torrent_network_safety", safe_meta)
        with pytest.raises(ForbiddenError):
            await to._impl_create_torrent_task(
                user_id=test_user["id"],
                quota_bytes=10**9,
                torrent=torrent_b64,
                selected_file_indexes=None,
                options=None,
            )


def _probe_result_failure():
    from app.services.http_probe import ProbeResult

    result = ProbeResult(success=False, error="Connection error: x")
    return _async_const(result)


def _probe_result(**kwargs):
    from app.services.http_probe import ProbeResult

    result = ProbeResult(success=True, final_url="http://127.0.0.1:1/f.zip", **kwargs)
    return _async_const(result)


def _async_const(value):
    async def inner(*args, **kwargs):
        return value

    return inner


def _minimal_torrent_b64() -> str:
    import base64

    from app.domain.torrent_metadata import parse_torrent_bytes  # noqa: F401

    def bstr(value):
        return str(len(value)).encode("ascii") + b":" + value

    def bint(value):
        return b"i" + str(value).encode("ascii") + b"e"

    def bdict(items):
        return b"d" + b"".join(bstr(k) + v for k, v in items) + b"e"

    info = bdict(
        [
            (b"name", bstr(b"test")),
            (b"length", bint(100)),
            (b"piece length", bint(16384)),
            (b"pieces", bstr(b"a" * 20)),
        ]
    )
    torrent = bdict([(b"announce", bstr(b"http://tracker.example.com")), (b"info", info)])
    return base64.b64encode(torrent).decode()


class TestRegisterAndSubmit:
    @pytest.fixture
    def patched(self, temp_db, test_user, monkeypatch):
        from app.modules.task_core.register import RegisterResult
        from app.services import task_service

        result = RegisterResult(pid=1, tid=1, outcome="created", status="queued")

        async def fake_register(**kwargs):
            return result

        monkeypatch.setattr(task_service, "register", fake_register)

        class NoGidBackend:
            async def submit(self, *, tid, uri, options):
                raise RuntimeError("rpc down")

            async def remove(self, tid):
                pass

        monkeypatch.setattr(task_service, "_get_backend", lambda: NoGidBackend())
        return task_service, monkeypatch

    @pytest.mark.asyncio
    async def test_submit_exception_rolls_back(self, patched, test_user):
        from app.domain.errors import BadGatewayError
        task_service, monkeypatch = patched
        unrefed = {}

        async def fake_unref(**kwargs):
            unrefed["pid"] = kwargs.get("pid")

        monkeypatch.setattr(task_service, "unref", fake_unref)
        monkeypatch.setattr(task_service, "submit_tid", _raise_runtime())

        resource = ResourceSpec(
            resource_key="rk", source_uri="https://x/f", resource_kind="http"
        )
        with pytest.raises(BadGatewayError):
            await to.register_and_submit(
                user_id=test_user["id"], quota_bytes=10**9, resource=resource
            )
        assert unrefed.get("pid") == 1

    @pytest.mark.asyncio
    async def test_submit_exception_rollback_fails(self, patched, test_user):
        from app.domain.errors import BadGatewayError
        task_service, monkeypatch = patched
        monkeypatch.setattr(task_service, "unref", _raise_runtime())
        monkeypatch.setattr(task_service, "submit_tid", _raise_runtime())
        resource = ResourceSpec(
            resource_key="rk", source_uri="https://x/f", resource_kind="http"
        )
        with pytest.raises(BadGatewayError):
            await to.register_and_submit(
                user_id=test_user["id"], quota_bytes=10**9, resource=resource
            )

    @pytest.mark.asyncio
    async def test_submit_returns_none_gid(self, patched, test_user):
        from app.domain.errors import BadGatewayError
        task_service, monkeypatch = patched

        async def none_gid(**kwargs):
            return None

        monkeypatch.setattr(task_service, "submit_tid", none_gid)

        async def fake_unref(**kwargs):
            pass

        monkeypatch.setattr(task_service, "unref", fake_unref)
        resource = ResourceSpec(
            resource_key="rk", source_uri="https://x/f", resource_kind="http"
        )
        with pytest.raises(BadGatewayError):
            await to.register_and_submit(
                user_id=test_user["id"], quota_bytes=10**9, resource=resource
            )

    @pytest.mark.asyncio
    async def test_join_submission_failure_tolerated(self, temp_db, test_user, monkeypatch):
        from app.modules.task_core.register import RegisterResult
        from app.services import task_service

        result = RegisterResult(pid=1, tid=1, outcome="created", status="queued")

        async def fake_register(**kwargs):
            return result

        async def fake_submit(**kwargs):
            return "gid-1"

        async def fake_get(tid):
            return {"aria2_gid": "gid-1", "resource_kind": "http", "source_uri": "https://x/f"}

        async def fake_unref(**kwargs):
            pass

        class Backend:
            async def join_submission(self, *, tid, gid, uris):
                raise RuntimeError("rpc down")

        monkeypatch.setattr(task_service, "register", fake_register)
        monkeypatch.setattr(task_service, "submit_tid", fake_submit)
        monkeypatch.setattr(task_service, "unref", fake_unref)
        monkeypatch.setattr(task_service, "_get_backend", lambda: Backend())
        monkeypatch.setattr(to, "get_global_download_by_id", fake_get)
        monkeypatch.setattr(
            to,
            "_resolve_join_submission_uris",
            lambda **kwargs: ["https://m/f"],
        )
        resource = ResourceSpec(
            resource_key="rk", source_uri="https://x/f", resource_kind="http"
        )
        payload = await to.register_and_submit(
            user_id=test_user["id"], quota_bytes=10**9, resource=resource
        )
        assert payload is not None


def _raise_runtime():
    async def inner(*args, **kwargs):
        raise RuntimeError("boom")

    return inner
