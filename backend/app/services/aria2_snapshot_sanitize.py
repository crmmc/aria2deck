"""aria2 快照脱敏共享层

把 ``Aria2RpcHandler`` 中面向用户响应的脱敏逻辑抽成可复用模块，
供 sync 写投影（task_backend_snapshots）和 RPC 读模型共用。

行为与原 ``_sanitize_status/_sanitize_files/_sanitize_uris/_sanitize_bittorrent``
完全一致，仅做搬迁，不改语义。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services.task_projection import (
    build_bittorrent_payload,
    has_bittorrent_payload,
    has_live_bt_evidence,
)

DEFAULT_STATUS_DOWNLOADS = "0"
DEFAULT_STATUS_WAITING = "waiting"
DEFAULT_BOOL_FALSE = "false"
DEFAULT_ERROR_CODE = "0"

ALLOWED_STATUS_VALUES = {
    "active",
    "waiting",
    "paused",
    "error",
    "complete",
    "removed",
}


def _status_str(value: Any, default: str = DEFAULT_STATUS_DOWNLOADS) -> str:
    if value is None:
        return default
    return str(value)


def _status_bool(value: Any, default: str = DEFAULT_BOOL_FALSE) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "false"}:
            return normalized
    return default


def _normalize_status_value(
    value: Any, default: str = DEFAULT_STATUS_WAITING
) -> str:
    status = str(value).strip().lower() if value is not None else default
    if status in ALLOWED_STATUS_VALUES:
        return status
    return default


def _sanitize_file_path(path: str) -> str:
    if not path:
        return path
    return Path(path).name


def new_file_payload() -> dict[str, Any]:
    return {
        "index": "1",
        "path": "",
        "length": "0",
        "completedLength": "0",
        "selected": "true",
        "uris": [],
    }


def new_status_payload() -> dict[str, Any]:
    return {
        "gid": "",
        "status": DEFAULT_STATUS_WAITING,
        "totalLength": DEFAULT_STATUS_DOWNLOADS,
        "completedLength": DEFAULT_STATUS_DOWNLOADS,
        "uploadLength": DEFAULT_STATUS_DOWNLOADS,
        "downloadSpeed": DEFAULT_STATUS_DOWNLOADS,
        "uploadSpeed": DEFAULT_STATUS_DOWNLOADS,
        "pieceLength": DEFAULT_STATUS_DOWNLOADS,
        "numPieces": DEFAULT_STATUS_DOWNLOADS,
        "connections": DEFAULT_STATUS_DOWNLOADS,
        "errorCode": DEFAULT_ERROR_CODE,
        "errorMessage": "",
        "dir": "",
        "files": [],
    }


def sanitize_uris(uris: Any) -> list[dict]:
    if not isinstance(uris, list):
        return []
    sanitized_uris: list[dict] = []
    for item in uris:
        if not isinstance(item, dict):
            continue
        status = _status_str(item.get("status"), "waiting")
        if status not in {"used", "waiting"}:
            status = "waiting"
        sanitized_uris.append({"uri": "", "status": status})
    return sanitized_uris


def sanitize_files(files: Any) -> list[dict]:
    if not isinstance(files, list):
        return []

    sanitized_files: list[dict] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        file_data = new_file_payload()
        file_data["index"] = _status_str(item.get("index"), file_data["index"])
        file_data["length"] = _status_str(
            item.get("length"), file_data["length"]
        )
        file_data["completedLength"] = _status_str(
            item.get("completedLength"), file_data["completedLength"]
        )
        file_data["selected"] = _status_bool(
            item.get("selected"), file_data["selected"]
        )
        file_data["path"] = _sanitize_file_path(
            _status_str(item.get("path"), "")
        )
        file_data["uris"] = sanitize_uris(item.get("uris"))
        sanitized_files.append(file_data)
    return sanitized_files


def sanitize_bittorrent(bt_info: Any) -> dict:
    name = ""
    mode = "single"
    if not isinstance(bt_info, dict):
        return build_bittorrent_payload(name, mode)

    info = bt_info.get("info")
    if isinstance(info, dict):
        raw_name = info.get("name")
        if isinstance(raw_name, str):
            name = raw_name

    raw_mode = bt_info.get("mode")
    if isinstance(raw_mode, str) and raw_mode.strip():
        mode = raw_mode

    return build_bittorrent_payload(name, mode)


def sanitize_status(status: dict) -> dict:
    """对 tellStatus 返回的数据进行脱敏处理"""
    result = new_status_payload()

    result["gid"] = _status_str(status.get("gid"), "")
    result["status"] = _normalize_status_value(status.get("status"))
    result["totalLength"] = _status_str(
        status.get("totalLength"), DEFAULT_STATUS_DOWNLOADS
    )
    result["completedLength"] = _status_str(
        status.get("completedLength"), DEFAULT_STATUS_DOWNLOADS
    )
    result["uploadLength"] = _status_str(
        status.get("uploadLength"), DEFAULT_STATUS_DOWNLOADS
    )
    result["downloadSpeed"] = _status_str(
        status.get("downloadSpeed"), DEFAULT_STATUS_DOWNLOADS
    )
    result["uploadSpeed"] = _status_str(
        status.get("uploadSpeed"), DEFAULT_STATUS_DOWNLOADS
    )
    result["pieceLength"] = _status_str(
        status.get("pieceLength"), DEFAULT_STATUS_DOWNLOADS
    )
    result["numPieces"] = _status_str(
        status.get("numPieces"), DEFAULT_STATUS_DOWNLOADS
    )
    result["connections"] = _status_str(
        status.get("connections"), DEFAULT_STATUS_DOWNLOADS
    )
    result["errorCode"] = _status_str(
        status.get("errorCode"), DEFAULT_ERROR_CODE
    )
    raw_error_message = status.get("errorMessage")
    result["errorMessage"] = (
        "aria2 下载失败" if isinstance(raw_error_message, str) and raw_error_message else ""
    )
    result["dir"] = ""
    result["files"] = sanitize_files(status.get("files"))
    if has_live_bt_evidence(status) or has_bittorrent_payload(status):
        result["infoHash"] = _status_str(status.get("infoHash"), "")
        result["numSeeders"] = _status_str(
            status.get("numSeeders"), DEFAULT_STATUS_DOWNLOADS
        )
        result["seeder"] = _status_bool(
            status.get("seeder"), DEFAULT_BOOL_FALSE
        )
        result["bittorrent"] = sanitize_bittorrent(status.get("bittorrent"))

    bitfield = status.get("bitfield")
    if isinstance(bitfield, str):
        result["bitfield"] = bitfield

    followed_by = status.get("followedBy")
    if isinstance(followed_by, list):
        gids = [str(gid) for gid in followed_by if isinstance(gid, (str, int))]
        result["followedBy"] = gids

    following = status.get("following")
    if following is not None:
        result["following"] = _status_str(following, "")

    belongs_to = status.get("belongsTo")
    if belongs_to is not None:
        result["belongsTo"] = _status_str(belongs_to, "")

    verified_length = status.get("verifiedLength")
    if verified_length is not None:
        result["verifiedLength"] = _status_str(
            verified_length, DEFAULT_STATUS_DOWNLOADS
        )

    verify_integrity_pending = status.get("verifyIntegrityPending")
    if verify_integrity_pending is not None:
        result["verifyIntegrityPending"] = _status_bool(
            verify_integrity_pending
        )

    return result
