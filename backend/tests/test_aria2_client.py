"""Tests for aria2 client."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import aiohttp

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
        client._call.assert_called_once_with("aria2.addUri", [["http://example.com/file.zip"]])

    async def test_add_uri_with_options(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value="gid123")

        result = await client.add_uri(
            ["http://example.com/file.zip"],
            {"dir": "/downloads"}
        )
        assert result == "gid123"
        client._call.assert_called_once_with(
            "aria2.addUri",
            [["http://example.com/file.zip"], {"dir": "/downloads"}]
        )

    async def test_add_torrent(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value="gid456")

        result = await client.add_torrent("base64_torrent_data")
        assert result == "gid456"
        client._call.assert_called_once_with(
            "aria2.addTorrent",
            ["base64_torrent_data", []]
        )

    async def test_add_torrent_with_options(self):
        client = Aria2Client("http://localhost:6800/jsonrpc", "secret")
        client._call = AsyncMock(return_value="gid456")

        result = await client.add_torrent(
            "base64_torrent_data",
            ["http://webseed.com"],
            {"dir": "/downloads"}
        )
        assert result == "gid456"
        client._call.assert_called_once_with(
            "aria2.addTorrent",
            ["base64_torrent_data", ["http://webseed.com"], {"dir": "/downloads"}]
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
        client._call.assert_called_once_with("aria2.changePosition", ["gid123", 5, "POS_SET"])
