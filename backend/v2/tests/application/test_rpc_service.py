from __future__ import annotations

import pytest

from aria2deck.application.downloads.models import (
    DownloadRecord,
    DownloadWriteResult,
    ObservedFile,
    TaskProjection,
)
from aria2deck.application.downloads.rpc_projection import VirtualGidSpace
from aria2deck.application.downloads.rpc_service import (
    RPC_METHODS,
    Aria2RpcService,
    RpcMethodError,
)
from aria2deck.domain.downloads import AttemptStatus, SubscriptionStatus
from aria2deck.domain.errors import DomainError, ErrorCode

USER_ID = 1
RAW_GID = "0123456789abcdef"


def _record(
    task_id: int,
    status: SubscriptionStatus,
    attempt_status: AttemptStatus = AttemptStatus.ACTIVE,
) -> DownloadRecord:
    return DownloadRecord(
        id=task_id,
        attempt_id=task_id * 10,
        gid=RAW_GID,
        subscription_status=status,
        attempt_status=attempt_status,
        name="secret-name.bin",
        display_uri="https://example.com/展示链接",
        total_bytes=100,
        completed_bytes=40,
        reserved_bytes=0,
        error_code=None,
        created_at_ms=1,
        updated_at_ms=2,
        finished_at_ms=None,
    )


def _projection(record: DownloadRecord) -> TaskProjection:
    return TaskProjection(
        id=record.id,
        status=record.subscription_status,
        status_label="标签",
        name=record.name,
        display_uri=record.display_uri,
        total_bytes=record.total_bytes,
        completed_bytes=record.completed_bytes,
        download_speed=10,
        upload_speed=1,
        peer_count=0,
        connection_count=2,
        files=(ObservedFile("folder/leaf.bin", 100, 40, True),),
        observation_fresh=True,
        reserved_bytes=0,
        error_code=None,
        retryable=False,
        created_at_ms=record.created_at_ms,
        updated_at_ms=record.updated_at_ms,
        finished_at_ms=None,
    )


class FakeQueries:
    def __init__(self, records: list[DownloadRecord]) -> None:
        self.records = records

    async def list_user_records(self, *, user_id: int) -> list[DownloadRecord]:
        return [record for record in self.records if True]

    async def get_task(self, *, user_id: int, task_id: int) -> TaskProjection:
        for record in self.records:
            if record.id == task_id:
                return _projection(record)
        raise DomainError(ErrorCode.NOT_FOUND, "任务不存在")

    async def stats(self, *, user_id: int) -> object:
        from aria2deck.application.downloads.models import TaskStats
        from aria2deck.domain.quota import Quota

        counts = {status: 0 for status in SubscriptionStatus}
        for record in self.records:
            counts[record.subscription_status] += 1
        from aria2deck.domain.quota import VisibleSpace

        return TaskStats(counts=counts, total=len(self.records), download_speed=5, upload_speed=1, quota=Quota(100, 0, 0), disk=VisibleSpace(100, 0, 0, 100, False))


class FakeCommands:
    def __init__(self, *, stop_confirmed: bool | None = True) -> None:
        self.cancelled: list[int] = []
        self.stop_confirmed = stop_confirmed

    async def create_uris(self, *, user_id: int, items: list) -> list:
        raise DomainError(ErrorCode.INVALID_INPUT, "不应在单元测试中触发真实创建")

    async def create_torrent(self, *, user_id: int, payload_base64: str, selected_file_indexes):
        raise DomainError(ErrorCode.INVALID_INPUT, "不应在单元测试中触发真实创建")

    async def cancel(self, *, user_id: int, task_ids: list[int]) -> list:
        self.cancelled.extend(task_ids)
        return [
            DownloadWriteResult(
                accepted=True,
                task_id=task_id,
                stop_confirmed=self.stop_confirmed,
            )
            for task_id in task_ids
        ]


class FakeHistory:
    def __init__(self) -> None:
        self.deleted: list[int] = []

    async def delete(self, *, user_id: int, task_ids: list[int]) -> list:
        self.deleted.extend(task_ids)
        return [DownloadWriteResult(accepted=True, task_id=task_id) for task_id in task_ids]


def _service(records: list[DownloadRecord]) -> tuple:
    gids = VirtualGidSpace(b"prefix-bytes-16".ljust(16, b"0")[:16])
    service = Aria2RpcService(
        queries=FakeQueries(records), commands=FakeCommands(), history=FakeHistory(), gids=gids
    )
    return service, gids


async def test_list_methods_returns_exactly_thirty() -> None:
    service, _ = _service([])
    methods = await service.dispatch("system.listMethods", [], user_id=USER_ID)
    assert len(methods) == 30, f"恰好 30 个方法，实际 {len(methods)}"
    assert set(methods) == set(RPC_METHODS)
    assert "system.multicall" in methods


