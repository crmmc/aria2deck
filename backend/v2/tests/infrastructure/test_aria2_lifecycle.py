from __future__ import annotations

import logging
from pathlib import Path

import aiohttp
import pytest

from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.settings import SystemSettings
from aria2deck.infrastructure.aria2_submission import Aria2SubmissionClient, _RpcRejected
from aria2deck.infrastructure.security import CredentialManager

GID = "0123456789abcdef"
# 清单用的相对路径靠 `{下载根}/{attempt_id}/` 前缀裁剪出来，测试里的文件项都挂在这个目录下。
ATTEMPT_ID = 5


def client(tmp_path: Path, *, url: str = "https://aria2.example/jsonrpc") -> Aria2SubmissionClient:
    return Aria2SubmissionClient(
        settings=lambda: SystemSettings(aria2_rpc_url=url),
        credentials=CredentialManager(pepper=None, debug=True),
        internal_base_url="http://app:8001",
        download_directory=tmp_path,
        clock_ms=lambda: 100,
    )


def file_entry(
    root: Path,
    relpath: str,
    *,
    length: str = "10",
    completed: str = "4",
    selected: str = "true",
) -> dict[str, str]:
    """aria2 的文件项：path 是平台设置的 `dir + "/" + 名字`，dir = `{root}/{attempt_id}`。"""
    return {
        "path": f"{root}/{ATTEMPT_ID}/{relpath}",
        "length": length,
        "completedLength": completed,
        "selected": selected,
    }


def snapshot(files: list[dict[str, str]] | None = None) -> dict[str, object]:
    return {
        "gid": GID,
        "status": "active",
        "totalLength": "10",
        "completedLength": "4",
        "downloadSpeed": "8",
        "uploadSpeed": 2,
        "numPeers": "3",
        "connections": "1",
        "files": [] if files is None else files,
        "followedBy": ["fedcba0987654321"],
        "following": [],
    }


async def test_snapshot_parses_safe_files_numbers_and_handoff_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = client(tmp_path)

    async def call(*args: object, **kwargs: object) -> object:
        return snapshot([file_entry(tmp_path, "file.bin")])

    monkeypatch.setattr(adapter, "_call", call)
    observed = await adapter.snapshot(gid=GID, attempt_id=ATTEMPT_ID)
    assert observed is not None
    assert observed.observed_at_ms == 100
    assert observed.total_length == 10
    assert observed.completed_length == 4
    assert observed.download_speed == 8
    assert observed.upload_speed == 2
    assert observed.peer_count == 3
    assert observed.connection_count == 1
    assert observed.files[0].name == "file.bin"
    assert observed.files[0].relpath == "file.bin"
    assert observed.files[0].selected is True
    assert observed.followed_by == ("fedcba0987654321",)


def malformed_snapshots() -> list[object]:
    cases: list[object] = [None, {**snapshot(), "gid": "wrong"}, {**snapshot(), "status": "unknown"}]
    for value in (True, "bad", -1):
        cases.append({**snapshot(), "totalLength": value})
    cases.extend(
        [
            {**snapshot(), "files": {}},
            {**snapshot(), "files": [None]},
            {**snapshot(), "files": [{"path": 1}]},
            {**snapshot(), "files": [{"path": "a", "selected": "yes"}]},
            {**snapshot(), "followedBy": "bad"},
            {**snapshot(), "following": [1]},
        ]
    )
    return cases


@pytest.mark.parametrize("result", malformed_snapshots())
async def test_snapshot_rejects_malformed_or_inconsistent_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: object
) -> None:
    adapter = client(tmp_path)

    async def call(*args: object, **kwargs: object) -> object:
        return result

    monkeypatch.setattr(adapter, "_call", call)
    with pytest.raises(DomainError) as caught:
        await adapter.snapshot(gid=GID, attempt_id=ATTEMPT_ID)
    assert caught.value.code is ErrorCode.RPC_UNAVAILABLE


async def test_snapshot_accepts_single_following_gid_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = client(tmp_path)
    parent_gid = "fedcba0987654321"

    async def call(*args: object, **kwargs: object) -> object:
        return {**snapshot(), "following": parent_gid}

    monkeypatch.setattr(adapter, "_call", call)
    observed = await adapter.snapshot(gid=GID, attempt_id=ATTEMPT_ID)
    assert observed is not None
    assert observed.following == (parent_gid,)


async def test_snapshot_accepts_empty_following_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = client(tmp_path)

    async def call(*args: object, **kwargs: object) -> object:
        return {**snapshot(), "following": ""}

    monkeypatch.setattr(adapter, "_call", call)
    observed = await adapter.snapshot(gid=GID, attempt_id=ATTEMPT_ID)
    assert observed is not None
    assert observed.following == ()


