from __future__ import annotations

import dataclasses

import pytest

from aria2deck.application.downloads.broadcast import task_message
from aria2deck.application.downloads.models import DownloadRecord, ObservedFile, TaskProjection
from aria2deck.application.downloads.projection import TaskProjector
from aria2deck.application.downloads.rpc_projection import (
    VirtualGidSpace,
    aria2_error_code,
    aria2_status,
    basename_of,
    failure_message,
    global_stat_payload,
    is_bittorrent,
    option_payload,
    servers_payload,
    slice_window,
    task_info_hash,
    tell_status_payload,
    uris_payload,
    version_payload,
)
from aria2deck.domain.downloads import AttemptStatus, SubscriptionStatus
from aria2deck.infrastructure.observations import InMemoryDownloadObservations

TASK = TaskProjection(
    id=7,
    status=SubscriptionStatus.ACTIVE,
    status_label="下载中",
    name="movie.mkv",
    display_uri="https://example.com/展示链接",
    total_bytes=1000,
    completed_bytes=400,
    download_speed=128,
    upload_speed=16,
    peer_count=3,
    connection_count=5,
    files=(ObservedFile("folder/movie.mkv", 1000, 400, True),),
    observation_fresh=True,
    reserved_bytes=0,
    error_code=None,
    retryable=False,
    created_at_ms=1,
    updated_at_ms=2,
    finished_at_ms=None,
)


def test_virtual_gid_is_deterministic_and_prefix_isolated() -> None:
    space_a = VirtualGidSpace()
    again = VirtualGidSpace(space_a.prefix)
    assert space_a.virtual_gid(7) == again.virtual_gid(7)
    assert len(space_a.virtual_gid(7)) == 16
    assert space_a.virtual_gid(7) != space_a.virtual_gid(8)
    space_b = VirtualGidSpace()
    assert space_a.virtual_gid(7) != space_b.virtual_gid(7), "重启前缀不同，新旧不碰撞"


@pytest.mark.parametrize(
    "status,expected",
    [
        (SubscriptionStatus.QUEUED, "waiting"),  # aria2 无 queued 枚举，排队即 waiting
        (SubscriptionStatus.ACTIVE, "active"),
        (SubscriptionStatus.WAITING, "waiting"),
        (SubscriptionStatus.PAUSED, "paused"),
        (SubscriptionStatus.COMPLETED, "complete"),
        (SubscriptionStatus.FAILED, "error"),
        (SubscriptionStatus.CANCELLED, "removed"),
        (SubscriptionStatus.EXPIRED, "removed"),
    ],
)
def test_status_mapping_table(status: SubscriptionStatus, expected: str) -> None:
    assert aria2_status(status) == expected


def test_tell_status_payload_stringifies_and_filters_keys() -> None:
    payload = tell_status_payload(TASK, "abcd1234abcd1234")
    assert payload["gid"] == "abcd1234abcd1234"
    assert payload["status"] == "active"
    for key in ("totalLength", "completedLength", "downloadSpeed", "uploadSpeed", "connections"):
        assert isinstance(payload[key], str)
    assert payload["totalLength"] == "1000"
    assert payload["connections"] == "5"
    files = payload["files"]
    assert isinstance(files, list)
    entry = files[0]
    assert isinstance(entry, dict)
    assert entry["path"] == "./movie.mkv", "path = dir + 名字（客户端按 dir 切相对路径）"
    assert entry["length"] == "1000"
    assert entry["selected"] == "true"

    filtered = tell_status_payload(TASK, "g", keys=["gid", "status"])
    assert set(filtered) == {"gid", "status"}


def test_uris_payload_only_exposes_display_uri() -> None:
    payload = uris_payload(TASK)
    assert payload == [{"uri": "https://example.com/展示链接", "status": "used"}]
    import dataclasses

    assert uris_payload(dataclasses.replace(TASK, display_uri=None)) == []


def test_global_stat_and_version_and_option_payloads() -> None:
    counts = {status: 0 for status in SubscriptionStatus}
    counts[SubscriptionStatus.ACTIVE] = 2
    counts[SubscriptionStatus.WAITING] = 3
    counts[SubscriptionStatus.CANCELLED] = 1
    stat = global_stat_payload(counts, 6, 111, 22)
    assert stat == {
        "downloadSpeed": "111",
        "uploadSpeed": "22",
        "numActive": "2",
        "numWaiting": "3",
        "numStopped": "1",
        "numStoppedTotal": "6",
    }
    version = version_payload()
    assert version["version"] and "aria2.rpc.multiCall" in version["enabledFeatures"]
    options = option_payload()
    assert "dir" in options and "secret" not in " ".join(options).lower()


