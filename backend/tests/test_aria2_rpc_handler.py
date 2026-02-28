"""Tests for Aria2RpcHandler construction requirements."""
import asyncio
import pytest
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

from sqlmodel import col, select

from app.aria2.client import Aria2Client
from app.core.state import AppState
from app.database import get_session
from app.models import DownloadTask, TaskHistory, UserTaskSubscription
from app.services.hash import get_uri_hash
from app.services.storage import get_task_download_dir
from app.services.aria2_rpc_handler import (
    Aria2RpcHandler,
    RpcError,
    RpcErrorCode,
)


def test_aria2_rpc_handler_requires_app_state():
    """Handler should fail fast when app_state is missing."""
    client = Aria2Client("http://localhost:6800/jsonrpc")
    with pytest.raises(RuntimeError):
        Aria2RpcHandler(user_id=1, aria2_client=client, app_state=cast(AppState, None))


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
    state.task_submit_locks = {}
    state.lock = asyncio.Lock()
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

    async def test_add_uri_existing_subscription_resets_to_pending(self, handler):
        handler.client.add_uri.return_value = "gid-existing"
        uri = "https://example.com/file.iso"
        uri_hash = get_uri_hash(uri)
        assert uri_hash is not None

        async with get_session() as db:
            task = DownloadTask(
                uri_hash=uri_hash,
                uri=uri,
                gid="gid-existing",
                status="active",
                name="file.iso",
            )
            db.add(task)
            await db.flush()
            assert task.id is not None
            sub = UserTaskSubscription(owner_id=handler.user_id, task_id=task.id, status="success")
            db.add(sub)

        result = await handler.handle("aria2.addUri", [[uri]])
        assert result == "gid-existing"

        async with get_session() as db:
            stmt = select(UserTaskSubscription).where(
                UserTaskSubscription.owner_id == handler.user_id,
                UserTaskSubscription.task_id == task.id,
            )
            refreshed_sub = (await db.exec(stmt)).first()
            assert refreshed_sub is not None
            assert refreshed_sub.status == "pending"

    async def test_add_uri_uses_task_download_dir(self, handler):
        handler.client.add_uri.return_value = "gid-dir-uri"
        uri = "https://example.com/dir-uri.bin"

        result = await handler.handle(
            "aria2.addUri",
            [[uri], {"max-connection-per-server": "4"}],
        )
        assert result == "gid-dir-uri"

        async with get_session() as db:
            task = (
                await db.exec(select(DownloadTask).where(DownloadTask.gid == "gid-dir-uri"))
            ).first()
            assert task is not None
            assert task.id is not None

        sent_options = handler.client.add_uri.call_args.args[1]
        assert sent_options["dir"] == str(get_task_download_dir(task.id))
        assert sent_options["max-connection-per-server"] == "4"

    async def test_add_uri_failure_marks_subscription_failed(self, handler):
        from pathlib import Path

        from app.core.config import settings

        handler.client.add_uri.side_effect = RuntimeError("rpc unavailable")
        uri = "https://example.com/fail.bin"

        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.addUri", [[uri]])
        assert exc_info.value.code == RpcErrorCode.INTERNAL_ERROR

        async with get_session() as db:
            task = (
                await db.exec(select(DownloadTask).where(DownloadTask.uri == uri))
            ).first()
            assert task is not None
            assert task.id is not None
            assert task.status == "error"

            sub = (
                await db.exec(
                    select(UserTaskSubscription).where(
                        UserTaskSubscription.owner_id == handler.user_id,
                        UserTaskSubscription.task_id == task.id,
                    )
                )
            ).first()
            assert sub is not None
            assert sub.status == "failed"
            assert sub.frozen_space == 0

            history = (
                await db.exec(
                    select(TaskHistory).where(TaskHistory.owner_id == handler.user_id)
                )
            ).all()
            assert len(history) == 1
            assert history[0].result == "failed"
            assert history[0].reason == "添加下载任务失败"

            task_dir = Path(settings.download_dir) / "downloading" / str(task.id)
            assert not task_dir.exists()

    async def test_add_torrent_failure_marks_subscription_failed(self, handler):
        from pathlib import Path

        from app.core.config import settings

        handler.client.add_torrent.side_effect = RuntimeError("rpc unavailable")
        torrent_data = "d" * 100

        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.addTorrent", [torrent_data])
        assert exc_info.value.code == RpcErrorCode.INTERNAL_ERROR

        async with get_session() as db:
            task = (
                await db.exec(
                    select(DownloadTask).where(col(DownloadTask.uri).like("torrent:%"))
                    .order_by(col(DownloadTask.id).desc())
                )
            ).first()
            assert task is not None
            assert task.id is not None
            assert task.status == "error"

            sub = (
                await db.exec(
                    select(UserTaskSubscription).where(
                        UserTaskSubscription.owner_id == handler.user_id,
                        UserTaskSubscription.task_id == task.id,
                    )
                )
            ).first()
            assert sub is not None
            assert sub.status == "failed"
            assert sub.frozen_space == 0

            history = (
                await db.exec(
                    select(TaskHistory).where(TaskHistory.owner_id == handler.user_id)
                )
            ).all()
            assert len(history) == 1
            assert history[0].result == "failed"
            assert history[0].reason == "添加种子任务失败"

            task_dir = Path(settings.download_dir) / "downloading" / str(task.id)
            assert not task_dir.exists()

    async def test_add_torrent_uses_task_download_dir(self, handler):
        handler.client.add_torrent.return_value = "gid-dir-torrent"
        torrent_data = "d" * 100

        result = await handler.handle(
            "aria2.addTorrent",
            [torrent_data, [], {"seed-ratio": "0"}],
        )
        assert result == "gid-dir-torrent"

        async with get_session() as db:
            task = (
                await db.exec(select(DownloadTask).where(DownloadTask.gid == "gid-dir-torrent"))
            ).first()
            assert task is not None
            assert task.id is not None

        sent_options = handler.client.add_torrent.call_args.args[2]
        assert sent_options["dir"] == str(get_task_download_dir(task.id))
        assert sent_options["seed-ratio"] == "0"


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

    async def test_pause_returns_gid(self, handler):
        result = await handler.handle("aria2.pause", ["some_gid"])
        assert result == "some_gid"

    async def test_force_pause_returns_gid(self, handler):
        result = await handler.handle("aria2.forcePause", ["some_gid"])
        assert result == "some_gid"

    async def test_unpause_returns_gid(self, handler):
        result = await handler.handle("aria2.unpause", ["some_gid"])
        assert result == "some_gid"

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

    async def test_get_option_returns_empty(self, handler):
        result = await handler.handle("aria2.getOption", ["some_gid"])
        assert result == {}
    async def test_change_option_returns_ok(self, handler):
        result = await handler.handle("aria2.changeOption", ["some_gid", {"max-download-limit": "1M"}])
        assert result == "OK"
    async def test_get_global_option_returns_empty(self, handler):
        result = await handler.handle("aria2.getGlobalOption", [])
        assert result == {}
    async def test_change_global_option(self, handler):
        handler.client.change_global_option.return_value = "OK"
        result = await handler.handle("aria2.changeGlobalOption", [{"max-concurrent-downloads": "10"}])
        assert result == "OK"