async def test_unknown_method_is_rejected() -> None:
    service, _ = _service([])
    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch("aria2.changeOption", [], user_id=USER_ID)
    assert caught.value.code == -32601


@pytest.mark.parametrize(
    "method",
    [
        "aria2.pause",
        "aria2.forcePause",
        "aria2.unpause",
        "aria2.pauseAll",
        "aria2.forcePauseAll",
        "aria2.unpauseAll",
    ],
)
async def test_pause_family_rejected_with_code_1(method: str) -> None:
    service, _ = _service([])
    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch(method, ["g"], user_id=USER_ID)
    assert caught.value.code == 1
    assert "不受支持" in str(caught.value)


async def test_change_uri_rejected_with_code_2() -> None:
    service, _ = _service([])
    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch("aria2.changeUri", ["g", 0, ["a"], ["b"]], user_id=USER_ID)
    assert caught.value.code == 2


async def test_multicall_dispatch_entry_rejects_nesting() -> None:
    service, _ = _service([])
    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch(
            "system.multicall", [[{"methodName": "aria2.tellActive", "params": []}]], user_id=USER_ID
        )
    assert caught.value.code == -32602


async def test_tell_status_resolves_virtual_and_raw_gid_without_echo() -> None:
    records = [_record(7, SubscriptionStatus.ACTIVE)]
    service, gids = _service(records)
    virtual = gids.virtual_gid(7)

    by_virtual = await service.dispatch("aria2.tellStatus", [virtual], user_id=USER_ID)
    assert by_virtual["gid"] == virtual
    assert by_virtual["status"] == "active"

    # raw GID 兼容查询：能查到，但回显请求传入的字符串。
    by_raw = await service.dispatch("aria2.tellStatus", [RAW_GID], user_id=USER_ID)
    assert by_raw["gid"] == RAW_GID
    assert by_raw["gid"] != records[0].gid or RAW_GID == records[0].gid

    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch("aria2.tellStatus", ["unknown000000000"], user_id=USER_ID)
    assert caught.value.code == 1


@pytest.mark.parametrize(
    "method,bucket_statuses",
    [
        ("aria2.tellActive", {SubscriptionStatus.ACTIVE}),
        (
            "aria2.tellWaiting",
            {SubscriptionStatus.QUEUED, SubscriptionStatus.WAITING, SubscriptionStatus.PAUSED},
        ),
        (
            "aria2.tellStopped",
            {
                SubscriptionStatus.COMPLETED,
                SubscriptionStatus.FAILED,
                SubscriptionStatus.CANCELLED,
                SubscriptionStatus.EXPIRED,
            },
        ),
    ],
)
async def test_tell_buckets_partition_statuses(method: str, bucket_statuses: set) -> None:
    records = [_record(index, status) for index, status in enumerate(SubscriptionStatus)]
    service, gids = _service(records)
    if "Waiting" in method or "Stopped" in method:
        items = await service.dispatch(method, [0, 100], user_id=USER_ID)
    else:
        items = await service.dispatch(method, [], user_id=USER_ID)
    statuses = {item["status"] for item in items}
    from aria2deck.application.downloads.rpc_projection import aria2_status

    assert statuses == {aria2_status(status) for status in bucket_statuses}
    assert all(len(item["gid"]) == 16 for item in items)


async def test_tell_waiting_offset_and_bounds() -> None:
    records = [_record(index, SubscriptionStatus.WAITING) for index in range(1, 6)]
    service, _ = _service(records)
    page = await service.dispatch("aria2.tellWaiting", [1, 2], user_id=USER_ID)
    assert len(page) == 2
    negative = await service.dispatch("aria2.tellWaiting", [-2, 2], user_id=USER_ID)
    assert len(negative) == 2
    beyond = await service.dispatch("aria2.tellWaiting", [99, 5], user_id=USER_ID)
    assert beyond == []


async def test_get_files_uris_and_projection_safety() -> None:
    service, gids = _service([_record(3, SubscriptionStatus.COMPLETED)])
    gid = gids.virtual_gid(3)
    files = await service.dispatch("aria2.getFiles", [gid], user_id=USER_ID)
    assert files[0]["path"] == "leaf.bin"
    uris = await service.dispatch("aria2.getUris", [gid], user_id=USER_ID)
    assert uris == [{"uri": "https://example.com/展示链接", "status": "used"}]