def test_basename_handles_nested_and_windows_separators() -> None:
    assert basename_of("a/b/c.bin") == "c.bin"
    assert basename_of("c.bin") == "c.bin"
    assert basename_of("a\\b\\c.bin") == "c.bin"


def test_slice_window_matches_aria2_man_semantics() -> None:
    """man 原文示例：队列 A,B,C 下 tellWaiting(-1, 2) 返回 [C, B]（倒序）。"""
    items = ["A", "B", "C"]

    assert slice_window(items, 0, 1) == ["A"]
    assert slice_window(items, 1, 2) == ["B", "C"]
    assert slice_window(items, -1, 2) == ["C", "B"]
    assert slice_window(items, -2, 2) == ["B", "A"]
    assert slice_window(items, -2, 5) == ["B", "A"]  # 窗口不越过队首
    assert slice_window(items, -3, 2) == ["A"]
    assert slice_window(items, -99, 2) == []  # 负数越界 → 空
    assert slice_window(items, 99, 5) == []  # 正数越界 → 空
    assert slice_window(items, 0, 0) == []  # num=0 → 空
    assert slice_window(items, -1, 0) == []
    assert slice_window(items, -1, -3) == []  # 非法 num 视作 0
    assert slice_window(items, 0, 99) == items


@pytest.mark.parametrize(
    "code,expected",
    [
        ("upstream_failure", "上游下载失败"),
        ("download_unavailable", "下载不可用"),
        ("rpc_unavailable", "aria2 连接不可用"),
        ("aria2_rejected", "aria2 拒绝了操作"),
        ("task_too_large", "任务超出大小限制"),
        ("quota_exceeded", "存储配额不足"),
        ("physical_space_exceeded", "机器空间不足"),
        ("submission_uncertain", "提交结果不可确认"),
        ("recovery_unsafe", "恢复不安全"),
        ("invalid_input", "输入无效"),
        ("invalid_state", "状态无效"),
        ("content_busy", "内容被占用"),
        ("admission_paused", "系统暂停期间任务未提交"),
        ("metadata_admission_paused", "系统暂停期间元数据未提交"),
        ("external_paused", "任务在 aria2 中被外部暂停"),
        ("submission_timeout", "提交超时，结果无法确认"),
        ("aria2_error", "aria2 报告下载失败"),
        ("aria2_removed", "任务已从 aria2 移除"),
        ("aria2_gid_missing", "aria2 里找不到这个下载"),
        ("ingestion_timeout", "下载完成后入库超时"),
        ("stale_event", "下载事件已过期"),
        # aria2 数字 fault 码并入统一拒绝文案。
        ("aria2_1", "aria2 拒绝了操作"),
        ("aria2_17", "aria2 拒绝了操作"),
        # 未收录的码一律通用文案：内部代号不再原样回显。
        ("custom_extension_code", "下载失败"),
        ("some_unknown_code", "下载失败"),
    ],
)
def test_failure_message_maps_controlled_codes_to_chinese(code: str, expected: str) -> None:
    assert failure_message(code) == expected
    assert failure_message(None) == ""


def test_rpc_and_rest_failure_text_share_one_source() -> None:
    """RPC errorMessage 与 REST error_message 必须同源：同一个码→同一个中文。"""
    for code in (
        "upstream_failure",
        "submission_timeout",
        "aria2_error",
        "metadata_admission_paused",
        "some_unknown_code",
        None,
    ):
        projection = dataclasses.replace(
            TASK, status=SubscriptionStatus.FAILED, error_code=code
        )
        assert task_message(projection).payload["error_message"] == failure_message(code)


def test_failed_task_projects_localized_error_message() -> None:
    """B2：失败任务输出中文失败原因；errorCode 必须是 aria2 数字码，不是内部码。"""
    failed = dataclasses.replace(TASK, status=SubscriptionStatus.FAILED, error_code="upstream_failure")
    payload = tell_status_payload(failed, "g")
    assert payload["status"] == "error"
    assert payload["errorCode"] == "1"
    assert payload["errorMessage"] == "上游下载失败"


