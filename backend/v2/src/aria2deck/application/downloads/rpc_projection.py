from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlsplit

from ...domain.downloads import SubscriptionStatus
from .models import DownloadRecord, TaskProjection

GID_LENGTH = 16

# RPC-15/16 共用的唯一状态映射表：订阅状态 → aria2 原生 status 字符串。
_ARIA2_STATUS: dict[SubscriptionStatus, str] = {
    # aria2 man 的 status 枚举只有 active/waiting/paused/error/complete/removed；
    # 排队中的任务在 aria2 里就是 waiting，不得另造 "queued"。
    SubscriptionStatus.QUEUED: "waiting",
    SubscriptionStatus.ACTIVE: "active",
    SubscriptionStatus.WAITING: "waiting",
    SubscriptionStatus.PAUSED: "paused",
    SubscriptionStatus.COMPLETED: "complete",
    SubscriptionStatus.FAILED: "error",
    SubscriptionStatus.CANCELLED: "removed",  # 文件删除取消 → removed（历史版本 326aeff 语义重放）
    SubscriptionStatus.EXPIRED: "removed",
}

# error_code → 脱敏中文失败原因（B2）：与前端 HistoryPage 的 ERROR_CODE_LABEL
# 同口径；输入是受控 snake_case 码（DomainError 值 / aria2_{数字} / 暂停码），
# 绝不落入路径、URI 或异常原文。
_ERROR_MESSAGES: dict[str, str] = {
    "upstream_failure": "上游下载失败",
    "download_unavailable": "下载不可用",
    "rpc_unavailable": "aria2 连接不可用",
    "aria2_rejected": "aria2 拒绝了操作",
    "task_too_large": "任务超出大小限制",
    "quota_exceeded": "存储配额不足",
    "physical_space_exceeded": "机器空间不足",
    "submission_uncertain": "提交结果不可确认",
    "recovery_unsafe": "恢复不安全",
    "invalid_input": "输入无效",
    "invalid_state": "状态无效",
    "content_busy": "内容被占用",
    "admission_paused": "系统暂停期间任务未提交",
    "metadata_admission_paused": "系统暂停期间元数据未提交",
}

_SAFE_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")

# 内部错误码 → aria2 EXIT STATUS 数字码。语义不能确定的一律归 1（未知错误）。
# admission/暂停类内部标记（admission_paused、metadata_admission_paused、external_paused）
# 不是下载失败原因，永不进入映射表，也就不可能出现在响应的 errorCode 里。
_ARIA2_ERROR_CODES: dict[str, str] = {
    "download_unavailable": "3",  # 3 = 资源未找到
    "quota_exceeded": "9",  # 9 = 磁盘空间不足（配额等价语义）
    "physical_space_exceeded": "9",
}
_ARIA2_NATIVE_ERROR_CODE = re.compile(r"aria2_(\d{1,3})")

# 虚拟下载目录：不暴露服务器真实路径（内容寻址存储的绝对路径）。
# 客户端（Aria2App AriaDirectory.createRoot）按 `path.substring(dir.length() + 1)` 切相对路径，
# 因此 files[].path 必须始终是 `dir + "/" + 名字`，纯 basename 会切错甚至越界崩溃。
_RPC_DIR = "."

_TORRENT_URI_PREFIX = "torrent:"
_MAGNET_BTIH = "urn:btih:"
_HEX_INFO_HASH = re.compile(r"[0-9a-fA-F]{40}")


def failure_message(error_code: str | None) -> str:
    """errorCode → 中文文案；aria2 数字 fault 并入拒绝语义；非安全形态一律脱敏。"""
    if error_code is None:
        return ""
    if error_code in _ERROR_MESSAGES:
        return _ERROR_MESSAGES[error_code]
    if error_code.startswith("aria2_"):
        return _ERROR_MESSAGES["aria2_rejected"]
    if _SAFE_ERROR_CODE.fullmatch(error_code):
        return error_code
    return "下载失败"

RPC_VERSION = "1.37.0"
RPC_FEATURES = ["aria2.rpc.multiCall", "aria2.rpc.listMethods", "aria2.rpc.secretToken"]

# getOption/getGlobalOption 兼容投影：只暴露安全字段，无内部路径/Secret/capability。
_OPTION_PROJECTION = {
    "dir": ".",
    "max-concurrent-downloads": "5",
    "max-connection-per-server": "16",
    "split": "16",
    "min-split-size": "20M",
    "continue": "true",
    "max-tries": "5",
    "retry-wait": "5",
    "timeout": "60",
    "connect-timeout": "60",
    "auto-file-renaming": "false",
    "allow-overwrite": "false",
}


def aria2_status(status: SubscriptionStatus) -> str:
    return _ARIA2_STATUS[status]