async def test_global_stat_version_session_option_compat() -> None:
    service, _ = _service([_record(1, SubscriptionStatus.ACTIVE), _record(2, SubscriptionStatus.FAILED)])
    stat = await service.dispatch("aria2.getGlobalStat", [], user_id=USER_ID)
    assert stat["numActive"] == "1" and stat["numStopped"] == "1"
    version = await service.dispatch("aria2.getVersion", [], user_id=USER_ID)
    assert version["version"]
    session = await service.dispatch("aria2.getSessionInfo", [], user_id=USER_ID)
    assert "sessionId" in session
    saved = await service.dispatch("aria2.saveSession", [], user_id=USER_ID)
    assert saved == "OK"
    options = await service.dispatch("aria2.getGlobalOption", [], user_id=USER_ID)
    assert "dir" in options and "capability" not in options


async def test_peers_and_servers_return_empty_collections() -> None:
    service, gids = _service([_record(5, SubscriptionStatus.ACTIVE)])
    gid = gids.virtual_gid(5)
    assert await service.dispatch("aria2.getPeers", [gid], user_id=USER_ID) == []
    assert await service.dispatch("aria2.getServers", [gid], user_id=USER_ID) == []
    with pytest.raises(RpcMethodError):
        await service.dispatch("aria2.getPeers", ["unknown"], user_id=USER_ID)


async def test_remove_and_force_remove_cancel_live_tasks() -> None:
    service, gids = _service([_record(9, SubscriptionStatus.ACTIVE)])
    gid = gids.virtual_gid(9)
    assert await service.dispatch("aria2.remove", [gid], user_id=USER_ID) == gid
    assert await service.dispatch("aria2.forceRemove", [gid], user_id=USER_ID) == gid
    assert service.commands.cancelled == [9, 9]

    terminal, terminal_gids = _service([_record(10, SubscriptionStatus.COMPLETED)])
    with pytest.raises(RpcMethodError) as caught:
        await terminal.dispatch("aria2.remove", [terminal_gids.virtual_gid(10)], user_id=USER_ID)
    assert caught.value.code == 1

    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch("aria2.remove", ["nosuchgid000000"], user_id=USER_ID)
    assert caught.value.code == 1


@pytest.mark.parametrize("method", ["aria2.remove", "aria2.forceRemove"])
@pytest.mark.parametrize("stop_confirmed", [False, None])
async def test_remove_requires_confirmed_stop(
    method: str, stop_confirmed: bool | None
) -> None:
    service, gids = _service([_record(9, SubscriptionStatus.ACTIVE)])
    service.commands = FakeCommands(stop_confirmed=stop_confirmed)
    gid = gids.virtual_gid(9)

    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch(method, [gid], user_id=USER_ID)

    assert caught.value.code == 1
    assert "停止结果无法确认" in str(caught.value)


async def test_remove_preserves_business_rejection() -> None:
    class RejectingCommands(FakeCommands):
        async def cancel(self, *, user_id: int, task_ids: list[int]) -> list:
            return [
                DownloadWriteResult(
                    accepted=False,
                    task_id=task_ids[0],
                    message="任务状态不允许取消",
                    stop_confirmed=True,
                )
            ]

    service, gids = _service([_record(9, SubscriptionStatus.ACTIVE)])
    service.commands = RejectingCommands()

    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch(
            "aria2.remove", [gids.virtual_gid(9)], user_id=USER_ID
        )

    assert caught.value.code == 1
    assert str(caught.value) == "任务状态不允许取消"


async def test_change_position_returns_compat_value() -> None:
    service, gids = _service([_record(4, SubscriptionStatus.WAITING)])
    position = await service.dispatch(
        "aria2.changePosition", [gids.virtual_gid(4), 0, "POS_SET"], user_id=USER_ID
    )
    assert position == 0
    with pytest.raises(RpcMethodError):
        await service.dispatch("aria2.changePosition", ["unknowngid00000", 0, "POS_SET"], user_id=USER_ID)


async def test_remove_and_purge_download_result_clear_terminal_only() -> None:
    service, gids = _service(
        [
            _record(1, SubscriptionStatus.COMPLETED),
            _record(2, SubscriptionStatus.FAILED),
            _record(3, SubscriptionStatus.ACTIVE),
        ]
    )
    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch("aria2.removeDownloadResult", [gids.virtual_gid(3)], user_id=USER_ID)
    assert caught.value.code == 1
    assert service.history.deleted == []

    assert await service.dispatch("aria2.removeDownloadResult", [gids.virtual_gid(1)], user_id=USER_ID) == "OK"
    assert service.history.deleted == [1]

    assert await service.dispatch("aria2.purgeDownloadResult", [], user_id=USER_ID) == "OK"
    assert service.history.deleted == [1, 1, 2], "清本人全部终态订阅，不影响进行中"