def _record(
    *, subscription: SubscriptionStatus, attempt: AttemptStatus, error_code: str | None
) -> DownloadRecord:
    return DownloadRecord(
        id=7,
        attempt_id=1,
        gid="0123456789abcdef",
        subscription_status=subscription,
        attempt_status=attempt,
        name="movie.mkv",
        display_uri="https://example.com/展示链接",
        total_bytes=1000,
        completed_bytes=0,
        reserved_bytes=0,
        error_code=error_code,
        created_at_ms=1,
        updated_at_ms=2,
        finished_at_ms=None,
    )


@pytest.mark.parametrize(
    "subscription,attempt,error_code,status,label,visible_code,message,rpc_status",
    [
        # admission 窗口期：标记是提交管线自己的阶段（等待放行），错误字段必须为空；
        # 窗口期在 aria2 侧的暂停态一律按排队中投影（aria2 只认 waiting）。
        (
            SubscriptionStatus.QUEUED, AttemptStatus.QUEUED, "admission_paused",
            SubscriptionStatus.QUEUED, "排队中", None, "", "waiting",
        ),
        (
            SubscriptionStatus.PAUSED, AttemptStatus.PAUSED, "admission_paused",
            SubscriptionStatus.QUEUED, "排队中", None, "", "waiting",
        ),
        (
            SubscriptionStatus.ACTIVE, AttemptStatus.ACTIVE, "metadata_admission_paused",
            SubscriptionStatus.ACTIVE, "下载中", None, "", "active",
        ),
        (
            SubscriptionStatus.CANCELLED, AttemptStatus.CANCELLED, "metadata_admission_paused",
            SubscriptionStatus.CANCELLED, "已取消", None, "", "removed",
        ),
        # 真失败才把码当失败原因：终态失败任务的标记是历史记录（aria2 error 状态需 errorMessage）。
        (
            SubscriptionStatus.FAILED, AttemptStatus.FAILED, "metadata_admission_paused",
            SubscriptionStatus.FAILED, "已失败", "metadata_admission_paused",
            "系统暂停期间元数据未提交", "error",
        ),
        # 真暂停（aria2 外部暂停）与真实失败不属于管道阶段，照常显示。
        (
            SubscriptionStatus.PAUSED, AttemptStatus.PAUSED, "external_paused",
            SubscriptionStatus.PAUSED, "已暂停", "external_paused",
            "任务在 aria2 中被外部暂停", "paused",
        ),
        (
            SubscriptionStatus.FAILED, AttemptStatus.FAILED, "submission_timeout",
            SubscriptionStatus.FAILED, "已失败", "submission_timeout",
            "提交超时，结果无法确认", "error",
        ),
    ],
)
def test_user_visible_fields_never_carry_pipeline_pause_markers(
    subscription: SubscriptionStatus,
    attempt: AttemptStatus,
    error_code: str,
    status: SubscriptionStatus,
    label: str,
    visible_code: str | None,
    message: str,
    rpc_status: str,
) -> None:
    """REST/WS 与 RPC 共用同一投影：admission 标记不进任何用户可见字段。"""
    projection = TaskProjector(
        observations=InMemoryDownloadObservations(), clock_ms=lambda: 0
    ).project(_record(subscription=subscription, attempt=attempt, error_code=error_code))

    assert projection.status is status
    assert projection.status_label == label
    assert projection.error_code == visible_code

    rest = task_message(projection).payload
    assert rest["status"] == status.value
    assert rest["status_label"] == label
    assert rest["error_code"] == visible_code
    assert rest["error_message"] == message

    rpc = tell_status_payload(projection, "g")
    assert rpc["status"] == rpc_status
    assert rpc["errorCode"] == ("1" if status is SubscriptionStatus.FAILED else "0")
    # aria2 语义：errorMessage 只在 error 状态给失败原因，其余状态恒空串。
    assert rpc["errorMessage"] == (
        message if status is SubscriptionStatus.FAILED else ""
    )