async def test_missing_snapshot_and_result_are_idempotent_but_other_rejections_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = client(tmp_path)

    async def missing(*args: object, **kwargs: object) -> object:
        raise _RpcRejected(1)

    monkeypatch.setattr(adapter, "_call", missing)
    assert await adapter.snapshot(gid=GID, attempt_id=ATTEMPT_ID) is None
    await adapter.remove_result(gid=GID)

    async def rejected(*args: object, **kwargs: object) -> object:
        raise _RpcRejected(2)

    monkeypatch.setattr(adapter, "_call", rejected)
    with pytest.raises(DomainError) as caught:
        await adapter.snapshot(gid=GID, attempt_id=ATTEMPT_ID)
    assert caught.value.code is ErrorCode.RPC_UNAVAILABLE

    async def transport(*args: object, **kwargs: object) -> object:
        raise DomainError(ErrorCode.SUBMISSION_UNCERTAIN, "offline")

    monkeypatch.setattr(adapter, "_call", transport)
    with pytest.raises(DomainError) as caught:
        await adapter.unpause(gid=GID)
    assert caught.value.code is ErrorCode.RPC_UNAVAILABLE


async def test_unpause_and_remove_result_validate_response_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = client(tmp_path)
    results = iter([GID, "OK"])

    async def success(*args: object, **kwargs: object) -> object:
        return next(results)

    monkeypatch.setattr(adapter, "_call", success)
    await adapter.unpause(gid=GID)
    await adapter.remove_result(gid=GID)

    async def wrong(*args: object, **kwargs: object) -> object:
        return "wrong"

    monkeypatch.setattr(adapter, "_call", wrong)
    with pytest.raises(DomainError):
        await adapter.unpause(gid=GID)
    with pytest.raises(DomainError):
        await adapter.remove_result(gid=GID)


async def test_live_gid_enumeration_combines_active_waiting_and_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = client(tmp_path)
    calls: list[tuple[str, list[object]]] = []
    waiting_pages = 0

    async def call(
        settings: object, method: str, params: list[object], **kwargs: object
    ) -> object:
        calls.append((method, params))
        if method == "aria2.tellActive":
            return [{"gid": GID, "status": "active"}]
        nonlocal waiting_pages
        waiting_pages += 1
        if waiting_pages == 1:
            return [{"gid": GID, "status": "waiting"}] * 1000
        return [{"gid": "fedcba0987654321", "status": "paused"}]

    monkeypatch.setattr(adapter, "_call", call)
    assert await adapter.list_live_gids() == [GID, "fedcba0987654321"]
    assert calls[1:] == [("aria2.tellWaiting", [0, 1000]), ("aria2.tellWaiting", [1000, 1000])]


@pytest.mark.parametrize(
    "method,bad",
    [
        ("aria2.tellActive", {}),
        ("aria2.tellActive", [None]),
        # 是 dict 但没有可用的 gid：枚举只认 gid，缺了就是 aria2 不可用。
        ("aria2.tellActive", [{"status": "active"}]),
        ("aria2.tellWaiting", [{"gid": 5}]),
        # 列表里混进非 dict：不能静默少认一个 GID。
        ("aria2.tellWaiting", [{"gid": GID}, None]),
        ("aria2.tellWaiting", {}),
    ],
)
async def test_live_gid_enumeration_rejects_malformed_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, bad: object
) -> None:
    adapter = client(tmp_path)

    async def call(
        settings: object, called: str, params: list[object], **kwargs: object
    ) -> object:
        if called == method:
            return bad
        return []

    monkeypatch.setattr(adapter, "_call", call)
    with pytest.raises(DomainError):
        await adapter.list_live_gids()


class Message:
    def __init__(self, kind: aiohttp.WSMsgType, data: str = "") -> None:
        self.type = kind
        self.data = data


class Socket:
    def __init__(self, messages: list[Message]) -> None:
        self.messages = messages

    async def __aenter__(self) -> Socket:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def __aiter__(self) -> Socket:
        return self

    async def __anext__(self) -> Message:
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)


class Session:
    def __init__(self, socket: Socket | Exception, urls: list[str]) -> None:
        self.socket = socket
        self.urls = urls

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def ws_connect(self, url: str, **kwargs: object) -> Socket:
        self.urls.append(url)
        if isinstance(self.socket, Exception):
            raise self.socket
        return self.socket