@pytest.mark.asyncio
class TestAria2RpcHandlerBulkMethods:
    """Tests for bulk operation methods."""

    async def test_pause_all(self, handler):
        result = await handler.handle("aria2.pauseAll", [])
        assert result == "OK"

    async def test_force_pause_all(self, handler):
        result = await handler.handle("aria2.forcePauseAll", [])
        assert result == "OK"

    async def test_unpause_all(self, handler):
        result = await handler.handle("aria2.unpauseAll", [])
        assert result == "OK"

    async def test_purge_download_result(self, handler):
        async with get_session() as db:
            done_task = DownloadTask(uri_hash="purge-hash-1", uri="https://x/1", gid="purge-gid-1", status="complete")
            active_task = DownloadTask(uri_hash="purge-hash-2", uri="https://x/2", gid="purge-gid-2", status="active")
            db.add(done_task)
            db.add(active_task)
            await db.flush()
            assert done_task.id is not None
            assert active_task.id is not None
            db.add(UserTaskSubscription(owner_id=handler.user_id, task_id=done_task.id, status="success"))
            db.add(UserTaskSubscription(owner_id=handler.user_id, task_id=active_task.id, status="pending"))
            db.add(TaskHistory(owner_id=handler.user_id, task_name="h1", uri="https://x/1", result="completed"))
            db.add(TaskHistory(owner_id=handler.user_id, task_name="h2", uri="https://x/2", result="cancelled"))

        result = await handler.handle("aria2.purgeDownloadResult", [])
        assert result == "OK"

        async with get_session() as db:
            stopped_stmt = select(UserTaskSubscription).where(
                UserTaskSubscription.owner_id == handler.user_id,
                col(UserTaskSubscription.status).in_(["success", "failed"]),
            )
            pending_stmt = select(UserTaskSubscription).where(
                UserTaskSubscription.owner_id == handler.user_id,
                UserTaskSubscription.status == "pending",
            )
            history_stmt = select(TaskHistory).where(TaskHistory.owner_id == handler.user_id)
            assert len((await db.exec(stopped_stmt)).all()) == 0
            assert len((await db.exec(pending_stmt)).all()) == 1
            assert len((await db.exec(history_stmt)).all()) == 0

    async def test_remove_download_result_no_gid(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.removeDownloadResult", [])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS


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
        assert isinstance(result[0], list)
        assert result[0][0]["version"] == "1.36.0"


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
        assert result["uploadLength"] == "0"
        assert result["files"] == []

    async def test_sanitize_status_with_dir(self, handler):
        status = {"dir": "/some/path", "gid": "abc123", "connections": "12"}
        result = handler._sanitize_status(status)
        assert "gid" in result
        assert result["dir"] == ""
        assert result["connections"] == "12"

    async def test_sanitize_status_with_files(self, handler):
        status = {
            "files": [
                {"path": "/some/path/file.txt", "length": "1000", "uris": [{"uri": "http://x"}]},
                {"path": "/another/path/file2.txt", "length": "2000", "uris": [{"uri": "http://y"}]},
            ],
            "bittorrent": {
                "announceList": [["udp://tracker.example.com:6969/announce"]],
                "info": {"name": "my-torrent"},
            },
        }
        result = handler._sanitize_status(status)
        assert "files" in result
        assert len(result["files"]) == 2
        assert result["files"][0]["path"] == "file.txt"
        assert result["files"][0]["uris"] == []
        assert result["bittorrent"]["announceList"] == []
        assert result["bittorrent"]["info"]["name"] == "my-torrent"

    async def test_get_files_strips_path_and_uris(self, handler):
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="hash-files-1",
                uri="https://example.com/file.bin",
                gid="gid-files-1",
                status="active",
                name="file.bin",
            )
            db.add(task)
            await db.flush()
            assert task.id is not None
            db.add(UserTaskSubscription(owner_id=handler.user_id, task_id=task.id, status="pending"))

        handler.client.get_files.return_value = [
            {
                "index": "1",
                "path": "/private/downloads/42/secret/file.bin",
                "length": "10",
                "completedLength": "0",
                "uris": [{"uri": "https://example.com/file.bin"}],
            }
        ]

        result = await handler.handle("aria2.getFiles", ["gid-files-1"])
        assert len(result) == 1
        assert result[0]["path"] == "file.bin"
        assert result[0]["uris"] == []


