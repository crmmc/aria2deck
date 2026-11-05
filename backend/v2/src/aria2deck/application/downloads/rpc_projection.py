from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from pathlib import PurePosixPath

from ...domain.downloads import SubscriptionStatus
from .models import DownloadRecord, TaskProjection

GID_LENGTH = 16

# RPC-15/16 共用的唯一状态映射表：订阅状态 → aria2 原生 status 字符串。
_ARIA2_STATUS: dict[SubscriptionStatus, str] = {
    SubscriptionStatus.QUEUED: "queued",
    SubscriptionStatus.ACTIVE: "active",
    SubscriptionStatus.WAITING: "waiting",
    SubscriptionStatus.PAUSED: "paused",
    SubscriptionStatus.COMPLETED: "complete",
    SubscriptionStatus.FAILED: "error",
    SubscriptionStatus.CANCELLED: "removed",  # 文件删除取消 → removed（历史版本 326aeff 语义重放）
    SubscriptionStatus.EXPIRED: "removed",
}

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


def _files_payload(projection: TaskProjection) -> list[dict[str, object]]:
    return [
        {
            "index": str(position),
            "path": basename_of(item.name),
            "length": str(item.total_bytes),
            "completedLength": str(item.completed_bytes),
            "selected": "true" if item.selected else "false",
            "uris": [],
        }
        for position, item in enumerate(projection.files, start=1)
    ]


def tell_status_payload(
    projection: TaskProjection, gid: str, *, keys: list[str] | None = None
) -> dict[str, object]:
    """aria2.tellStatus 安全投影：gid 字段回显请求传入的字符串（绝不填真实 GID）。"""
    payload: dict[str, object] = {
        "gid": gid,
        "status": aria2_status(projection.status),
        "totalLength": str(projection.total_bytes or 0),
        "completedLength": str(projection.completed_bytes),
        "uploadSpeed": str(projection.upload_speed),
        "downloadSpeed": str(projection.download_speed),
        "numPieces": "1",
        "pieceLength": str(projection.total_bytes or 0),
        "connections": str(projection.connection_count),
        "errorCode": projection.error_code or "0",
        "errorMessage": "",
        "dir": ".",
        "files": _files_payload(projection),
        "bittorrent": {
            "infoHash": "",
            "announceList": [],
            "comment": "",
            "creationDate": 0,
            "mode": "single",
            "metadata": {},
        },
        "followedBy": [],
        "following": "",
        "belongsTo": "",
        "sequence": "0",
        "numSeeders": "0",
        "seeder": "false",
    }
    if keys:
        allowed = set(keys)
        payload = {key: value for key, value in payload.items() if key in allowed}
    return payload


def uris_payload(projection: TaskProjection) -> list[dict[str, str]]:
    """getUris：只出用户提交的展示 URI；capability 与原始签名 URI 绝不出现。"""
    if projection.display_uri is None:
        return []
    return [{"uri": projection.display_uri, "status": "used"}]


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
    """tellWaiting/tellStopped offset/num：负 offset 从尾部数（aria2 语义）。"""
    count = len(items)
    if offset < 0:
        start = max(count + offset, 0)
    else:
        start = min(offset, count)
    if num < 0:
        num = 0
    return items[start : start + num]