async def test_websocket_listener_filters_messages_and_forwards_only_gids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages = [
        Message(aiohttp.WSMsgType.TEXT, "not-json"),
        Message(aiohttp.WSMsgType.TEXT, "{}"),
        Message(aiohttp.WSMsgType.TEXT, '{"method":"aria2.onDownloadStart"}'),
        Message(aiohttp.WSMsgType.TEXT, '{"method":"aria2.onDownloadStart","params":[null]}'),
        Message(aiohttp.WSMsgType.BINARY, "ignored"),
        Message(aiohttp.WSMsgType.TEXT, '{"method":"aria2.onDownloadStart","params":[{"gid":"a"}]}'),
        Message(aiohttp.WSMsgType.TEXT, '{"method":"aria2.onBtDownloadComplete","params":[{"gid":"b"}]}'),
    ]
    urls: list[str] = []
    session = Session(Socket(messages), urls)
    monkeypatch.setattr(
        "aria2deck.infrastructure.aria2_submission.aiohttp.ClientSession", lambda: session
    )
    received: list[str] = []

    async def on_gid(gid: str) -> None:
        received.append(gid)

    await client(tmp_path).listen(on_gid)
    assert received == ["a", "b"]
    assert urls == ["wss://aria2.example/jsonrpc"]


async def test_websocket_listener_maps_error_frames_and_network_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls: list[str] = []
    session = Session(Socket([Message(aiohttp.WSMsgType.ERROR)]), urls)
    monkeypatch.setattr(
        "aria2deck.infrastructure.aria2_submission.aiohttp.ClientSession", lambda: session
    )
    with pytest.raises(DomainError) as caught:
        await client(tmp_path).listen(lambda gid: asyncio_sleep(gid))
    assert caught.value.code is ErrorCode.RPC_UNAVAILABLE

    session = Session(aiohttp.ClientError("offline"), urls)
    monkeypatch.setattr(
        "aria2deck.infrastructure.aria2_submission.aiohttp.ClientSession", lambda: session
    )
    with pytest.raises(DomainError) as caught:
        await client(tmp_path, url="http://aria2.example/jsonrpc").listen(lambda gid: asyncio_sleep(gid))
    assert caught.value.code is ErrorCode.RPC_UNAVAILABLE


async def asyncio_sleep(value: str) -> None:
    return None


async def test_snapshot_prefers_bittorrent_info_name_over_file_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """种子真名来自 bittorrent.info.name（aria2 man：magnet 元数据任务没有该键）。"""
    adapter = client(tmp_path)
    payload = {
        # 单文件也有 basename 可作名字，但种子真名优先。
        **snapshot([file_entry(tmp_path, "file.bin")]),
        "bittorrent": {"info": {"name": "Big Buck Bunny"}, "mode": "single"},
    }

    async def call(*args: object, **kwargs: object) -> object:
        return payload

    monkeypatch.setattr(adapter, "_call", call)
    observed = await adapter.snapshot(gid=GID, attempt_id=ATTEMPT_ID)
    assert observed is not None
    assert observed.name == "Big Buck Bunny"


async def test_snapshot_falls_back_to_single_file_basename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = client(tmp_path)

    async def call(*args: object, **kwargs: object) -> object:
        return snapshot([file_entry(tmp_path, "file.bin")])

    monkeypatch.setattr(adapter, "_call", call)
    observed = await adapter.snapshot(gid=GID, attempt_id=ATTEMPT_ID)
    assert observed is not None
    assert observed.name == "file.bin"


@pytest.mark.parametrize(
    "relpaths",
    [
        # 元数据占位文件不是真名。
        ["[METADATA]aabb"],
        # 多文件时 files[0] 只是内层文件名，不是任务名。
        ["pkg/a.bin", "pkg/b.bin"],
        # 无文件（元数据未到手）时没有名字。
        [],
    ],
)
async def test_snapshot_reports_no_name_without_a_real_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relpaths: list[str]
) -> None:
    adapter = client(tmp_path)
    payload = snapshot([
        file_entry(tmp_path, relpath, length="1", completed="0") for relpath in relpaths
    ])

    async def call(*args: object, **kwargs: object) -> object:
        return payload

    monkeypatch.setattr(adapter, "_call", call)
    observed = await adapter.snapshot(gid=GID, attempt_id=ATTEMPT_ID)
    assert observed is not None
    assert observed.name is None


