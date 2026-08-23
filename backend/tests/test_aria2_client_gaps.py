"""Coverage gaps for app/aria2 (client / gateway / sync / listener) and app/auth.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.aria2.client import Aria2Client


@pytest.fixture
def client():
    c = Aria2Client("http://127.0.0.1:6800/jsonrpc", "secret")
    c._call = AsyncMock(return_value="OK")
    return c


class TestClientPassThroughs:
    @pytest.mark.asyncio
    async def test_change_option(self, client):
        assert await client.change_option("g", {"dir": "/d"}) == "OK"
        client._call.assert_awaited_once_with("aria2.changeOption", ["g", {"dir": "/d"}])

    @pytest.mark.asyncio
    async def test_get_uris(self, client):
        client._call.return_value = []
        assert await client.get_uris("g") == []
        client._call.assert_awaited_once_with("aria2.getUris", ["g"])

    @pytest.mark.asyncio
    async def test_change_uri(self, client):
        client._call.return_value = [0, 1]
        assert await client.change_uri("g", 1, ["a"], ["b"]) == [0, 1]
        client._call.assert_awaited_once_with("aria2.changeUri", ["g", 1, ["a"], ["b"]])


class TestClientHttpErrors:
    def _client_with_response(self, monkeypatch, payload, *, status=200, raw=b""):
        client = Aria2Client("http://127.0.0.1:6800/jsonrpc", "")

        class FakeResp:
            def __init__(self):
                self.status = status

            async def text(self):
                return raw.decode("utf-8", "replace")

            async def json(self):
                if isinstance(payload, Exception):
                    raise payload
                return payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class FakePost:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return FakeResp()

            async def __aexit__(self, *args):
                return False

        class FakeSession:
            def post(self, *args, **kwargs):
                return FakePost(*args, **kwargs)

        async def fake_session(self):
            return FakeSession()

        monkeypatch.setattr(Aria2Client, "get_session", classmethod(lambda cls: fake_session(cls)))
        return client

    @pytest.mark.asyncio
    async def test_http_error(self, monkeypatch):
        client = self._client_with_response(monkeypatch, None, status=500, raw=b"boom")
        with pytest.raises(RuntimeError, match="HTTP 500"):
            await client._call("aria2.pause", [])

    @pytest.mark.asyncio
    async def test_non_json(self, monkeypatch):
        client = self._client_with_response(
            monkeypatch, ValueError("bad json"), raw=b"<html>"
        )
        with pytest.raises(RuntimeError, match="非 JSON"):
            await client._call("aria2.pause", [])

    @pytest.mark.asyncio
    async def test_rpc_error(self, monkeypatch):
        client = self._client_with_response(monkeypatch, {"error": {"code": 1}})
        with pytest.raises(RuntimeError):
            await client._call("aria2.pause", [])

    @pytest.mark.asyncio
    async def test_result(self, monkeypatch):
        client = self._client_with_response(monkeypatch, {"result": "ok"})
        assert await client._call("aria2.pause", []) == "ok"


class TestGateway:
    def test_resolve_defaults(self, monkeypatch):
        import app.aria2.gateway as gw

        monkeypatch.setattr(gw, "_cached_rpc_url", None)
        monkeypatch.setattr(gw, "_cached_rpc_secret", None)
        monkeypatch.setattr(gw.settings, "aria2_rpc_url", "http://default:6800/jsonrpc")
        monkeypatch.setattr(gw.settings, "aria2_rpc_secret", "s1")
        assert gw.resolve_aria2_config() == ("http://default:6800/jsonrpc", "s1")

    def test_get_client_with_request_state_reuse(self, monkeypatch):
        import app.aria2.gateway as gw

        class State:
            pass

        class FakeApp:
            state = State()

        class FakeRequest:
            app = FakeApp()

        url, secret = gw.resolve_aria2_config()
        first = gw.get_aria2_client(FakeRequest())
        # lifespan 会把 client 挂到 app.state；此后按配置复用
        FakeApp.state.aria2_client = first
        assert gw.get_aria2_client(FakeRequest()) is first
        # URL 变化时重建
        monkeypatch.setattr(gw, "_cached_rpc_url", "http://other:6800/jsonrpc")
        third = gw.get_aria2_client(FakeRequest())
        assert third is not first


class TestAuthHelpers:
    def test_ms_to_iso_none(self):
        from app.auth import ms_to_iso

        assert ms_to_iso(None) is None
        assert ms_to_iso(0) is not None

    @pytest.mark.asyncio
    async def test_get_user_by_rpc_secret_no_match(self, temp_db):
        from app.auth import get_user_by_rpc_secret

        assert await get_user_by_rpc_secret("no-such-secret") is None