# 权威清单：Aria2App `api/AriaRequests.java::SMALL_KEYS`（源码逐字），
# tellActive/tellWaiting/tellStopped 固定请求这 16 个键，由 DownloadWithUpdate.SmallUpdate 硬解析。
SMALL_UPDATE_KEYS = [
    "totalLength",
    "gid",
    "status",
    "pieceLength",
    "numPieces",
    "dir",
    "bittorrent",
    "completedLength",
    "downloadSpeed",
    "uploadSpeed",
    "connections",
    "uploadLength",
    "numSeeders",
    "files",
    "errorCode",
    "errorMessage",
]
# aria2 man 标注 "BitTorrent only"：客户端以 bittorrent 键存在性判定 isTorrent()。
BT_ONLY_KEYS = ["bittorrent", "infoHash", "numSeeders", "seeder"]
NON_TORRENT_KEYS = [key for key in SMALL_UPDATE_KEYS if key not in BT_ONLY_KEYS]

TORRENT_TASK = dataclasses.replace(
    TASK,
    display_uri="torrent:0123456789abcdef0123456789abcdef01234567",
    name="seed.bin",
)


@pytest.mark.parametrize("key", NON_TORRENT_KEYS)
def test_http_task_provides_all_non_torrent_small_update_keys(key: str) -> None:
    """HTTP 任务：SmallUpdate 的非 BT 硬必填键一个都不能少。"""
    assert key in tell_status_payload(TASK, "g"), f"客户端硬必填键 {key} 缺失"


@pytest.mark.parametrize("key", SMALL_UPDATE_KEYS)
def test_torrent_task_provides_all_small_update_keys(key: str) -> None:
    """种子任务：SMALL_KEYS 全 16 键 + 顶层 infoHash（BigUpdate 硬读）都在。"""
    payload = tell_status_payload(TORRENT_TASK, "g")
    assert key in payload, f"客户端硬必填键 {key} 缺失"
    assert "infoHash" in payload


@pytest.mark.parametrize("key", BT_ONLY_KEYS)
def test_torrent_only_keys_are_absent_for_http_tasks(key: str) -> None:
    """HTTP 任务不得带 BT 专属键：Aria2App 见到 bittorrent 就会当种子任务解析。"""
    assert key not in tell_status_payload(TASK, "g"), f"HTTP 任务不该输出 {key}"


@pytest.mark.parametrize("projection", [TASK, TORRENT_TASK])
@pytest.mark.parametrize("key", ["followedBy", "following", "belongsTo"])
def test_empty_optional_keys_are_omitted(projection: TaskProjection, key: str) -> None:
    """无数据时省略（man 语义）；Aria2App 用 following != null 判定元数据子下载。"""
    assert key not in tell_status_payload(projection, "g")


def test_small_keys_filter_is_satisfied_end_to_end() -> None:
    """客户端实际发的 keys 数组过一遍裁剪后，硬必填键仍然齐全。"""
    http_payload = tell_status_payload(TASK, "g", keys=SMALL_UPDATE_KEYS)
    assert [key for key in NON_TORRENT_KEYS if key not in http_payload] == []

    torrent_payload = tell_status_payload(TORRENT_TASK, "g", keys=[*SMALL_UPDATE_KEYS, "infoHash"])
    assert [key for key in SMALL_UPDATE_KEYS if key not in torrent_payload] == []
    assert torrent_payload["infoHash"] == "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize("status", list(SubscriptionStatus))
def test_projected_status_is_in_client_enum(status: SubscriptionStatus) -> None:
    """Aria2App Download.Status.parse 只认这 6 个字面值，其余抛 ParseException。"""
    payload = tell_status_payload(dataclasses.replace(TASK, status=status), "g")
    assert payload["status"] in {"active", "paused", "waiting", "error", "removed", "complete"}


@pytest.mark.parametrize(
    "status,error_code",
    [
        (SubscriptionStatus.ACTIVE, "metadata_admission_paused"),
        (SubscriptionStatus.FAILED, "upstream_failure"),
        (SubscriptionStatus.FAILED, "aria2_24"),
        (SubscriptionStatus.COMPLETED, None),
    ],
)
def test_error_code_is_always_int_parsable(status: SubscriptionStatus, error_code: str | None) -> None:
    """Aria2App 用 getInt 读 errorCode：任何状态都必须能解析成数字。"""
    payload = tell_status_payload(
        dataclasses.replace(TASK, status=status, error_code=error_code), "g"
    )
    assert isinstance(payload["errorCode"], str)
    assert str(payload["errorCode"]).isdigit(), payload["errorCode"]