def is_bittorrent(projection: TaskProjection) -> bool:
    """种子/磁力任务判定：展示 URI 带协议前缀即 BT（客户端也用 bittorrent 键存在性判定）。"""
    uri = projection.display_uri or ""
    return uri.startswith(_TORRENT_URI_PREFIX) or uri.startswith("magnet:")


def aria2_error_code(projection: TaskProjection) -> str:
    """aria2 的 errorCode 是无错恒 `"0"` 的数字字符串，内部错误码不得外泄。"""
    if projection.status is not SubscriptionStatus.FAILED or not projection.error_code:
        return "0"
    mapped = _ARIA2_ERROR_CODES.get(projection.error_code)
    if mapped is not None:
        return mapped
    native = _ARIA2_NATIVE_ERROR_CODE.fullmatch(projection.error_code)
    if native is not None:
        return native.group(1)
    return "1"


def task_info_hash(projection: TaskProjection) -> str:
    """BT 任务的 info hash：种子取展示 URI（`torrent:<hash>`），Magnet 取 btih。

    领域层已把 incoming 的 base32 btih 归一成 40 位十六进制，因此这里的十六进制校验只是兜底；
    v2 multihash（urn:btmh）不解析。非 BT 任务恒空串。
    """
    uri = projection.display_uri or ""
    if uri.startswith(_TORRENT_URI_PREFIX):
        return uri[len(_TORRENT_URI_PREFIX) :]
    if not uri.startswith("magnet:"):
        return ""
    for value in parse_qs(urlsplit(uri).query).get("xt", []):
        if value.startswith(_MAGNET_BTIH):
            candidate = value[len(_MAGNET_BTIH) :]
            return candidate.lower() if _HEX_INFO_HASH.fullmatch(candidate) else ""
    return ""


class VirtualGidSpace:
    """Deterministic per-process virtual GIDs derived from subscription ids.

    16 字节随机混淆前缀 + 订阅 ID 的混淆哈希 → 16 hex：无状态可重建，
    重启后前缀不同 → 新旧虚拟 GID 不碰撞；外部永远只见虚拟 GID。
    """

    def __init__(self, prefix: bytes | None = None) -> None:
        self.prefix = prefix if prefix is not None else secrets.token_bytes(16)

    def virtual_gid(self, subscription_id: int) -> str:
        digest = hashlib.sha256(self.prefix + str(subscription_id).encode()).hexdigest()
        return digest[:GID_LENGTH]


def basename_of(path_or_name: str) -> str:
    return PurePosixPath(path_or_name.replace("\\", "/")).name or path_or_name


def display_uris(projection: TaskProjection) -> list[dict[str, str]]:
    """getUris 与文件项 uris 的唯一来源：只出展示 URI，内部签名 URI 绝不外泄。"""
    uri = projection.display_uri
    return [] if uri is None else [{"uri": uri, "status": "used"}]


def _file_path(name: str) -> str:
    """`files[].path` 保持 aria2 的 `dir + "/" + 名字` 形态（客户端按 dir 长度切相对路径）。"""
    return f"{_RPC_DIR}/{name}"


def _files_payload(projection: TaskProjection) -> list[dict[str, object]]:
    if not projection.files:
        # 无文件（失败/元数据前）时合成一项：客户端按 files[0].path 推导名字，
        # 空数组会让它落到 "???", 与 REST/网页端显示的名字割裂。
        return [
            {
                "index": "1",
                "path": _file_path(projection.name),
                "length": str(projection.total_bytes or 0),
                "completedLength": str(projection.completed_bytes),
                "selected": "true",
                "uris": display_uris(projection),
            }
        ]
    uris = display_uris(projection)
    return [
        {
            "index": str(position),
            "path": _file_path(basename_of(item.name)),
            "length": str(item.total_bytes),
            "completedLength": str(item.completed_bytes),
            "selected": "true" if item.selected else "false",
            # 首个文件项带展示 URI（客户端“复制链接”与名字兜底都用它），其余为空。
            "uris": uris if position == 1 else [],
        }
        for position, item in enumerate(projection.files, start=1)
    ]