@pytest.mark.asyncio
class TestAria2RpcHandlerUserSpace:
    """Tests for user space calculation."""

    async def test_get_user_download_dir(self, handler):
        result = handler._get_user_download_dir()
        assert result is not None
        assert str(handler.user_id) in result

    async def test_verify_task_owner_not_found(self, handler):
        result = await handler._verify_task_owner("nonexistent_gid")
        assert result is None


@pytest.mark.asyncio
class TestAria2RpcHandlerShutdown:
    """Tests for shutdown methods."""

    async def test_shutdown(self, handler):
        result = await handler.handle("aria2.shutdown", [])
        assert result == "OK"
    async def test_force_shutdown(self, handler):
        result = await handler.handle("aria2.forceShutdown", [])
        assert result == "OK"

    async def test_save_session(self, handler):
        result = await handler.handle("aria2.saveSession", [])
        assert result == "OK"


@pytest.mark.asyncio
class TestAria2RpcHandlerChangePosition:

    async def test_change_position_returns_zero(self, handler):
        result = await handler.handle("aria2.changePosition", [])
        assert result == 0

    async def test_change_position_with_params_returns_zero(self, handler):
        result = await handler.handle("aria2.changePosition", ["some_gid", 0, "POS_SET"])
        assert result == 0


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

    async def test_remove_last_subscription_cleans_task_download_dir(self, handler):
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="hash-remove-cleanup",
                uri="https://example.com/remove-cleanup.bin",
                gid="gid-remove-cleanup",
                status="active",
                name="remove-cleanup.bin",
            )
            db.add(task)
            await db.flush()
            assert task.id is not None
            db.add(UserTaskSubscription(owner_id=handler.user_id, task_id=task.id, status="pending"))

        task_dir = get_task_download_dir(task.id)
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "partial.bin").write_text("x")

        handler._cleanup_aria2_gid = AsyncMock()
        result = await handler.handle("aria2.remove", ["gid-remove-cleanup"])

        assert result == "gid-remove-cleanup"
        assert not task_dir.exists()
        handler._cleanup_aria2_gid.assert_awaited_once_with("gid-remove-cleanup")

    async def test_pause_returns_gid_for_nonexistent(self, handler):
        result = await handler.handle("aria2.pause", ["nonexistent_gid"])
        assert result == "nonexistent_gid"
    async def test_unpause_returns_gid_for_nonexistent(self, handler):
        result = await handler.handle("aria2.unpause", ["nonexistent_gid"])
        assert result == "nonexistent_gid"

    async def test_tell_status_task_not_found(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.tellStatus", ["nonexistent_gid"])
        assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND

    async def test_tell_status_history_gid(self, handler):
        async with get_session() as db:
            history = TaskHistory(
                owner_id=handler.user_id,
                task_name="hist-task",
                uri="https://example.com/hist.iso",
                total_length=1024,
                result="completed",
            )
            db.add(history)
            await db.flush()
            assert history.id is not None

        result = await handler.handle("aria2.tellStatus", [f"hist-{history.id}"])
        assert result["gid"] == f"hist-{history.id}"
        assert result["status"] == "complete"
        assert result["totalLength"] == "1024"
        assert result["completedLength"] == "1024"
        assert result["files"][0]["path"] == "hist-task"

    async def test_get_files_supports_history_gid(self, handler):
        async with get_session() as db:
            history = TaskHistory(
                owner_id=handler.user_id,
                task_name="hist-files.bin",
                uri="https://example.com/hist-files.bin",
                total_length=321,
                result="failed",
                reason="network error",
            )
            db.add(history)
            await db.flush()
            assert history.id is not None

        result = await handler.handle("aria2.getFiles", [f"hist-{history.id}"])
        assert len(result) == 1
        assert result[0]["path"] == "hist-files.bin"
        assert result[0]["length"] == "321"

    async def test_get_uris_supports_history_gid(self, handler):
        async with get_session() as db:
            history = TaskHistory(
                owner_id=handler.user_id,
                task_name="hist-uris.bin",
                uri="https://user:pass@example.com/hist-uris.bin",
                total_length=1,
                result="completed",
            )
            db.add(history)
            await db.flush()
            assert history.id is not None

        result = await handler.handle("aria2.getUris", [f"hist-{history.id}"])
        assert result == [{"uri": "https://***:***@example.com/hist-uris.bin", "status": "used"}]

    async def test_get_peers_servers_support_history_gid(self, handler):
        async with get_session() as db:
            history = TaskHistory(
                owner_id=handler.user_id,
                task_name="hist-peers.bin",
                uri="https://example.com/hist-peers.bin",
                total_length=1,
                result="cancelled",
            )
            db.add(history)
            await db.flush()
            assert history.id is not None

        peers = await handler.handle("aria2.getPeers", [f"hist-{history.id}"])
        servers = await handler.handle("aria2.getServers", [f"hist-{history.id}"])
        assert peers == []
        assert servers == []

    async def test_tell_status_history_error_keeps_name_in_files(self, handler):
        async with get_session() as db:
            history = TaskHistory(
                owner_id=handler.user_id,
                task_name="ubuntu.iso",
                uri="https://example.com/ubuntu.iso",
                total_length=2048,
                result="failed",
                reason="连接超时",
            )
            db.add(history)
            await db.flush()
            assert history.id is not None

        result = await handler.handle("aria2.tellStatus", [f"hist-{history.id}"])
        assert result["status"] == "error"
        assert result["errorMessage"] == "连接超时"
        assert result["completedLength"] == "0"
        assert result["files"][0]["path"] == "ubuntu.iso"
        assert result["files"][0]["completedLength"] == "0"

    async def test_tell_status_history_cancelled_maps_to_removed(self, handler):
        async with get_session() as db:
            history = TaskHistory(
                owner_id=handler.user_id,
                task_name="cancelled-task.iso",
                uri="https://example.com/cancelled-task.iso",
                total_length=4096,
                result="cancelled",
                reason="用户取消",
            )
            db.add(history)
            await db.flush()
            assert history.id is not None

        result = await handler.handle("aria2.tellStatus", [f"hist-{history.id}"])
        assert result["status"] == "removed"
        assert result["errorCode"] == "0"
        assert result["errorMessage"] == ""
        assert result["completedLength"] == "0"
        assert result["files"][0]["path"] == "cancelled-task.iso"

    async def test_tell_status_db_fallback_uses_task_name(self, handler):
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="hash-db-fallback-name",
                uri="https://example.com/releases/archive.tar.gz",
                gid="gid-db-fallback-name",
                status="error",
                name="archive.tar.gz",
                total_length=4096,
                completed_length=1024,
                error_display="连接被拒绝",
            )
            db.add(task)
            await db.flush()
            assert task.id is not None
            db.add(UserTaskSubscription(owner_id=handler.user_id, task_id=task.id, status="pending"))

        handler.client.tell_status.side_effect = RuntimeError("rpc unavailable")
        result = await handler.handle("aria2.tellStatus", ["gid-db-fallback-name"])

        assert result["status"] == "error"
        assert result["errorMessage"] == "连接被拒绝"
        assert result["files"][0]["path"] == "archive.tar.gz"

    async def test_tell_status_db_fallback_uses_uri_basename_when_name_missing(self, handler):
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="hash-db-fallback-uri",
                uri="https://example.com/downloads/video.mp4?token=abc",
                gid="gid-db-fallback-uri",
                status="error",
                name=None,
                total_length=100,
                completed_length=10,
                error_display="后端错误",
            )
            db.add(task)
            await db.flush()
            assert task.id is not None
            db.add(UserTaskSubscription(owner_id=handler.user_id, task_id=task.id, status="pending"))

        handler.client.tell_status.side_effect = RuntimeError("rpc unavailable")
        result = await handler.handle("aria2.tellStatus", ["gid-db-fallback-uri"])

        assert result["files"][0]["path"] == "video.mp4"

    async def test_tell_status_success_without_files_uses_task_name(self, handler):
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="hash-live-no-files",
                uri="https://example.com/live/task.bin",
                gid="gid-live-no-files",
                status="active",
                name="task.bin",
                total_length=500,
                completed_length=200,
            )
            db.add(task)
            await db.flush()
            assert task.id is not None
            db.add(UserTaskSubscription(owner_id=handler.user_id, task_id=task.id, status="pending"))

        handler.client.tell_status.return_value = {
            "gid": "gid-live-no-files",
            "status": "active",
            "totalLength": "500",
            "completedLength": "200",
            "downloadSpeed": "12",
            "uploadSpeed": "0",
        }

        result = await handler.handle("aria2.tellStatus", ["gid-live-no-files"])
        assert result["files"][0]["path"] == "task.bin"
        assert result["files"][0]["completedLength"] == "200"

    async def test_tell_status_preserves_multifile_when_one_file_has_name(self, handler):
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="hash-live-multi",
                uri="https://example.com/live/multi.zip",
                gid="gid-live-multi",
                status="active",
                name="multi.zip",
                total_length=1000,
                completed_length=500,
            )
            db.add(task)
            await db.flush()
            assert task.id is not None
            db.add(UserTaskSubscription(owner_id=handler.user_id, task_id=task.id, status="pending"))

        handler.client.tell_status.return_value = {
            "gid": "gid-live-multi",
            "status": "active",
            "totalLength": "1000",
            "completedLength": "500",
            "files": [
                {"path": "", "length": "300", "completedLength": "100"},
                {"path": "/downloads/real-name.bin", "length": "700", "completedLength": "400"},
            ],
        }

        result = await handler.handle("aria2.tellStatus", ["gid-live-multi"])
        assert len(result["files"]) == 2
        assert result["files"][1]["path"] == "real-name.bin"

    async def test_get_files_task_not_found(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.getFiles", ["nonexistent_gid"])
        assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND

    async def test_get_uris_task_not_found(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.getUris", ["nonexistent_gid"])
        assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND

    async def test_get_uris_masks_credentials(self, handler):
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="hash-uri-1",
                uri="https://example.com/resource.bin",
                gid="gid-uri-1",
                status="active",
                name="uri-test",
            )
            db.add(task)
            await db.flush()
            assert task.id is not None
            db.add(UserTaskSubscription(owner_id=handler.user_id, task_id=task.id, status="pending"))

        handler.client.get_uris.return_value = [
            {"uri": "https://user:pass@example.com/resource.bin", "status": "used"},
            {"uri": "https://example.com/next.bin", "status": "unknown"},
        ]

        result = await handler.handle("aria2.getUris", ["gid-uri-1"])
        assert len(result) == 2
        assert result[0]["uri"] == "https://***:***@example.com/resource.bin"
        assert result[0]["status"] == "used"
        assert result[1]["status"] == "waiting"

    async def test_get_version_returns_platform_shape(self, handler):
        handler.client.get_version.return_value = {
            "version": "1.37.0",
            "enabledFeatures": ["BitTorrent", "AsyncDNS"],
            "extra": "ignored",
        }
        result = await handler.handle("aria2.getVersion", [])
        assert result == {"version": "1.37.0", "enabledFeatures": ["BitTorrent", "AsyncDNS"]}

    async def test_get_version_fallback_when_backend_error(self, handler):
        handler.client.get_version.side_effect = RuntimeError("backend down")
        result = await handler.handle("aria2.getVersion", [])
        assert result == {"version": "aria2deck-proxy", "enabledFeatures": []}

    async def test_get_peers_masks_sensitive_fields(self, handler):
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="hash-peer-1",
                uri="magnet:?xt=urn:btih:1234567890abcdef1234567890abcdef12345678",
                gid="gid-peer-1",
                status="active",
                name="peer-test",
            )
            db.add(task)
            await db.flush()
            assert task.id is not None
            db.add(UserTaskSubscription(owner_id=handler.user_id, task_id=task.id, status="pending"))

        handler.client.get_peers.return_value = [
            {
                "peerId": "peer-raw",
                "ip": "8.8.8.8",
                "port": "6881",
                "bitfield": "ffff",
                "amChoking": "true",
                "peerChoking": "false",
                "downloadSpeed": "1200",
                "uploadSpeed": "300",
                "seeder": "true",
            }
        ]

        result = await handler.handle("aria2.getPeers", ["gid-peer-1"])
        assert len(result) == 1
        assert result[0]["peerId"] == "masked-peer"
        assert result[0]["ip"] == "0.0.0.0"
        assert result[0]["port"] == "0"
        assert result[0]["downloadSpeed"] == "1200"
        assert result[0]["uploadSpeed"] == "300"

    async def test_get_servers_masks_sensitive_fields(self, handler):
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="hash-server-1",
                uri="https://example.com/resource.bin",
                gid="gid-server-1",
                status="active",
                name="server-test",
            )
            db.add(task)
            await db.flush()
            assert task.id is not None
            db.add(UserTaskSubscription(owner_id=handler.user_id, task_id=task.id, status="pending"))

        handler.client.get_servers.return_value = [
            {
                "index": "1",
                "servers": [
                    {
                        "uri": "https://example.com/resource.bin",
                        "currentUri": "https://cdn.example.com/resource.bin",
                        "downloadSpeed": "2048",
                    }
                ],
            }
        ]

        result = await handler.handle("aria2.getServers", ["gid-server-1"])
        assert len(result) == 1
        assert result[0]["index"] == "1"
        assert len(result[0]["servers"]) == 1
        assert result[0]["servers"][0]["uri"] == ""
        assert result[0]["servers"][0]["currentUri"] == ""
        assert result[0]["servers"][0]["downloadSpeed"] == "2048"