class RecordingCommands(FakeCommands):
    def __init__(self) -> None:
        super().__init__()
        self.uri_items: list[object] = []
        self.torrent_calls: list[dict[str, object]] = []

    async def create_uris(self, *, user_id: int, items: list) -> list:
        self.uri_items.extend(items)
        return [DownloadWriteResult(accepted=True, task_id=42)]

    async def create_torrent(self, *, user_id: int, payload_base64: str, selected_file_indexes):
        self.torrent_calls.append(
            {"payload": payload_base64, "selected": selected_file_indexes}
        )
        return DownloadWriteResult(accepted=True, task_id=99)


async def test_adduri_wraps_single_creation_with_mirrors() -> None:
    service, gids = _service([])
    service.commands = RecordingCommands()
    gid = await service.dispatch(
        "aria2.addUri",
        [["https://example.com/a.bin", "https://mirror/b.bin"], {"out": "/etc/x"}],
        user_id=USER_ID,
    )
    assert gid == gids.virtual_gid(42)
    item = service.commands.uri_items[0]
    assert item.uri == "https://example.com/a.bin"
    assert item.options["mirrors"] == ["https://mirror/b.bin"]  # type: ignore[index]

    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch("aria2.addUri", [[], []], user_id=USER_ID)
    assert caught.value.code == 1
    with pytest.raises(RpcMethodError):
        await service.dispatch("aria2.addUri", ["not-a-list"], user_id=USER_ID)
    with pytest.raises(RpcMethodError):
        await service.dispatch("aria2.addUri", [], user_id=USER_ID)

    class Rejecting(RecordingCommands):
        async def create_uris(self, *, user_id: int, items: list) -> list:
            return [DownloadWriteResult(accepted=False, message="FTP 不支持")]

    service.commands = Rejecting()
    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch("aria2.addUri", [["ftp://x"]], user_id=USER_ID)
    assert caught.value.code == 1


async def test_addtorrent_option_parsing_paths() -> None:
    service, gids = _service([])
    service.commands = RecordingCommands()

    gid = await service.dispatch(
        "aria2.addTorrent", ["dG9ycmVudA==", {"select-file": "1,3"}], user_id=USER_ID
    )
    assert gid == gids.virtual_gid(99)
    assert service.commands.torrent_calls[0]["selected"] == [1, 3]

    await service.dispatch(
        "aria2.addTorrent", ["dG9ycmVudA==", {"select-file": [0, 2]}], user_id=USER_ID
    )
    assert service.commands.torrent_calls[1]["selected"] == [0, 2]

    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch("aria2.addTorrent", ["dG9ycmVudA==", {"select-file": 5}], user_id=USER_ID)
    assert caught.value.code == 1
    with pytest.raises(RpcMethodError):
        await service.dispatch("aria2.addTorrent", ["dG9ycmVudA==", {"bt-tracker": "http://t"}], user_id=USER_ID)
    with pytest.raises(RpcMethodError):
        await service.dispatch("aria2.addTorrent", ["", []], user_id=USER_ID)
    with pytest.raises(RpcMethodError):
        await service.dispatch("aria2.addTorrent", [], user_id=USER_ID)

    class FailingTorrent(RecordingCommands):
        async def create_torrent(self, *, user_id: int, payload_base64: str, selected_file_indexes):
            return DownloadWriteResult(accepted=False, message="种子无效")

    service.commands = FailingTorrent()
    with pytest.raises(RpcMethodError):
        await service.dispatch("aria2.addTorrent", ["dG9ycmVudA=="], user_id=USER_ID)


async def test_keys_and_int_param_edges() -> None:
    service, gids = _service([_record(8, SubscriptionStatus.WAITING)])
    gid = gids.virtual_gid(8)
    # keys 过滤 + 空列表视为 None + 非整数 offset 走默认值。
    payload = await service.dispatch("aria2.tellStatus", [gid, ["gid", "status"]], user_id=USER_ID)
    assert set(payload) == {"gid", "status"}
    empty_keys = await service.dispatch("aria2.tellStatus", [gid, []], user_id=USER_ID)
    assert "status" in empty_keys
    defaulted = await service.dispatch("aria2.tellWaiting", ["not-int", 0], user_id=USER_ID)
    assert defaulted == []  # 非整数 offset 走默认 0；num=0 → 空。
    boolean_offset = await service.dispatch("aria2.tellWaiting", [True, 0], user_id=USER_ID)
    assert boolean_offset == []


async def test_get_option_resolves_raw_gid_too() -> None:
    service, _ = _service([_record(6, SubscriptionStatus.ACTIVE)])
    options = await service.dispatch("aria2.getOption", [RAW_GID], user_id=USER_ID)
    assert options["out"] == "secret-name.bin"
