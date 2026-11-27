from __future__ import annotations

import logging

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
        files=(ObservedFile("folder/leaf.bin", 100, 40, True, "folder/leaf.bin"),),
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

    async def create_uris(self, *, user_id: int, items: list, submit: bool = True) -> list:
        raise DomainError(ErrorCode.INVALID_INPUT, "不应在单元测试中触发真实创建")

    async def create_torrent(
        self,
        *,
        user_id: int,
        payload_base64: str,
        selected_file_indexes,
        allow_rename: bool = True,
        submit: bool = True,
    ):
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
    assert files[0]["path"] == "./leaf.bin"
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


async def test_peers_empty_and_servers_mirror_file_indexes() -> None:
    service, gids = _service([_record(5, SubscriptionStatus.ACTIVE)])
    gid = gids.virtual_gid(5)
    assert await service.dispatch("aria2.getPeers", [gid], user_id=USER_ID) == []
    # 单文件观测：条目与 getFiles 的 index 一一对应，servers 恒空。
    assert await service.dispatch("aria2.getServers", [gid], user_id=USER_ID) == [
        {"index": "1", "servers": []}
    ]
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
        self.uri_submit: list[bool] = []
        self.torrent_calls: list[dict[str, object]] = []

    async def create_uris(self, *, user_id: int, items: list, submit: bool = True) -> list:
        self.uri_items.extend(items)
        self.uri_submit.append(submit)
        return [DownloadWriteResult(accepted=True, task_id=42)]

    async def create_torrent(
        self,
        *,
        user_id: int,
        payload_base64: str,
        selected_file_indexes,
        allow_rename: bool = True,
        submit: bool = True,
    ):
        self.torrent_calls.append(
            {
                "payload": payload_base64,
                "selected": selected_file_indexes,
                "allow_rename": allow_rename,
                "submit": submit,
            }
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
        async def create_uris(
            self, *, user_id: int, items: list, submit: bool = True
        ) -> list:
            return [DownloadWriteResult(accepted=False, message="FTP 不支持")]

    service.commands = Rejecting()
    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch("aria2.addUri", [["ftp://x"]], user_id=USER_ID)
    assert caught.value.code == 1


async def test_rpc_creation_registers_without_waiting_for_submission() -> None:
    """RPC-10/11 契约：RPC 面只登记（submit=False），提交交给恢复循环。

    修复前的行为是同步提交：aria2 不可达会把已入队的任务报成添加失败。
    """
    service, gids = _service([])
    service.commands = RecordingCommands()

    gid = await service.dispatch(
        "aria2.addUri", [["https://example.com/a.bin"]], user_id=USER_ID
    )
    await service.dispatch("aria2.addTorrent", ["dG9ycmVudA=="], user_id=USER_ID)

    assert gid == gids.virtual_gid(42)
    assert service.commands.uri_submit == [False]
    assert service.commands.torrent_calls[-1]["submit"] is False


async def test_adduri_ignores_unknown_aria2_client_options(caplog) -> None:
    """回归：AriaNg/Motrix 附带的标准 aria2 选项不得让 addUri 失败。"""
    service, gids = _service([])
    service.commands = RecordingCommands()

    with caplog.at_level(logging.DEBUG, logger="aria2deck.application.downloads.rpc_service"):
        gid = await service.dispatch(
            "aria2.addUri",
            [
                ["https://example.com/a.zip"],
                {
                    "pause": "true",
                    "dir": "/data/downloads",
                    "split": "8",
                    "user-agent": "Motrix",
                    "out": "改名.zip",
                    "max-connection-per-server": "4",
                },
            ],
            user_id=USER_ID,
        )

    assert gid == gids.virtual_gid(42)
    assert service.commands.uri_items[0].options == {
        "out": "改名.zip",
        "max-connection-per-server": "4",
    }
    assert "split" in caplog.text, "被丢弃的选项键名需要留痕"
    assert "/data/downloads" not in caplog.text and "Motrix" not in caplog.text, "日志只记键名，不记值"


async def test_adduri_maps_aria2_header_option_to_headers() -> None:
    """规范 header（"Name: Value" 数组）→ 内部 headers dict，原始大小写保留。"""
    service, _ = _service([])
    service.commands = RecordingCommands()

    await service.dispatch(
        "aria2.addUri",
        [
            ["https://example.com/a.bin"],
            {"header": ["Referer: https://a", "User-Agent: X"]},
        ],
        user_id=USER_ID,
    )

    assert service.commands.uri_items[0].options == {
        "headers": {"Referer": "https://a", "User-Agent": "X"}
    }


async def test_header_option_single_string_and_later_wins(caplog) -> None:
    """header 也接受单个字符串（规范允许）；同名后者覆盖；非法项忽略不报错。"""
    import logging

    service, _ = _service([])
    service.commands = RecordingCommands()

    with caplog.at_level(logging.DEBUG, logger="aria2deck.application.downloads.rpc_service"):
        await service.dispatch(
            "aria2.addUri",
            [["https://example.com/a.bin"], {"header": "X-Single: 1"}],
            user_id=USER_ID,
        )
        await service.dispatch(
            "aria2.addUri",
            [["https://example.com/b.bin"], {"header": ["X-Dup: 老值", "X-Dup: 新值"]}],
            user_id=USER_ID,
        )
        await service.dispatch(
            "aria2.addUri",
            [
                ["https://example.com/c.bin"],
                {"header": ["缺冒号", 123, ": 空头名", "X-Ok: 值"], "pause": "true"},
            ],
            user_id=USER_ID,
        )
        # 全部非法 → 不产生 headers 键，也不报错。
        await service.dispatch(
            "aria2.addUri", [["https://example.com/d.bin"], {"header": ["缺冒号"]}], user_id=USER_ID
        )

    assert service.commands.uri_items[0].options == {"headers": {"X-Single": "1"}}
    assert service.commands.uri_items[1].options == {"headers": {"X-Dup": "新值"}}
    assert service.commands.uri_items[2].options == {"headers": {"X-Ok": "值"}}
    assert service.commands.uri_items[3].options == {}
    assert "忽略非法项" in caplog.text
    assert "缺冒号" not in caplog.text and "空头名" not in caplog.text, "只记个数，不记值"


async def test_header_option_merges_into_existing_headers_dict() -> None:
    service, _ = _service([])
    service.commands = RecordingCommands()

    await service.dispatch(
        "aria2.addUri",
        [["https://example.com/a.bin"], {"headers": {"X-A": "旧"}, "header": ["X-A: 新", "X-B: 2"]}],
        user_id=USER_ID,
    )

    assert service.commands.uri_items[0].options == {"headers": {"X-A": "新", "X-B": "2"}}


async def test_adduri_keeps_allow_rename_policy() -> None:
    """allow_rename 由 create_uris 消费（D7），过滤后必须原样送达。"""
    service, _ = _service([])
    service.commands = RecordingCommands()

    await service.dispatch(
        "aria2.addUri",
        [["https://example.com/a.zip"], {"out": "同名.zip", "allow_rename": "false", "pause": "true"}],
        user_id=USER_ID,
    )

    assert service.commands.uri_items[0].options == {"out": "同名.zip", "allow_rename": "false"}


async def test_addtorrent_option_parsing_paths() -> None:
    service, gids = _service([])
    service.commands = RecordingCommands()

    gid = await service.dispatch(
        "aria2.addTorrent", ["dG9ycmVudA==", {"select-file": "1,3"}], user_id=USER_ID
    )
    assert gid == gids.virtual_gid(99)
    assert service.commands.torrent_calls[0]["selected"] == [1, 3]
    assert service.commands.torrent_calls[0]["allow_rename"] is True

    await service.dispatch(
        "aria2.addTorrent",
        ["dG9ycmVudA==", {"select-file": [0, 2], "allow_rename": "false"}],
        user_id=USER_ID,
    )
    assert service.commands.torrent_calls[1]["selected"] == [0, 2]
    assert service.commands.torrent_calls[1]["allow_rename"] is False

    with pytest.raises(RpcMethodError):
        await service.dispatch(
            "aria2.addTorrent",
            ["dG9ycmVudA==", {"allow_rename": "yes"}],
            user_id=USER_ID,
        )

    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch("aria2.addTorrent", ["dG9ycmVudA==", {"select-file": 5}], user_id=USER_ID)
    assert caught.value.code == 1
    # 官方签名 addTorrent(torrent, [uris], [options], [position])：position 接受并忽略。
    await service.dispatch(
        "aria2.addTorrent",
        ["dG9ycmVudA==", [], {"pause": "true", "select-file": "2"}, 0],
        user_id=USER_ID,
    )
    assert service.commands.torrent_calls[-1]["selected"] == [2]

    # 未知种子选项只记键名后丢弃，不再整次拒绝（客户端兼容）。
    await service.dispatch(
        "aria2.addTorrent",
        ["dG9ycmVudA==", {"bt-tracker": "http://t", "pause": "true"}],
        user_id=USER_ID,
    )
    assert service.commands.torrent_calls[-1]["selected"] is None
    # 规范 header 选项在种子路径同样先转换再过滤：不报错，但 create_torrent 无 options 通道。
    await service.dispatch(
        "aria2.addTorrent",
        ["dG9ycmVudA==", {"header": ["X-A: 1"]}, ],
        user_id=USER_ID,
    )
    assert service.commands.torrent_calls[-1]["selected"] is None
    with pytest.raises(RpcMethodError):
        await service.dispatch("aria2.addTorrent", ["", []], user_id=USER_ID)
    with pytest.raises(RpcMethodError):
        await service.dispatch("aria2.addTorrent", [], user_id=USER_ID)

    class FailingTorrent(RecordingCommands):
        async def create_torrent(
            self,
            *,
            user_id: int,
            payload_base64: str,
            selected_file_indexes,
            allow_rename: bool = True,
            submit: bool = True,
        ):
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
    # 非数值形态才报错（规范内字符串数字走宽容转换，见 tellWaiting 专项用例）。
    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch("aria2.tellWaiting", ["not-int", 0], user_id=USER_ID)
    assert caught.value.code == 1 and "offset" in str(caught.value)
    with pytest.raises(RpcMethodError):
        await service.dispatch("aria2.tellWaiting", [True, 0], user_id=USER_ID)


async def test_tell_waiting_accepts_string_numbers_and_keys(caplog) -> None:
    """客户端常把 offset/num 传成字符串：宽容转换后按窗口切片，keys 照常过滤。"""
    records = [_record(index, SubscriptionStatus.WAITING) for index in range(1, 6)]
    service, _ = _service(records)

    with caplog.at_level(logging.DEBUG, logger="aria2deck.application.downloads.rpc_service"):
        page = await service.dispatch(
            "aria2.tellWaiting", ["1", "2", ["gid", "status"], "extras-不应入日志"], user_id=USER_ID
        )

    assert len(page) == 2
    assert set(page[0]) == {"gid", "status"}
    assert "aria2.tellWaiting" in caplog.text, "方法名留痕"
    assert "extras-不应入日志" not in caplog.text, "只记个数，不记值"

    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch("aria2.tellWaiting", ["0", "abc"], user_id=USER_ID)
    assert "num" in str(caught.value)
    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch("aria2.tellWaiting", [None, 0], user_id=USER_ID)
    assert "offset" in str(caught.value)
    # 缺省 offset/num 走默认 0：num=0 → 空窗口。
    assert await service.dispatch("aria2.tellWaiting", [], user_id=USER_ID) == []


async def test_tell_keys_arrays_are_accepted() -> None:
    service, gids = _service([_record(8, SubscriptionStatus.ACTIVE)])
    gid = gids.virtual_gid(8)
    full = await service.dispatch("aria2.tellStatus", [gid], user_id=USER_ID)
    assert "status" in full
    subset = await service.dispatch("aria2.tellStatus", [gid, ["gid"], "多余"], user_id=USER_ID)
    assert set(subset) == {"gid"}

    active = await service.dispatch("aria2.tellActive", [["gid", "status"]], user_id=USER_ID)
    assert [set(item) for item in active] == [{"gid", "status"}]
    assert await service.dispatch("aria2.tellActive", [], user_id=USER_ID)


async def test_adduri_accepts_trailing_position_param() -> None:
    """官方签名 addUri(uris, [options], [position])：position 接受并忽略。"""
    service, gids = _service([])
    service.commands = RecordingCommands()

    gid = await service.dispatch(
        "aria2.addUri",
        [["https://example.com/a.zip"], {"pause": "true"}, 0],
        user_id=USER_ID,
    )

    assert gid == gids.virtual_gid(42)
    assert service.commands.uri_items[0].options == {}


async def test_change_position_accepts_spec_params() -> None:
    """changePosition(gid, pos, how)：字符串 pos 与三个 how 枚举都不得报错。"""
    service, gids = _service([_record(4, SubscriptionStatus.WAITING)])
    gid = gids.virtual_gid(4)

    for how in ("POS_SET", "POS_CUR", "POS_END"):
        assert await service.dispatch("aria2.changePosition", [gid, "0", how], user_id=USER_ID) == 0
    assert await service.dispatch("aria2.changePosition", [gid, 3, "POS_SET", "多余"], user_id=USER_ID) == 0
    assert await service.dispatch("aria2.changePosition", [gid, "2"], user_id=USER_ID) == 0
    assert await service.dispatch("aria2.changePosition", [gid], user_id=USER_ID) == 0

    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch("aria2.changePosition", [gid, "abc", "POS_SET"], user_id=USER_ID)
    assert "pos" in str(caught.value)
    with pytest.raises(RpcMethodError) as caught:
        await service.dispatch("aria2.changePosition", [gid, 0, "POS_MIDDLE"], user_id=USER_ID)
    assert "how" in str(caught.value)


@pytest.mark.parametrize(
    "method",
    [
        "aria2.getFiles",
        "aria2.getUris",
        "aria2.getPeers",
        "aria2.getServers",
        "aria2.getOption",
        "aria2.tellStatus",
    ],
)
async def test_gid_methods_tolerate_extra_params(method: str) -> None:
    service, gids = _service([_record(5, SubscriptionStatus.ACTIVE)])
    gid = gids.virtual_gid(5)

    baseline = await service.dispatch(method, [gid], user_id=USER_ID)
    assert await service.dispatch(method, [gid, "多余", 42], user_id=USER_ID) == baseline


@pytest.mark.parametrize("method", ["aria2.remove", "aria2.forceRemove"])
async def test_remove_tolerates_extra_params(method: str) -> None:
    service, gids = _service([_record(9, SubscriptionStatus.ACTIVE)])
    gid = gids.virtual_gid(9)

    assert await service.dispatch(method, [gid, "多余"], user_id=USER_ID) == gid


async def test_terminal_removal_tolerates_extra_params() -> None:
    service, gids = _service([_record(1, SubscriptionStatus.COMPLETED)])
    assert (
        await service.dispatch(
            "aria2.removeDownloadResult", [gids.virtual_gid(1), "多余"], user_id=USER_ID
        )
        == "OK"
    )
    assert await service.dispatch("aria2.purgeDownloadResult", ["多余"], user_id=USER_ID) == "OK"
    assert await service.dispatch("aria2.saveSession", ["多余"], user_id=USER_ID) == "OK"


# 规范形态清单：逐条对照 aria2 官方 RPC 章节签名
# （aria2.addUri([secret], uris[, options[, position]]) 等）。
# 原则：规范内入参一律不得报错；未实现/产品明确拒绝的方法不在此表。
_SPEC_SHAPED_CALLS: list[tuple[str, object]] = [
    (
        "aria2.addUri",
        lambda gid, terminal: [
            ["https://example.com/a.bin"],
            {"dir": "/data/downloads", "split": "8", "out": "a.bin"},
            0,
        ],
    ),
    (
        "aria2.addTorrent",
        lambda gid, terminal: ["dG9ycmVudA==", [], {"select-file": "1"}, 0],
    ),
    ("aria2.remove", lambda gid, terminal: [gid]),
    ("aria2.forceRemove", lambda gid, terminal: [gid]),
    ("aria2.tellStatus", lambda gid, terminal: [gid, ["gid", "status"]]),
    ("aria2.tellStatus", lambda gid, terminal: [gid, []]),
    ("aria2.tellActive", lambda gid, terminal: [["gid"]]),
    ("aria2.tellActive", lambda gid, terminal: []),
    ("aria2.tellWaiting", lambda gid, terminal: [0, 10]),
    ("aria2.tellWaiting", lambda gid, terminal: [-1, 2, ["gid"]]),
    ("aria2.tellStopped", lambda gid, terminal: [0, 10, []]),
    ("aria2.changePosition", lambda gid, terminal: [gid, 0, "POS_SET"]),
    ("aria2.changePosition", lambda gid, terminal: [gid, -1, "POS_CUR"]),
    ("aria2.changePosition", lambda gid, terminal: [gid, 2, "POS_END"]),
    ("aria2.getUris", lambda gid, terminal: [gid]),
    ("aria2.getFiles", lambda gid, terminal: [gid]),
    ("aria2.getPeers", lambda gid, terminal: [gid]),
    ("aria2.getServers", lambda gid, terminal: [gid]),
    ("aria2.getOption", lambda gid, terminal: [gid]),
    ("aria2.getGlobalStat", lambda gid, terminal: []),
    ("aria2.getGlobalOption", lambda gid, terminal: []),
    ("aria2.getVersion", lambda gid, terminal: []),
    ("aria2.getSessionInfo", lambda gid, terminal: []),
    ("aria2.saveSession", lambda gid, terminal: []),
    ("aria2.purgeDownloadResult", lambda gid, terminal: []),
    ("aria2.removeDownloadResult", lambda gid, terminal: [terminal]),
    ("system.listMethods", lambda gid, terminal: []),
]


@pytest.mark.parametrize("method,params_of", _SPEC_SHAPED_CALLS)
async def test_spec_shaped_calls_never_error(method: str, params_of) -> None:
    """官方签名里的每一个可选参数位置都照单全收，不得因规范内入参报错。"""
    service, gids = _service(
        [_record(9, SubscriptionStatus.ACTIVE), _record(1, SubscriptionStatus.COMPLETED)]
    )
    service.commands = RecordingCommands()

    await service.dispatch(
        method, params_of(gids.virtual_gid(9), gids.virtual_gid(1)), user_id=USER_ID
    )


async def test_get_option_resolves_raw_gid_too() -> None:
    service, _ = _service([_record(6, SubscriptionStatus.ACTIVE)])
    options = await service.dispatch("aria2.getOption", [RAW_GID], user_id=USER_ID)
    assert options["out"] == "secret-name.bin"