@pytest.mark.asyncio
class TestAria2RpcHandlerTellMethods:

    async def test_tell_waiting_empty_params(self, handler):
        result = await handler.handle("aria2.tellWaiting", [])
        assert result == []

    async def test_tell_stopped_empty_params(self, handler):
        result = await handler.handle("aria2.tellStopped", [])
        assert result == []

    async def test_tell_waiting_negative_offset_reverse_order(self, handler):
        handler.client.tell_waiting.return_value = [
            {"gid": "x-1"},
            {"gid": "u-1"},
            {"gid": "x-2"},
            {"gid": "u-2"},
        ]
        handler._get_user_gids = AsyncMock(return_value={"u-1", "u-2"})

        result = await handler.handle("aria2.tellWaiting", [-1, 2])
        assert [item["gid"] for item in result] == ["u-2", "u-1"]

    async def test_tell_waiting_non_integer_params_invalid(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.tellWaiting", ["1", 10])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS

    async def test_tell_waiting_paginated_after_filter(self, handler):
        handler.client.tell_waiting.return_value = [
            {"gid": "x-1"},
            {"gid": "u-1"},
            {"gid": "x-2"},
            {"gid": "u-2"},
        ]
        handler._get_user_gids = AsyncMock(return_value={"u-1", "u-2"})

        result = await handler.handle("aria2.tellWaiting", [1, 1])
        assert len(result) == 1
        assert result[0]["gid"] == "u-2"

    async def test_tell_active_filters_with_string_gid_set(self, handler):
        handler.client.tell_active.return_value = [
            {"gid": "u-1", "status": "active", "totalLength": "0", "completedLength": "0", "downloadSpeed": "0", "uploadSpeed": "0"},
            {"gid": "x-1", "status": "active", "totalLength": "0", "completedLength": "0", "downloadSpeed": "0", "uploadSpeed": "0"},
        ]
        handler._get_user_gids = AsyncMock(return_value={"u-1", "u-2"})

        result = await handler.handle("aria2.tellActive", [])
        assert len(result) == 1
        assert result[0]["gid"] == "u-1"

    async def test_tell_active_enriches_files_when_backend_missing_files(self, handler):
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="hash-active-enrich",
                uri="https://example.com/active.bin",
                gid="gid-active-enrich",
                status="queued",
                name="active.bin",
                total_length=1000,
                completed_length=333,
            )
            db.add(task)
            await db.flush()
            assert task.id is not None
            db.add(UserTaskSubscription(owner_id=handler.user_id, task_id=task.id, status="pending"))

        handler.client.tell_active.return_value = [
            {
                "gid": "gid-active-enrich",
                "status": "active",
                "totalLength": "1000",
                "completedLength": "333",
            }
        ]
        handler._get_user_gids = AsyncMock(return_value={"gid-active-enrich"})

        result = await handler.handle("aria2.tellActive", [])
        assert len(result) == 1
        assert result[0]["files"][0]["path"] == "active.bin"
        assert result[0]["files"][0]["completedLength"] == "333"

    async def test_tell_waiting_enriches_files_when_backend_missing_files(self, handler):
        async with get_session() as db:
            task = DownloadTask(
                uri_hash="hash-waiting-enrich",
                uri="https://example.com/waiting.bin",
                gid="gid-waiting-enrich",
                status="active",
                name="waiting.bin",
                total_length=300,
                completed_length=0,
            )
            db.add(task)
            await db.flush()
            assert task.id is not None
            db.add(UserTaskSubscription(owner_id=handler.user_id, task_id=task.id, status="pending"))

        handler.client.tell_waiting.return_value = [
            {
                "gid": "gid-waiting-enrich",
                "status": "waiting",
                "totalLength": "300",
                "completedLength": "0",
            }
        ]
        handler._get_user_gids = AsyncMock(return_value={"gid-waiting-enrich"})

        result = await handler.handle("aria2.tellWaiting", [0, 10])
        assert len(result) == 1
        assert result[0]["files"][0]["path"] == "waiting.bin"

    async def test_normalize_gid_collection_accepts_tuple_values(self, handler):
        gids = handler._normalize_gid_collection({("u-1",), ("u-2",)})
        assert gids == {"u-1", "u-2"}

    async def test_tell_stopped_negative_offset_reverse_order(self, handler):
        async with get_session() as db:
            h1 = TaskHistory(owner_id=handler.user_id, task_name="h1", uri="https://x/1", total_length=1, result="completed")
            h2 = TaskHistory(owner_id=handler.user_id, task_name="h2", uri="https://x/2", total_length=2, result="cancelled", reason="用户取消")
            h3 = TaskHistory(owner_id=handler.user_id, task_name="h3", uri="https://x/3", total_length=3, result="failed", reason="No peers")
            db.add(h1)
            db.add(h2)
            db.add(h3)
            await db.flush()
            assert h2.id is not None
            assert h3.id is not None

        result = await handler.handle("aria2.tellStopped", [-1, 2])
        assert [item["gid"] for item in result] == [f"hist-{h3.id}", f"hist-{h2.id}"]
        assert [item["status"] for item in result] == ["error", "removed"]
        assert [item["files"][0]["path"] for item in result] == ["h3", "h2"]


