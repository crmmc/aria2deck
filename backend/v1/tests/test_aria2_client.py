"""Tests for aria2 client."""

import pytest
from unittest.mock import AsyncMock

from app.aria2.client import Aria2Client


class TestAria2Client:
    def test_init(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret123")
        assert client._rpc_url == "http://localhost:6800/jsonrpc"
        assert client._secret == "secret123"

    def test_init_no_secret(self):
        client = Aria2Client("http://localhost:6800/jsonrpc")
        assert client._secret == ""

    def test_build_params_with_secret(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret123")
        params = client._build_params(["param1", "param2"])
        assert params == ["token:secret123", "param1", "param2"]

    def test_build_params_without_secret(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "")
        params = client._build_params(["param1", "param2"])
        assert params == ["param1", "param2"]


@pytest.mark.asyncio
class TestAria2ClientAsync:
    async def test_add_uri(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value="gid123")

        result = await client.add_uri(["http://example.com/file.zip"])
        assert result == "gid123"
        client._call.assert_called_once_with(
            "aria2.addUri", [["http://example.com/file.zip"]]
        )

    async def test_add_uri_with_options(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value="gid123")

        result = await client.add_uri(
            ["http://example.com/file.zip"], {"dir": "/downloads"}
        )
        assert result == "gid123"
        client._call.assert_called_once_with(
            "aria2.addUri", [["http://example.com/file.zip"], {"dir": "/downloads"}]
        )

    async def test_add_torrent(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value="gid456")

        result = await client.add_torrent("base64_torrent_data")
        assert result == "gid456"
        client._call.assert_called_once_with(
            "aria2.addTorrent", ["base64_torrent_data", []]
        )

    async def test_add_torrent_with_options(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value="gid456")

        result = await client.add_torrent(
            "base64_torrent_data", ["http://webseed.com"], {"dir": "/downloads"}
        )
        assert result == "gid456"
        client._call.assert_called_once_with(
            "aria2.addTorrent",
            ["base64_torrent_data", ["http://webseed.com"], {"dir": "/downloads"}],
        )

    async def test_tell_status(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value={"gid": "gid123", "status": "active"})

        result = await client.tell_status("gid123")
        assert result == {"gid": "gid123", "status": "active"}
        client._call.assert_called_once_with("aria2.tellStatus", ["gid123"])

    async def test_pause(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value="gid123")

        result = await client.pause("gid123")
        assert result == "gid123"
        client._call.assert_called_once_with("aria2.pause", ["gid123"])

    async def test_unpause(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value="gid123")

        result = await client.unpause("gid123")
        assert result == "gid123"
        client._call.assert_called_once_with("aria2.unpause", ["gid123"])

    async def test_remove(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value="gid123")

        result = await client.remove("gid123")
        assert result == "gid123"
        client._call.assert_called_once_with("aria2.remove", ["gid123"])

    async def test_remove_download_result(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value="OK")

        result = await client.remove_download_result("gid123")
        assert result == "OK"
        client._call.assert_called_once_with("aria2.removeDownloadResult", ["gid123"])

    async def test_get_global_stat(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value={"downloadSpeed": "1000"})

        result = await client.get_global_stat()
        assert result == {"downloadSpeed": "1000"}
        client._call.assert_called_once_with("aria2.getGlobalStat", [])

    async def test_get_files(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value=[{"path": "/file.zip"}])

        result = await client.get_files("gid123")
        assert result == [{"path": "/file.zip"}]
        client._call.assert_called_once_with("aria2.getFiles", ["gid123"])

    async def test_get_peers(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value=[{"peerId": "peer-1"}])

        result = await client.get_peers("gid123")
        assert result == [{"peerId": "peer-1"}]
        client._call.assert_called_once_with("aria2.getPeers", ["gid123"])

    async def test_get_servers(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value=[{"index": "1"}])

        result = await client.get_servers("gid123")
        assert result == [{"index": "1"}]
        client._call.assert_called_once_with("aria2.getServers", ["gid123"])

    async def test_tell_active(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value=[{"gid": "gid1"}])

        result = await client.tell_active()
        assert result == [{"gid": "gid1"}]
        client._call.assert_called_once_with("aria2.tellActive", [])

    async def test_tell_waiting(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value=[{"gid": "gid2"}])

        result = await client.tell_waiting(0, 100)
        assert result == [{"gid": "gid2"}]
        client._call.assert_called_once_with("aria2.tellWaiting", [0, 100])

    async def test_tell_stopped(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value=[{"gid": "gid3"}])

        result = await client.tell_stopped(0, 100)
        assert result == [{"gid": "gid3"}]
        client._call.assert_called_once_with("aria2.tellStopped", [0, 100])

    async def test_force_remove(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value="gid123")

        result = await client.force_remove("gid123")
        assert result == "gid123"
        client._call.assert_called_once_with("aria2.forceRemove", ["gid123"])

    async def test_get_version(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value={"version": "1.36.0"})

        result = await client.get_version()
        assert result == {"version": "1.36.0"}
        client._call.assert_called_once_with("aria2.getVersion", [])

    async def test_change_position(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value=5)

        result = await client.change_position("gid123", 5, "POS_SET")
        assert result == 5
        client._call.assert_called_once_with(
            "aria2.changePosition", ["gid123", 5, "POS_SET"]
        )


class TestMulticall:
    """M24 Task 1: system.multicall via a single HTTP request."""

    def _capture_client(self, monkeypatch, payload, *, status=200, raw=b""):
        from app.aria2.client import Aria2Client as Cls

        client = Cls("http://127.0.0.1:6800/jsonrpc", "secret")
        captured: dict = {}

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
                captured["url"] = args[0] if args else None
                captured["json"] = kwargs.get("json")

            async def __aenter__(self):
                return FakeResp()

            async def __aexit__(self, *args):
                return False

        class FakeSession:
            posts = 0

            def post(self, *args, **kwargs):
                type(self).posts += 1
                return FakePost(*args, **kwargs)

        async def fake_session(self):
            return FakeSession()

        monkeypatch.setattr(
            Cls, "get_session", classmethod(lambda cls: fake_session(cls))
        )
        return client, captured, FakeSession

    @pytest.mark.asyncio
    async def test_single_request_nested_token(self, monkeypatch):
        payload = {"result": [["g1"], {"code": 1, "message": "bad"}]}
        client, captured, sess = self._capture_client(monkeypatch, payload)
        outcomes = await client.multicall(
            [
                {"methodName": "aria2.addUri", "params": [["http://a"], {"dir": "/d"}]},
                {"methodName": "aria2.tellStatus", "params": ["g1"]},
            ]
        )
        assert sess.posts == 1
        body = captured["json"]
        assert body["method"] == "system.multicall"
        outer = body["params"][0]
        assert not any(
            isinstance(p, str) and p.startswith("token:") for p in body["params"]
        )
        assert outer[0]["methodName"] == "aria2.addUri"
        assert outer[0]["params"][0] == "token:secret"
        assert outer[0]["params"][1] == ["http://a"]
        assert outer[1]["params"] == ["token:secret", "g1"]
        assert len(outcomes) == 2
        assert outcomes[0].ok and outcomes[0].result == "g1"
        assert not outcomes[1].ok
        assert outcomes[1].fault_code == 1
        assert outcomes[1].fault_message == "bad"

    @pytest.mark.asyncio
    async def test_top_level_rpc_error(self, monkeypatch):
        client, _, _ = self._capture_client(monkeypatch, {"error": {"code": 1}})
        with pytest.raises(RuntimeError):
            await client.multicall([{"methodName": "aria2.pause", "params": ["g"]}])

    @pytest.mark.asyncio
    async def test_length_mismatch(self, monkeypatch):
        client, _, _ = self._capture_client(monkeypatch, {"result": [["g1"]]})
        with pytest.raises(RuntimeError):
            await client.multicall(
                [
                    {"methodName": "aria2.addUri", "params": [["u"]]},
                    {"methodName": "aria2.addUri", "params": [["u2"]]},
                ]
            )

    @pytest.mark.asyncio
    async def test_result_not_list(self, monkeypatch):
        client, _, _ = self._capture_client(monkeypatch, {"result": "g1"})
        with pytest.raises(RuntimeError):
            await client.multicall([{"methodName": "aria2.addUri", "params": [["u"]]}])

    @pytest.mark.asyncio
    async def test_illegal_item_shape(self, monkeypatch):
        client, _, _ = self._capture_client(
            monkeypatch, {"result": ["g1", ["a", "b"], {"code": 1}]}
        )
        with pytest.raises(RuntimeError):
            await client.multicall(
                [{"methodName": "aria2.addUri", "params": [["u"]]}]
            )

    @pytest.mark.asyncio
    async def test_length_mismatch_error_leaks_no_raw_values(self, monkeypatch):
        client, _, _ = self._capture_client(
            monkeypatch, {"result": [["token:supersecret"]]}
        )
        with pytest.raises(RuntimeError) as excinfo:
            await client.multicall(
                [
                    {"methodName": "aria2.addUri", "params": [["http://a.example/x"]]},
                    {"methodName": "aria2.pause", "params": ["g"]},
                ]
            )
        message = str(excinfo.value)
        assert "supersecret" not in message
        assert "a.example" not in message
        assert "2" in message and "1" in message  # expected/actual counts

    @pytest.mark.asyncio
    async def test_illegal_item_error_leaks_no_raw_values(self, monkeypatch):
        client, _, _ = self._capture_client(
            monkeypatch, {"result": ["http://a.example/x?token=supersecret"]}
        )
        with pytest.raises(RuntimeError) as excinfo:
            await client.multicall(
                [{"methodName": "aria2.addUri", "params": [["u"]]}]
            )
        message = str(excinfo.value)
        assert "supersecret" not in message
        assert "a.example" not in message
        assert "http" not in message
        assert "index" in message and "str" in message

    @pytest.mark.asyncio
    async def test_http_error(self, monkeypatch):
        client, _, _ = self._capture_client(
            monkeypatch, None, status=502, raw=b"bad gateway"
        )
        with pytest.raises(RuntimeError, match="HTTP 502"):
            await client.multicall([{"methodName": "aria2.addUri", "params": [["u"]]}])

    @pytest.mark.asyncio
    async def test_non_json(self, monkeypatch):
        client, _, _ = self._capture_client(
            monkeypatch, ValueError("bad"), raw=b"<html>"
        )
        with pytest.raises(RuntimeError, match="非 JSON"):
            await client.multicall([{"methodName": "aria2.addUri", "params": [["u"]]}])

    @pytest.mark.asyncio
    async def test_30_calls_single_request(self, monkeypatch):
        calls = [
            {"methodName": "aria2.addUri", "params": [[f"http://x/{i}"], {"gid": f"{i:016x}"}]}
            for i in range(30)
        ]
        payload = {"result": [[f"{i:016x}"] for i in range(30)]}
        client, _, sess = self._capture_client(monkeypatch, payload)
        outcomes = await client.multicall(calls)
        assert sess.posts == 1
        assert len(outcomes) == 30
        assert all(o.ok for o in outcomes)

    @pytest.mark.asyncio
    async def test_multicall_without_secret_no_token(self, monkeypatch):
        payload = {"result": [["g"]]}
        client, captured, _ = self._capture_client(monkeypatch, payload)
        client._secret = ""
        outcomes = await client.multicall(
            [{"methodName": "aria2.getGlobalStat", "params": []}]
        )
        assert outcomes[0].ok
        assert captured["json"]["params"][0][0]["params"] == []


class TestAria2ClientM24Gaps:
    """M24 审计补齐：_request 非 dict JSON、_call 成功/错误、共享 session 生命周期。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [["g1", "g2"], "g1", 123, None])
    async def test_non_dict_json_response_raises(self, monkeypatch, payload):
        from app.aria2.client import Aria2Client as Cls

        client = Cls("http://127.0.0.1:6800/jsonrpc", "secret")

        class FakeResp:
            def __init__(self):
                self.status = 200

            async def text(self):
                return "raw"

            async def json(self):
                return payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class FakePost:
            async def __aenter__(self):
                return FakeResp()

            async def __aexit__(self, *args):
                return False

        class FakeSession:
            def post(self, *args, **kwargs):
                return FakePost()

        async def fake_session(self):
            return FakeSession()

        monkeypatch.setattr(Cls, "get_session", classmethod(lambda cls: fake_session(cls)))
        with pytest.raises(RuntimeError, match="非法响应"):
            await client._request({"jsonrpc": "2.0", "id": "1", "method": "x", "params": []})

    @pytest.mark.asyncio
    async def test_call_returns_result(self, monkeypatch):
        from app.aria2.client import Aria2Client as Cls

        client = Cls("http://127.0.0.1:6800/jsonrpc", "secret")
        client._request = AsyncMock(return_value={"result": "gid"})
        assert await client._call("aria2.pause", ["gid"]) == "gid"
        client._request.assert_called_once()

    @pytest.mark.asyncio
    async def test_call_top_level_error_raises(self, monkeypatch):
        from app.aria2.client import Aria2Client as Cls

        client = Cls("http://127.0.0.1:6800/jsonrpc", "secret")
        client._request = AsyncMock(return_value={"error": {"code": 1, "message": "boom"}})
        with pytest.raises(RuntimeError) as excinfo:
            await client._call("aria2.pause", ["gid"])
        assert "boom" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_shared_session_lifecycle(self):
        from app.aria2.client import Aria2Client as Cls

        Cls._session = None
        first = await Cls.get_session()
        assert not first.closed
        second = await Cls.get_session()
        assert first is second  # 复用同一 session
        await Cls.close_session()
        assert Cls._session is None
        recreated = await Cls.get_session()
        assert recreated is not first
        assert not recreated.closed
        await Cls.close_session()
