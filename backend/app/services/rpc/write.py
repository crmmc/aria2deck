"""aria2 RPC write methods (M4 T14).

Implementations of the state-mutating RPC methods, extracted from
``aria2_rpc_handler.py``. Each function takes ``user_id`` explicitly;
``Aria2RpcHandler`` (services/rpc/system.py) keeps thin delegates that
forward ``self.user_id``.

Behaviour is unchanged from the legacy handler.
"""

from __future__ import annotations

from typing import Any

from app.core.config import get_internal_base_url
from app.core.security import (
    WEBSEED_URI_SCHEMES,
    check_torrent_network_endpoints,
)
from app.domain.errors import (
    BadGatewayError,
    ConflictError,
    NotFoundError,
)
from app.domain.status import ACTIVE_USER_TASK_STATUSES
from app.domain.torrent_metadata import (
    TorrentMetadataError,
    build_select_file_option,
    build_selection_resource_key,
    parse_torrent_base64_async,
    selected_total_size,
)
from app.http.safe_client import UnsafeTargetError, normalize_public_http_url
from app.modules.task_core.register import ResourceSpec
from app.repositories.task.sources import torrent_source_uri_placeholder
from app.services import task_service
from app.services.gateway import (
    http_resource_identity,
    source_request_options,
)
from app.services.rpc._shared import (
    RpcError,
    RpcErrorCode,
    _check_quota_and_disk,
    _extract_name_from_uri,
    _get_user_quota,
    _gid_for_created_task,
    _raise_create_download_error,
    _resolve_owned_row,
    _resource_key_for_uri,
    _resource_kind_for_uri,
    _selected_torrent_indexes,
    _validate_submit_options,
    _validate_uri_list,
    _with_rpc_mirrors,
)

RPC_ADD_URI_SCHEMES = frozenset({"http", "https", "magnet"})


async def _handle_add_uri(user_id: int, params: list) -> str:
    """aria2.addUri(uris[, options[, position]])"""
    if not params:
        raise RpcError(RpcErrorCode.INVALID_PARAMS, "uris is required")
    submit_uris = await _validate_uri_list(
        params[0],
        name="uris",
        allowed_schemes=RPC_ADD_URI_SCHEMES,
        allow_empty=False,
    )
    if len(submit_uris) > 1 and any(
        item.lower().startswith("magnet:") for item in submit_uris
    ):
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            "magnet URI does not support mirrors",
        )
    options = (
        dict(params[1]) if len(params) > 1 and isinstance(params[1], dict) else {}
    )
    _validate_submit_options(options)
    if "bt-tracker" in options:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "bt-tracker option is not allowed"
        )
    options = _with_rpc_mirrors(options, submit_uris)
    await _check_quota_and_disk(user_id)
    uri = submit_uris[0]

    try:
        if _resource_kind_for_uri(uri) == "http":
            try:
                uri = normalize_public_http_url(uri)
            except UnsafeTargetError as exc:
                raise RpcError(RpcErrorCode.INVALID_PARAMS, str(exc)) from exc
            get_internal_base_url()
            source_opts = source_request_options(
                options, mirrors=submit_uris[1:]
            )
            resource_key = http_resource_identity(
                _resource_key_for_uri(uri), source_opts
            )
        else:
            resource_key = _resource_key_for_uri(uri)

        resource = ResourceSpec(
            resource_key=resource_key,
            source_uri=uri,
            resource_kind=_resource_kind_for_uri(uri),
            display_name=_extract_name_from_uri(uri) or uri,
            size_bytes=0,
            size_known=False,
            source_payload=uri,
            source_options=options,
        )
        task = await task_service.register_and_submit(
            user_id=user_id,
            quota_bytes=await _get_user_quota(user_id),
            resource=resource,
            options=options,
        )
    except Exception as exc:
        _raise_create_download_error(user_id, exc)

    return await _gid_for_created_task(task, resource_key)