@pytest.mark.asyncio
class TestAria2RpcHandlerMulticall:

    async def test_multicall_invalid_params(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("system.multicall", [])
        assert exc_info.value.code == RpcErrorCode.INVALID_PARAMS

    async def test_multicall_invalid_method_call(self, handler):
        result = await handler.handle("system.multicall", [["not_a_dict"]])
        assert len(result) == 1
        assert result[0]["faultCode"] == RpcErrorCode.INVALID_PARAMS
        assert "faultString" in result[0]

    async def test_multicall_missing_method_name(self, handler):
        result = await handler.handle("system.multicall", [[{"params": []}]])
        assert len(result) == 1
        assert result[0]["faultCode"] == RpcErrorCode.METHOD_NOT_FOUND

    async def test_multicall_method_error(self, handler):
        result = await handler.handle("system.multicall", [[
            {"methodName": "aria2.tellStatus", "params": []}
        ]])
        assert len(result) == 1
        assert result[0]["faultCode"] == RpcErrorCode.INVALID_PARAMS
        assert "faultString" in result[0]

    async def test_multicall_strips_inner_token(self, handler):
        handler.client.get_version.return_value = {"version": "1.36.0"}
        result = await handler.handle("system.multicall", [[
            {"methodName": "aria2.getVersion", "params": ["token:inner"]}
        ]])
        assert len(result) == 1
        assert result[0][0]["version"] == "1.36.0"


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

    async def test_purge_download_result(self, handler):
        result = await handler.handle("aria2.purgeDownloadResult", [])
        assert result == "OK"

    async def test_remove_download_result(self, handler):
        with pytest.raises(RpcError) as exc_info:
            await handler.handle("aria2.removeDownloadResult", ["gid123"])
        assert exc_info.value.code == RpcErrorCode.TASK_NOT_FOUND

    async def test_remove_download_result_accepts_task_fallback_gid(self, handler):
        async with get_session() as db:
            task = DownloadTask(uri_hash="fallback-hash-1", uri="https://x/fallback", gid=None, status="complete")
            db.add(task)
            await db.flush()
            assert task.id is not None
            db.add(UserTaskSubscription(owner_id=handler.user_id, task_id=task.id, status="success"))
            db.add(TaskHistory(owner_id=handler.user_id, task_name="fallback", uri="https://x/fallback", result="completed"))

        result = await handler.handle("aria2.removeDownloadResult", [f"task-{task.id}"])
        assert result == "OK"

        async with get_session() as db:
            history = (
                await db.exec(
                    select(TaskHistory).where(
                        TaskHistory.owner_id == handler.user_id,
                        TaskHistory.uri == "https://x/fallback",
                    )
                )
            ).first()
            assert history is None

    async def test_remove_download_result_accepts_history_gid(self, handler):
        async with get_session() as db:
            history = TaskHistory(owner_id=handler.user_id, task_name="hist-only", uri="https://x/hist-only", total_length=10, result="cancelled")
            db.add(history)
            await db.flush()
            assert history.id is not None

        result = await handler.handle("aria2.removeDownloadResult", [f"hist-{history.id}"])
        assert result == "OK"

        async with get_session() as db:
            deleted = await db.get(TaskHistory, history.id)
            assert deleted is None

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

    async def test_get_global_stat_accepts_tuple_and_row_like_counts(self, handler):
        class _FakeRow:
            def __init__(self, value):
                self._mapping = {"count": value}

        class _FakeResult:
            def __init__(self, value):
                self._value = value

            def one(self):
                return self._value

        class _FakeDb:
            def __init__(self):
                self._values = iter([(1,), _FakeRow("2"), ("3",)])

            async def exec(self, _stmt):
                return _FakeResult(next(self._values))

        class _FakeSession:
            def __init__(self):
                self._db = _FakeDb()

            async def __aenter__(self):
                return self._db

            async def __aexit__(self, _exc_type, _exc, _tb):
                return False

        with patch("app.services.aria2_rpc_handler.get_session", return_value=_FakeSession()):
            result = await handler._handle_get_global_stat([])

        assert result["numActive"] == "1"
        assert result["numWaiting"] == "2"
        assert result["numStopped"] == "3"

    async def test_get_user_available_space_accepts_tuple_quota(self, handler):
        class _QuotaResult:
            def first(self):
                return ("1024",)

        class _QuotaDb:
            async def exec(self, _stmt):
                return _QuotaResult()

        class _QuotaSession:
            async def __aenter__(self):
                return _QuotaDb()

            async def __aexit__(self, _exc_type, _exc, _tb):
                return False

        with patch("app.services.aria2_rpc_handler.get_session", return_value=_QuotaSession()), \
             patch("app.services.aria2_rpc_handler.get_user_space_info", new_callable=AsyncMock, return_value={"available": 123}) as mock_space:
            available = await handler._get_user_available_space()

        assert available == 123
        mock_space.assert_awaited_once_with(handler.user_id, 1024)


@pytest.mark.asyncio
class TestAria2RpcHandlerGetSessionInfo:

    async def test_get_session_info_returns_session_id(self, handler):
        result = await handler.handle("aria2.getSessionInfo", [])
        assert "sessionId" in result
        assert isinstance(result["sessionId"], str)


@pytest.mark.asyncio
class TestAria2RpcHandlerUserSpaceExtended:

    async def test_get_user_available_space_no_user(self, handler, temp_db):
        from app.database import get_session
        from app.models import User
        async with get_session() as db:
            user = await db.get(User, handler.user_id)
            if user:
                await db.delete(user)
                await db.commit()
        result = await handler._get_user_available_space()
        assert result == 0

    async def test_check_disk_space_returns_tuple(self, handler):
        result = handler._check_disk_space()
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], int)

    async def test_get_user_available_space_with_user(self, handler, test_user, temp_db):
        result = await handler._get_user_available_space()
        assert result > 0

    async def test_sanitize_path_absolute_outside_user_dir(self, handler):
        result = handler._sanitize_path("/some/other/path/file.txt")
        assert result == "/some/other/path/file.txt"
