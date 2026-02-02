"""Tests for Aria2RpcHandler construction requirements."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.aria2.client import Aria2Client
from app.services.aria2_rpc_handler import (
    Aria2RpcHandler,
    RpcError,
    RpcErrorCode,
)


def test_aria2_rpc_handler_requires_app_state():
    """Handler should fail fast when app_state is missing."""
    client = Aria2Client("http://localhost:6800/jsonrpc")
    with pytest.raises(RuntimeError):
        Aria2RpcHandler(user_id=1, aria2_client=client, app_state=None)


class TestRpcError:

    def test_rpc_error_init(self):
        error = RpcError(RpcErrorCode.TASK_NOT_FOUND, "Task not found")
        assert error.code == RpcErrorCode.TASK_NOT_FOUND
        assert error.message == "Task not found"
        assert error.data is None

    def test_rpc_error_with_data(self):
        error = RpcError(RpcErrorCode.INVALID_PARAMS, "Invalid params", {"param": "value"})
        assert error.code == RpcErrorCode.INVALID_PARAMS
        assert error.data == {"param": "value"}

    def test_rpc_error_to_dict(self):
        error = RpcError(RpcErrorCode.INTERNAL_ERROR, "Internal error")
        result = error.to_dict()
        assert result == {"code": RpcErrorCode.INTERNAL_ERROR, "message": "Internal error"}

    def test_rpc_error_to_dict_with_data(self):
        error = RpcError(RpcErrorCode.INVALID_PARAMS, "Invalid", {"key": "val"})
        result = error.to_dict()
        assert result == {
            "code": RpcErrorCode.INVALID_PARAMS,
            "message": "Invalid",
            "data": {"key": "val"}
        }


class TestRpcErrorCode:

    def test_error_codes(self):
        assert RpcErrorCode.PARSE_ERROR == -32700
        assert RpcErrorCode.INVALID_REQUEST == -32600
        assert RpcErrorCode.METHOD_NOT_FOUND == -32601
        assert RpcErrorCode.INVALID_PARAMS == -32602
        assert RpcErrorCode.INTERNAL_ERROR == -32603
        assert RpcErrorCode.TASK_NOT_FOUND == 1
        assert RpcErrorCode.PERMISSION_DENIED == 2
        assert RpcErrorCode.QUOTA_EXCEEDED == 3


@pytest.fixture
def mock_aria2_client():
    client = AsyncMock()
    client.get_version.return_value = {"version": "1.36.0", "enabledFeatures": ["BitTorrent"]}
    client.get_global_stat.return_value = {
        "downloadSpeed": "1000",
        "uploadSpeed": "500",
        "numActive": "1",
        "numWaiting": "0",
        "numStopped": "0"
    }
    client.tell_active.return_value = []
    client.tell_waiting.return_value = []
    client.tell_stopped.return_value = []
    return client


@pytest.fixture
def mock_app_state():
    state = MagicMock()
    state.user_space_locks = {}
    return state


@pytest.fixture
def handler(test_user: dict, mock_aria2_client, mock_app_state):
    return Aria2RpcHandler(test_user["id"], mock_aria2_client, mock_app_state)


@pytest.mark.asyncio
class TestAria2RpcHandlerMethods:

    async def test_handle_method_not_found(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.unknownMethod", [])
        assert exc_info.value.code == RpcErrorCode.METHOD_NOT_FOUND

    async def test_get_version(self, handler):
        result = await handler.handle("aria2.getVersion", [])
        assert result["version"] == "1.36.0"
        assert "BitTorrent" in result["enabledFeatures"]

    async def test_get_global_stat(self, handler):
        result = await handler.handle("aria2.getGlobalStat", [])
        assert "downloadSpeed" in result
        assert "numActive" in result

    async def test_tell_active(self, handler):
        result = await handler.handle("aria2.tellActive", [])
        assert isinstance(result, list)

    async def test_tell_waiting(self, handler):
        result = await handler.handle("aria2.tellWaiting", [0, 100])
        assert isinstance(result, list)

    async def test_tell_stopped(self, handler):
        result = await handler.handle("aria2.tellStopped", [0, 100])
        assert isinstance(result, list)

    async def test_system_list_methods(self, handler):
        result = await handler.handle("system.listMethods", [])
        assert isinstance(result, list)
        assert "aria2.addUri" in result
        assert "aria2.getVersion" in result

    async def test_get_session_info(self, handler):
        handler.client.get_version.return_value = {"version": "1.36.0"}
        result = await handler.handle("aria2.getSessionInfo", [])
        assert "sessionId" in result


@pytest.mark.asyncio
class TestAria2RpcHandlerAddMethods:
    """Tests for add methods (addUri, addTorrent)."""

    async def test_add_uri_empty_params(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.addUri", [])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS

    async def test_add_uri_empty_uris(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.addUri", [[]])
        assert exc_info.value.code in [RpcErrorCode.INVALID_PARAMS, RpcErrorCode.INTERNAL_ERROR]

    async def test_add_torrent_empty_params(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.addTorrent", [])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS


@pytest.mark.asyncio
class TestAria2RpcHandlerTaskMethods:
    """Tests for task control methods."""

    async def test_remove_invalid_gid(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.remove", [])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS

    async def test_force_remove_invalid_gid(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.forceRemove", [])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS

    async def test_pause_invalid_gid(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.pause", [])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS

    async def test_force_pause_invalid_gid(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.forcePause", [])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS

    async def test_unpause_invalid_gid(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.unpause", [])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS


@pytest.mark.asyncio
class TestAria2RpcHandlerStatusMethods:
    """Tests for status query methods."""

    async def test_tell_status_invalid_gid(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.tellStatus", [])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS

    async def test_get_files_invalid_gid(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.getFiles", [])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS

    async def test_get_uris_invalid_gid(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.getUris", [])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS


@pytest.mark.asyncio
class TestAria2RpcHandlerOptionMethods:
    """Tests for option methods."""

    async def test_get_option_task_not_found(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.getOption", ["nonexistent_gid"])
        assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND

    async def test_change_option_task_not_found(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.changeOption", ["nonexistent_gid", {"max-download-limit": "1M"}])
        assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND

    async def test_get_global_option(self, handler):
        handler.client.get_global_option.return_value = {"max-concurrent-downloads": "5"}
        result = await handler.handle("aria2.getGlobalOption", [])
        assert isinstance(result, dict)

    async def test_change_global_option(self, handler):
        handler.client.change_global_option.return_value = "OK"
        result = await handler.handle("aria2.changeGlobalOption", [{"max-concurrent-downloads": "10"}])
        assert result == "OK"


@pytest.mark.asyncio
class TestAria2RpcHandlerBulkMethods:
    """Tests for bulk operation methods."""

    async def test_pause_all(self, handler):
        handler.client.pause_all.return_value = "OK"
        result = await handler.handle("aria2.pauseAll", [])
        assert result == "OK"

    async def test_force_pause_all(self, handler):
        handler.client.force_pause_all.return_value = "OK"
        result = await handler.handle("aria2.forcePauseAll", [])
        assert result == "OK"

    async def test_unpause_all(self, handler):
        handler.client.unpause_all.return_value = "OK"
        result = await handler.handle("aria2.unpauseAll", [])
        assert result == "OK"

    async def test_purge_download_result(self, handler):
        handler.client.purge_download_result.return_value = "OK"
        result = await handler.handle("aria2.purgeDownloadResult", [])
        assert result == "OK"

    async def test_remove_download_result_with_gid(self, handler):
        handler.client.remove_download_result.return_value = "OK"
        result = await handler.handle("aria2.removeDownloadResult", ["abc123"])
        assert result == "OK"


@pytest.mark.asyncio
class TestAria2RpcHandlerSystemMethods:
    """Tests for system methods."""

    async def test_system_multicall_empty(self, handler):
        result = await handler.handle("system.multicall", [[]])
        assert result == []

    async def test_system_multicall_single(self, handler):
        handler.client.get_version.return_value = {"version": "1.36.0"}
        result = await handler.handle("system.multicall", [[
            {"methodName": "aria2.getVersion", "params": []}
        ]])
        assert len(result) == 1
        assert isinstance(result[0], (dict, list))


@pytest.mark.asyncio
class TestAria2RpcHandlerHelpers:
    """Tests for helper methods."""

    async def test_get_handler_name_aria2(self, handler):
        name = handler._get_handler_name("aria2.addUri")
        assert name == "_handle_add_uri"

    async def test_get_handler_name_system(self, handler):
        name = handler._get_handler_name("system.listMethods")
        assert name == "_handle_system_list_methods"

    async def test_get_handler_name_camel_case(self, handler):
        name = handler._get_handler_name("aria2.getGlobalStat")
        assert name == "_handle_get_global_stat"


@pytest.mark.asyncio
class TestAria2RpcHandlerSanitization:
    """Tests for path sanitization methods."""

    async def test_sanitize_path_empty(self, handler):
        result = handler._sanitize_path("")
        assert result == ""

    async def test_sanitize_path_none(self, handler):
        result = handler._sanitize_path(None)
        assert result is None

    async def test_sanitize_path_relative(self, handler):
        result = handler._sanitize_path("movie/file.mp4")
        assert result == "movie/file.mp4"

    async def test_sanitize_status_empty(self, handler):
        result = handler._sanitize_status({})
        assert result == {}

    async def test_sanitize_status_with_dir(self, handler):
        status = {"dir": "/some/path", "gid": "abc123"}
        result = handler._sanitize_status(status)
        assert "dir" in result
        assert "gid" in result

    async def test_sanitize_status_with_files(self, handler):
        status = {
            "files": [
                {"path": "/some/path/file.txt", "length": "1000"},
                {"path": "/another/path/file2.txt", "length": "2000"}
            ]
        }
        result = handler._sanitize_status(status)
        assert "files" in result
        assert len(result["files"]) == 2


@pytest.mark.asyncio
class TestAria2RpcHandlerUserSpace:
    """Tests for user space calculation."""

    async def test_get_user_download_dir(self, handler):
        result = handler._get_user_download_dir()
        assert result is not None
        assert str(handler.user_id) in result

    async def test_get_user_incomplete_dir(self, handler):
        result = handler._get_user_incomplete_dir()
        assert result is not None
        assert ".incomplete" in result

    async def test_verify_task_owner_not_found(self, handler):
        result = handler._verify_task_owner("nonexistent_gid")
        assert result is None


@pytest.mark.asyncio
class TestAria2RpcHandlerShutdown:
    """Tests for shutdown methods."""

    async def test_shutdown(self, handler):
        handler.client.shutdown.return_value = "OK"
        result = await handler.handle("aria2.shutdown", [])
        assert result == "OK"

    async def test_force_shutdown(self, handler):
        handler.client.force_shutdown.return_value = "OK"
        result = await handler.handle("aria2.forceShutdown", [])
        assert result == "OK"

    async def test_save_session(self, handler):
        handler.client.save_session.return_value = "OK"
        result = await handler.handle("aria2.saveSession", [])
        assert result == "OK"


@pytest.mark.asyncio
class TestAria2RpcHandlerChangePosition:

    async def test_change_position_invalid_params(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.changePosition", [])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS

    async def test_change_position_task_not_found(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.changePosition", ["nonexistent_gid", 0, "POS_SET"])
        assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND


@pytest.mark.asyncio
class TestAria2RpcHandlerWithTasks:

    async def test_remove_task_not_found(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.remove", ["nonexistent_gid"])
        assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND

    async def test_force_remove_task_not_found(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.forceRemove", ["nonexistent_gid"])
        assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND

    async def test_pause_task_not_found(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.pause", ["nonexistent_gid"])
        assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND

    async def test_unpause_task_not_found(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.unpause", ["nonexistent_gid"])
        assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND

    async def test_tell_status_task_not_found(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.tellStatus", ["nonexistent_gid"])
        assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND

    async def test_get_files_task_not_found(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.getFiles", ["nonexistent_gid"])
        assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND

    async def test_get_uris_task_not_found(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.getUris", ["nonexistent_gid"])
        assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND


@pytest.mark.asyncio
class TestAria2RpcHandlerTellMethods:

    async def test_tell_waiting_invalid_params(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.tellWaiting", [])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS

    async def test_tell_stopped_invalid_params(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.tellStopped", [])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS


@pytest.mark.asyncio
class TestAria2RpcHandlerMulticall:

    async def test_multicall_invalid_params(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("system.multicall", [])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS

    async def test_multicall_invalid_method_call(self, handler):
        result = await handler.handle("system.multicall", [["not_a_dict"]])
        assert len(result) == 1
        assert "faultCode" in result[0]

    async def test_multicall_missing_method_name(self, handler):
        result = await handler.handle("system.multicall", [[{"params": []}]])
        assert len(result) == 1
        assert result[0]["faultCode"] == RpcErrorCode.INVALID_PARAMS

    async def test_multicall_method_error(self, handler):
        result = await handler.handle("system.multicall", [[
            {"methodName": "aria2.tellStatus", "params": []}
        ]])
        assert len(result) == 1
        assert "faultCode" in result[0]


@pytest.mark.asyncio
class TestAria2RpcHandlerStaticMethods:

    async def test_get_global_option_returns_empty(self, handler):
        result = await handler.handle("aria2.getGlobalOption", [])
        assert result == {}

    async def test_change_global_option_returns_ok(self, handler):
        result = await handler.handle("aria2.changeGlobalOption", [{"key": "value"}])
        assert result == "OK"

    async def test_shutdown_returns_ok(self, handler):
        result = await handler.handle("aria2.shutdown", [])
        assert result == "OK"

    async def test_force_shutdown_returns_ok(self, handler):
        result = await handler.handle("aria2.forceShutdown", [])
        assert result == "OK"

    async def test_save_session_returns_ok(self, handler):
        result = await handler.handle("aria2.saveSession", [])
        assert result == "OK"

    async def test_purge_download_result_returns_ok(self, handler):
        result = await handler.handle("aria2.purgeDownloadResult", [])
        assert result == "OK"

    async def test_remove_download_result_returns_ok(self, handler):
        result = await handler.handle("aria2.removeDownloadResult", ["gid123"])
        assert result == "OK"

    async def test_pause_all_returns_ok(self, handler):
        result = await handler.handle("aria2.pauseAll", [])
        assert result == "OK"

    async def test_force_pause_all_returns_ok(self, handler):
        result = await handler.handle("aria2.forcePauseAll", [])
        assert result == "OK"

    async def test_unpause_all_returns_ok(self, handler):
        result = await handler.handle("aria2.unpauseAll", [])
        assert result == "OK"

    async def test_get_option_returns_empty(self, handler):
        result = await handler.handle("aria2.getOption", [])
        assert result == {}

    async def test_change_option_returns_ok(self, handler):
        result = await handler.handle("aria2.changeOption", [])
        assert result == "OK"


@pytest.mark.asyncio
class TestAria2RpcHandlerAddTorrent:

    async def test_add_torrent_invalid_type(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.addTorrent", [123])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS

    async def test_add_torrent_too_large(self, handler):
        large_torrent = "x" * (15 * 1024 * 1024)
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.addTorrent", [large_torrent])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS


@pytest.mark.asyncio
class TestAria2RpcHandlerGetGlobalStat:

    async def test_get_global_stat_with_no_tasks(self, handler):
        result = await handler.handle("aria2.getGlobalStat", [])
        assert "downloadSpeed" in result
        assert "uploadSpeed" in result
        assert "numActive" in result
        assert "numWaiting" in result
        assert "numStopped" in result

    async def test_get_global_stat_returns_string_values(self, handler):
        result = await handler.handle("aria2.getGlobalStat", [])
        assert isinstance(result["downloadSpeed"], str)
        assert isinstance(result["numActive"], str)


@pytest.mark.asyncio
class TestAria2RpcHandlerGetSessionInfo:

    async def test_get_session_info_returns_session_id(self, handler):
        result = await handler.handle("aria2.getSessionInfo", [])
        assert "sessionId" in result
        assert isinstance(result["sessionId"], str)


@pytest.mark.asyncio
class TestAria2RpcHandlerUserSpaceExtended:

    async def test_get_user_available_space_no_user(self, handler, temp_db):
        from app.db import execute
        execute("DELETE FROM users WHERE id = ?", [handler.user_id])
        result = handler._get_user_available_space()
        assert result == 0

    async def test_check_disk_space_returns_tuple(self, handler):
        result = handler._check_disk_space()
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], int)

    async def test_get_user_available_space_with_user(self, handler, test_user, temp_db):
        result = handler._get_user_available_space()
        assert result > 0

    async def test_sanitize_path_absolute_outside_user_dir(self, handler):
        result = handler._sanitize_path("/some/other/path/file.txt")
        assert result == "/some/other/path/file.txt"