@pytest.mark.parametrize(
    "bittorrent",
    # 形态异常的 bittorrent 不影响整份观测：只当作没有 bittorrent 名，回落单文件名。
    ["bad", {"info": "bad"}, {"info": {"name": 7}}, {"info": {"name": ""}}],
)
async def test_snapshot_ignores_malformed_bittorrent_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bittorrent: object
) -> None:
    adapter = client(tmp_path)
    payload = {
        **snapshot([file_entry(tmp_path, "a.bin", length="1", completed="0")]),
        "bittorrent": bittorrent,
    }

    async def call(*args: object, **kwargs: object) -> object:
        return payload

    monkeypatch.setattr(adapter, "_call", call)
    observed = await adapter.snapshot(gid=GID, attempt_id=ATTEMPT_ID)
    assert observed is not None
    assert observed.name == "a.bin"


async def test_snapshot_keeps_attempt_relative_paths_for_bt_subdirectories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BT 多文件磁盘布局是 `{attempt}/{种子名}/子目录/文件`，清单必须拿到整段相对路径。"""
    adapter = client(tmp_path)
    payload = snapshot(
        [
            file_entry(tmp_path, "bundle/sub/deep.bin", length="5", completed="5"),
            file_entry(tmp_path, "bundle/root.bin", length="3", completed="3"),
        ]
    )

    async def call(*args: object, **kwargs: object) -> object:
        return payload

    monkeypatch.setattr(adapter, "_call", call)
    observed = await adapter.snapshot(gid=GID, attempt_id=ATTEMPT_ID)
    assert observed is not None
    assert [(item.name, item.relpath) for item in observed.files] == [
        ("deep.bin", "bundle/sub/deep.bin"),
        ("root.bin", "bundle/root.bin"),
    ]


async def test_snapshot_normalizes_windows_separators_in_reported_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = client(tmp_path)
    payload = snapshot(
        [
            {
                "path": f"{tmp_path}\\{ATTEMPT_ID}\\bundle\\a.bin",
                "length": "1",
                "completedLength": "1",
                "selected": "true",
            }
        ]
    )

    async def call(*args: object, **kwargs: object) -> object:
        return payload

    monkeypatch.setattr(adapter, "_call", call)
    observed = await adapter.snapshot(gid=GID, attempt_id=ATTEMPT_ID)
    assert observed is not None
    assert observed.files[0].relpath == "bundle/a.bin"


@pytest.mark.parametrize(
    "path",
    [
        # 别的 attempt 目录（任务串了）。
        "{root}/6/a.bin",
        # 前缀只共享字符串前缀，不是同一目录。
        "{root}/50/a.bin",
        # 不在下载根下的绝对路径。
        "/elsewhere/5/a.bin",
        # 相对路径：aria2 没有按我们设的 dir 回报。
        "5/a.bin",
        # 正好就是尝试目录本身，没有文件名。
        "{root}/5/",
    ],
)
async def test_snapshot_drops_files_whose_path_is_not_under_the_attempt_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    path: str,
) -> None:
    """dir 由平台设置，路径不在本次尝试目录下只能视为不可信：不猜、不转换，整条剔除。"""
    adapter = client(tmp_path)
    payload = snapshot(
        [
            {"path": path.format(root=tmp_path), "length": "1", "completedLength": "1"},
            # 可信的那一条必须留下：剔除只针对坏条目。
            file_entry(tmp_path, "keep.bin", length="2", completed="2"),
        ]
    )

    async def call(*args: object, **kwargs: object) -> object:
        return payload

    monkeypatch.setattr(adapter, "_call", call)
    with caplog.at_level(
        logging.WARNING, logger="aria2deck.infrastructure.aria2_submission"
    ):
        observed = await adapter.snapshot(gid=GID, attempt_id=ATTEMPT_ID)

    assert observed is not None
    assert [item.relpath for item in observed.files] == ["keep.bin"]
    assert any("不在任务目录下" in record.getMessage() for record in caplog.records)


async def test_snapshot_keeps_unselected_files_for_display(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """子集下载的未选中项没落盘，但显示侧要能看到，剔除交给清单落库那一层。"""
    adapter = client(tmp_path)
    payload = snapshot(
        [
            file_entry(tmp_path, "picked.bin", length="2", completed="2"),
            file_entry(
                tmp_path, "skipped.bin", length="1", completed="0", selected="false"
            ),
        ]
    )

    async def call(*args: object, **kwargs: object) -> object:
        return payload

    monkeypatch.setattr(adapter, "_call", call)
    observed = await adapter.snapshot(gid=GID, attempt_id=ATTEMPT_ID)
    assert observed is not None
    assert [(item.relpath, item.selected) for item in observed.files] == [
        ("picked.bin", True),
        ("skipped.bin", False),
    ]