def tell_status_payload(
    projection: TaskProjection, gid: str, *, keys: list[str] | None = None
) -> dict[str, object]:
    """aria2.tellStatus 安全投影：gid 字段回显请求传入的字符串（绝不填真实 GID）。"""
    info_hash = task_info_hash(projection)
    payload: dict[str, object] = {
        "gid": gid,
        "status": aria2_status(projection.status),
        "totalLength": str(projection.total_bytes or 0),
        "completedLength": str(projection.completed_bytes),
        # 投影只观测上传速率，不保存上传总量。
        "uploadLength": "0",
        "uploadSpeed": str(projection.upload_speed),
        "downloadSpeed": str(projection.download_speed),
        "numPieces": "1",
        "pieceLength": str(projection.total_bytes or 0),
        "connections": str(projection.connection_count),
        "errorCode": aria2_error_code(projection),
        # 仅 error 状态输出中文失败原因（旧版同语义：非失败态恒空串）。
        "errorMessage": (
            failure_message(projection.error_code)
            if projection.status == SubscriptionStatus.FAILED
            else ""
        ),
        # 目录保持 "."：真实目录是内部内容寻址存储的绝对路径，暴露即泄露服务器布局，
        # 客户端只把它当展示字段（getFiles 的 path 也刻意只有 basename）。
        "dir": _RPC_DIR,
        "files": _files_payload(projection),
        # 本地不做分块校验，因此校验中长度恒 0、校验排队恒 false。
        "verifiedLength": "0",
        "verifyIntegrityPending": "false",
    }
    # BT 专属键按 aria2 man 的语义「BitTorrent only」输出：Aria2App 以 bittorrent 键的
    # 存在性判定 isTorrent()，HTTP 任务带上它会被当成种子任务（错图标 + 额外硬读 infoHash）。
    if is_bittorrent(projection):
        payload.update(
            {
                "bittorrent": {
                    "infoHash": info_hash,
                    "announceList": [],
                    "comment": "",
                    "creationDate": 0,
                    "mode": "single",
                    "metadata": {},
                },
                "infoHash": info_hash,
                "numSeeders": "0",
                "seeder": "false",
            }
        )
    # followedBy / following / belongsTo 无数据时一律省略（man：无此类下载/无父任务则该键不出现）。
    # Aria2App 用 following != null 判定「是否元数据任务的子下载」，空串会误触删除确认弹窗。
    if keys:
        allowed = set(keys)
        payload = {key: value for key, value in payload.items() if key in allowed}
    return payload


def servers_payload(projection: TaskProjection) -> list[dict[str, object]]:
    """getServers 与 getFiles 同源：每个文件 index 一条，servers 恒空数组。

    本地没有服务器级观测（HTTP 直连经内部网关），诚实返回空列表；但**条目本身不能省**：
    客户端按 files[].index 去 SparseArray 取键，缺条目拿到 null 后在 ServersAdapter
    的 `objs.addAll(servers)` 处 NPE 闪退（ServersAdapter.java:161）。
    """
    return [{"index": entry["index"], "servers": []} for entry in _files_payload(projection)]


def uris_payload(projection: TaskProjection) -> list[dict[str, str]]:
    """getUris：只出用户提交的展示 URI；capability 与原始签名 URI 绝不出现。"""
    return display_uris(projection)


def global_stat_payload(
    counts: dict[SubscriptionStatus, int], total: int, download_speed: int, upload_speed: int
) -> dict[str, str]:
    return {
        "downloadSpeed": str(download_speed),
        "uploadSpeed": str(upload_speed),
        "numActive": str(counts.get(SubscriptionStatus.ACTIVE, 0)),
        "numWaiting": str(
            counts.get(SubscriptionStatus.QUEUED, 0)
            + counts.get(SubscriptionStatus.WAITING, 0)
            + counts.get(SubscriptionStatus.PAUSED, 0)
        ),
        "numStopped": str(
            counts.get(SubscriptionStatus.COMPLETED, 0)
            + counts.get(SubscriptionStatus.FAILED, 0)
            + counts.get(SubscriptionStatus.CANCELLED, 0)
            + counts.get(SubscriptionStatus.EXPIRED, 0)
        ),
        "numStoppedTotal": str(total),
    }


def version_payload() -> dict[str, object]:
    return {"version": RPC_VERSION, "enabledFeatures": list(RPC_FEATURES)}


def option_payload(record: DownloadRecord | None = None) -> dict[str, str]:
    values = dict(_OPTION_PROJECTION)
    if record is not None:
        values["out"] = basename_of(record.name)
    return values


@dataclass(frozen=True, slots=True)
class ResolvedGid:
    subscription_id: int
    requested_gid: str


def slice_window(items: list[TaskProjection], offset: int, num: int) -> list[TaskProjection]:
    """tellWaiting/tellStopped 窗口：正 offset 正序切片；负 offset 从末尾倒数且倒序。

    aria2 man：offset == -1 指最后一个，-2 指倒数第二个，此时响应按倒序返回
    （三元素队列下 tellWaiting(-1, 2) 为 [C, B]）。
    """
    count = len(items)
    size = max(num, 0)
    if offset >= 0:
        start = min(offset, count)
        return items[start : start + size]
    end = count + offset
    if end < 0:
        return []
    return list(reversed(items[max(end - size + 1, 0) : end + 1]))