def test_files_payload_satisfies_aria_file_hard_fields() -> None:
    """AriaFile 硬字段：index(getInt)/path/length(getLong)/completedLength(getLong)/selected/uris。"""
    payload = tell_status_payload(TASK, "g")
    files = payload["files"]
    assert isinstance(files, list) and files
    entry = files[0]
    assert isinstance(entry, dict)
    assert entry["index"].isdigit()  # getInt 可解析
    assert entry["length"].isdigit() and entry["completedLength"].isdigit()  # getLong 可解析
    assert entry["path"] == "./movie.mkv"
    assert entry["selected"] in {"true", "false"}  # org.json getBoolean 能强转字符串
    assert entry["uris"] == [{"uri": "https://example.com/展示链接", "status": "used"}]


@pytest.mark.parametrize(
    "status",
    [
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.QUEUED,
        SubscriptionStatus.WAITING,
        SubscriptionStatus.PAUSED,
        SubscriptionStatus.COMPLETED,
        SubscriptionStatus.CANCELLED,
        SubscriptionStatus.EXPIRED,
    ],
)
@pytest.mark.parametrize(
    "marker", ["metadata_admission_paused", "admission_paused", "upstream_failure", None]
)
def test_error_code_is_zero_without_failure(
    status: SubscriptionStatus, marker: str | None
) -> None:
    """admission/暂停类内部标记不是失败原因：任何非 FAILED 状态的 errorCode 恒 "0"。"""
    projection = dataclasses.replace(TASK, status=status, error_code=marker)
    assert tell_status_payload(projection, "g")["errorCode"] == "0"


@pytest.mark.parametrize(
    "code,expected",
    [
        ("upstream_failure", "1"),
        ("download_unavailable", "3"),
        ("quota_exceeded", "9"),
        ("physical_space_exceeded", "9"),
        ("aria2_24", "24"),
        ("aria2_error", "1"),
        ("admission_paused", "1"),
        ("metadata_admission_paused", "1"),
        ("custom_extension_code", "1"),
    ],
)
def test_failed_error_code_is_numeric_aria2_code(code: str, expected: str) -> None:
    projection = dataclasses.replace(TASK, status=SubscriptionStatus.FAILED, error_code=code)
    payload = tell_status_payload(projection, "g")
    assert payload["errorCode"] == expected
    assert str(payload["errorCode"]).isdigit()
    assert payload["errorMessage"], "失败任务必须有中文失败原因"
    assert "admission" not in str(payload["errorCode"])


@pytest.mark.parametrize(
    "uri,expected",
    [
        (
            "torrent:0123456789abcdef0123456789abcdef01234567",
            "0123456789abcdef0123456789abcdef01234567",
        ),
        (
            "magnet:?xt=urn:btih:0123456789ABCDEF0123456789ABCDEF01234567&dn=x",
            "0123456789abcdef0123456789abcdef01234567",
        ),
        ("magnet:?dn=x&xt=urn:btih:" + "0" * 40, "0" * 40),
        ("magnet:?xt=urn:btih:" + "A" * 32, ""),  # base32 btih 不解析
        ("magnet:?xt=urn:btmh:1220abcd&dn=x", ""),  # 非 btih 的 xt 一律空串
        ("magnet:?dn=no-xt", ""),
        ("https://example.com/a.bin", ""),
        (None, ""),
    ],
)
def test_task_info_hash_derivation(uri: str | None, expected: str) -> None:
    projection = dataclasses.replace(TASK, display_uri=uri)
    assert task_info_hash(projection) == expected
    payload = tell_status_payload(projection, "g")
    if not is_bittorrent(projection):
        # 非 BT：infoHash / bittorrent 均不出现（aria2 man："BitTorrent only"）。
        assert "infoHash" not in payload
        assert "bittorrent" not in payload
        return
    assert payload["infoHash"] == expected
    assert payload["bittorrent"] == {
        "infoHash": expected,
        "announceList": [],
        "comment": "",
        "creationDate": 0,
        "mode": "single",
        "metadata": {},
    }


MULTI_FILE_TASK = dataclasses.replace(
    TASK,
    files=(
        ObservedFile("a/one.bin", 10, 10, True),
        ObservedFile("a/two.bin", 5, 0, False),
        ObservedFile("b/three.bin", 7, 7, True),
    ),
)