async def _handle_add_torrent(user_id: int, params: list) -> str:
    """aria2.addTorrent(torrent[, uris[, options[, position]]])"""
    if not params or not isinstance(params[0], str):
        raise RpcError(RpcErrorCode.INVALID_PARAMS, "torrent data is required")
    torrent_data = params[0]
    # 限制 torrent 文件大小（10MB）
    if len(torrent_data) > 10 * 1024 * 1024:
        raise RpcError(RpcErrorCode.INVALID_PARAMS, "Torrent data too large")
    webseed_uris = await _validate_uri_list(
        params[1] if len(params) > 1 else [],
        name="uris",
        allowed_schemes=WEBSEED_URI_SCHEMES,
        allow_empty=True,
    )
    if webseed_uris:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            "Torrent webseed URIs are not allowed",
        )
    try:
        metadata = await parse_torrent_base64_async(torrent_data)
    except TorrentMetadataError as exc:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            f"无效的种子文件: {exc}",
        ) from exc
    endpoint_error = await check_torrent_network_endpoints(
        metadata.tracker_urls,
        metadata.webseed_urls,
    )
    if endpoint_error:
        raise RpcError(RpcErrorCode.INVALID_PARAMS, endpoint_error)
    options = (
        dict(params[2]) if len(params) > 2 and isinstance(params[2], dict) else {}
    )
    if "bt-tracker" in options:
        raise RpcError(RpcErrorCode.INVALID_PARAMS, "bt-tracker option is not allowed")
    selected_indexes = _selected_torrent_indexes(
        metadata, options.pop("select-file", None)
    )
    selected_size = selected_total_size(metadata, selected_indexes)
    select_file = build_select_file_option(
        selected_indexes, metadata.file_count
    )
    submit_options = dict(options)
    if select_file:
        submit_options["select-file"] = select_file
    await _check_quota_and_disk(user_id)
    resource_key = build_selection_resource_key(
        metadata.info_hash,
        selected_indexes,
        total_file_count=metadata.file_count,
    )
    magnet_uri = f"magnet:?xt=urn:btih:{metadata.info_hash}"

    try:
        selection_indexes = selected_indexes if select_file else None
        resource = ResourceSpec(
            resource_key=resource_key,
            source_uri=torrent_source_uri_placeholder(metadata.info_hash),
            resource_kind="torrent",
            display_name=metadata.name,
            size_bytes=selected_size,
            size_known=True,
            display_uri=magnet_uri,
            source_payload=f"base64:{torrent_data}",
            selection_indexes=selection_indexes,
            source_options=options,
        )
        task = await task_service.register_and_submit(
            user_id=user_id,
            quota_bytes=await _get_user_quota(user_id),
            resource=resource,
            options=submit_options,
        )
    except Exception as exc:
        _raise_create_download_error(user_id, exc)

    return await _gid_for_created_task(task, resource_key)


async def _handle_remove(user_id: int, params: list) -> str:
    """aria2.remove(gid)"""
    if not params:
        raise RpcError(RpcErrorCode.INVALID_PARAMS, "gid is required")
    gid = str(params[0])
    row = await _resolve_owned_row(user_id, gid)
    if row is None or row["status"] not in ACTIVE_USER_TASK_STATUSES:
        raise RpcError(RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}")
    try:
        await task_service.cancel_task(
            user_id=user_id,
            user_task_id=int(row["id"]),
            quota_bytes=await _get_user_quota(user_id),
        )
    except (NotFoundError, ConflictError):
        raise RpcError(
            RpcErrorCode.TASK_NOT_FOUND, f"Task not found: {gid}"
        ) from None
    except BadGatewayError as exc:
        raise RpcError(
            RpcErrorCode.INTERNAL_ERROR, "Internal error"
        ) from exc
    return gid


async def _handle_force_remove(user_id: int, params: list) -> str:
    """aria2.forceRemove(gid) - 同 remove"""
    return await _handle_remove(user_id, params)