@pytest.mark.parametrize(
    "projection",
    [TASK, MULTI_FILE_TASK, dataclasses.replace(TASK, files=())],
    ids=["single", "multi", "synthesized"],
)
def test_servers_indexes_match_files(projection: TaskProjection) -> None:
    """客户端不变量：getServers 的 index 集合必须与 getFiles 完全一致。"""
    files = tell_status_payload(projection, "g")["files"]
    assert [entry["index"] for entry in servers_payload(projection)] == [
        entry["index"] for entry in files
    ]


@pytest.mark.parametrize(
    "projection",
    [TASK, MULTI_FILE_TASK, dataclasses.replace(TASK, files=())],
    ids=["single", "multi", "synthesized"],
)
def test_servers_entry_shape(projection: TaskProjection) -> None:
    for entry in servers_payload(projection):
        assert set(entry) == {"index", "servers"}
        assert entry["index"] == str(entry["index"]) and str(entry["index"]).isdigit()
        assert entry["servers"] == []


@pytest.mark.parametrize(
    "projection",
    [TASK, MULTI_FILE_TASK, dataclasses.replace(TASK, files=())],
    ids=["single", "multi", "synthesized"],
)
def test_client_sparse_lookup_never_null(projection: TaskProjection) -> None:
    """模拟 ServersAdapter.notifyItemsChanged: servers.get(file.index) 缺键返回 null → NPE。"""
    sparse = {int(entry["index"]): entry["servers"] for entry in servers_payload(projection)}
    for file_entry in tell_status_payload(projection, "g")["files"]:
        assert sparse.get(int(file_entry["index"])) is not None


@pytest.mark.parametrize(
    "projection",
    [
        TASK,
        dataclasses.replace(TASK, files=()),
        # 1 字符名字：纯 basename 时客户端 substring(dir.length()+1) 会越界崩溃。
        dataclasses.replace(TASK, files=(), name="a"),
        dataclasses.replace(
            TASK,
            files=(
                ObservedFile("x/a", 1, 0, True),
                ObservedFile("x/b.bin", 2, 0, True),
            ),
        ),
    ],
)
def test_file_paths_survive_client_directory_slicing(projection: TaskProjection) -> None:
    """Aria2App AriaDirectory.createRoot 对每项做 path.substring(dir.length()+1)：
    path 必须始终是 dir + "/" + 名字，且切片结果非空、不含分隔符。"""
    payload = tell_status_payload(projection, "g")
    prefix = f"{payload['dir']}/"
    for entry in payload["files"]:
        path = str(entry["path"])
        assert path.startswith(prefix), "files[].path 必须带 dir 前缀"
        relative = path[len(prefix) :]  # 客户端同款切片
        assert relative != "" and "/" not in relative


def test_fileless_task_synthesizes_name_for_client_fallback() -> None:
    """失败的无文件任务：客户端按 files[0].path 取名字，空数组会退化成 "???"。"""
    name = "Big Buck Bunny"
    projection = dataclasses.replace(
        TASK,
        status=SubscriptionStatus.FAILED,
        display_uri="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Big+Buck+Bunny",
        name=name,
        total_bytes=None,
        completed_bytes=0,
        files=(),
        error_code="upstream_failure",
    )

    files = tell_status_payload(projection, "g")["files"]
    assert len(files) == 1
    entry = files[0]
    assert entry["path"] == f"./{name}", "path = dir + 显示名"
    assert entry["path"].strip() != "", "客户端名字推导不再落到 ???"
    assert entry["length"] == "0" and entry["completedLength"] == "0"
    assert entry["selected"] == "true"
    assert entry["uris"][0]["status"] == "used"


def test_fileless_http_task_uses_display_name_basename() -> None:
    """HTTP 失败任务同理：path 就是 REST 那份名字（领域层已从 URI basename 派生）。"""
    projection = dataclasses.replace(
        TASK, status=SubscriptionStatus.FAILED, files=(), name="movie.mkv", error_code="aria2_1"
    )
    files = tell_status_payload(projection, "g")["files"]
    assert [entry["path"] for entry in files] == ["./movie.mkv"]


def test_only_first_file_carries_display_uri() -> None:
    projection = dataclasses.replace(
        TASK,
        files=(
            ObservedFile("a/one.bin", 10, 10, True),
            ObservedFile("a/two.bin", 5, 0, False),
        ),
    )
    files = tell_status_payload(projection, "g")["files"]
    assert [entry["uris"] for entry in files] == [
        [{"uri": "https://example.com/展示链接", "status": "used"}],
        [],
    ]
    assert [entry["path"] for entry in files] == ["./one.bin", "./two.bin"]


def test_file_uris_empty_without_display_uri() -> None:
    projection = dataclasses.replace(TASK, display_uri=None)
    assert tell_status_payload(projection, "g")["files"][0]["uris"] == []
    fileless = dataclasses.replace(TASK, display_uri=None, files=())
    assert tell_status_payload(fileless, "g")["files"][0]["uris"] == []


def test_magnet_metadata_task_snapshot() -> None:
    """抓包现场复现：active 的 magnet 元数据任务（带 admission 标记）完整投影。"""
    info_hash = "0123456789abcdef0123456789abcdef01234567"
    projection = TaskProjection(
        id=11,
        status=SubscriptionStatus.ACTIVE,
        status_label="获取元数据中",
        name="磁力任务",
        display_uri=f"magnet:?xt=urn:btih:{info_hash}&dn=movie",
        total_bytes=None,
        completed_bytes=0,
        download_speed=0,
        upload_speed=0,
        peer_count=0,
        connection_count=0,
        files=(),
        observation_fresh=True,
        reserved_bytes=0,
        error_code="metadata_admission_paused",
        retryable=False,
        created_at_ms=1,
        updated_at_ms=2,
        finished_at_ms=None,
    )

    assert tell_status_payload(projection, "gid0123456789ab") == {
        "gid": "gid0123456789ab",
        "status": "active",
        "totalLength": "0",
        "completedLength": "0",
        "uploadLength": "0",
        "uploadSpeed": "0",
        "downloadSpeed": "0",
        "numPieces": "1",
        "pieceLength": "0",
        "connections": "0",
        "errorCode": "0",
        "errorMessage": "",
        "dir": ".",
        # 无文件时合成一项，让客户端 files[0].path 推导出与网页端一致的名字。
        "files": [
            {
                "index": "1",
                "path": "./磁力任务",
                "length": "0",
                "completedLength": "0",
                "selected": "true",
                "uris": [{"uri": f"magnet:?xt=urn:btih:{info_hash}&dn=movie", "status": "used"}],
            }
        ],
        "infoHash": info_hash,
        "verifiedLength": "0",
        "verifyIntegrityPending": "false",
        "bittorrent": {
            "infoHash": info_hash,
            "announceList": [],
            "comment": "",
            "creationDate": 0,
            "mode": "single",
            "metadata": {},
        },
        "numSeeders": "0",
        "seeder": "false",
    }
    assert aria2_error_code(projection) == "0"


@pytest.mark.parametrize(
    "status",
    [
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.COMPLETED,
        SubscriptionStatus.CANCELLED,
        SubscriptionStatus.EXPIRED,
    ],
)
def test_non_failed_statuses_keep_error_message_empty(status: SubscriptionStatus) -> None:
    """旧版同语义：errorMessage 仅 error 状态输出，其余状态（含残留 error_code）恒空串。"""
    import dataclasses

    payload = tell_status_payload(
        dataclasses.replace(TASK, status=status, error_code="upstream_failure"), "g"
    )
    assert payload["errorMessage"] == ""
    assert payload["errorCode"] == "0", "无错误恒 \"0\"，内部码不得外泄"


def test_unsafe_error_code_text_never_leaks() -> None:
    """负断言：路径、URI、异常原文等非受控形态一律脱敏为通用文案，原文零出现。"""
    import dataclasses

    unsafe_values = (
        "/data/downloads/secret.iso",
        "https://internal.example/fetch/abc",
        "ValueError: boom at app/service.py:42",
        "a" * 200,
        "UPPER-CASE Leak",
    )
    for unsafe in unsafe_values:
        projection = dataclasses.replace(
            TASK, status=SubscriptionStatus.FAILED, error_code=unsafe
        )
        message = tell_status_payload(projection, "g")["errorMessage"]
        assert message == "下载失败"
        assert unsafe not in message
