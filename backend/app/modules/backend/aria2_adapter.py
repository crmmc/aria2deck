"""aria2 backend adapter for Task Core.

Implements ``BackendPort`` by wrapping ``Aria2Client`` and reading the
tid ↔ gid mapping from ``global_downloads.aria2_gid``. After a successful
submit the adapter persists the gid via ``assign_submitted_gid`` so later
``tell_many``/``pause``/``remove`` calls resolve the gid from the DB.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from app.aria2.client import Aria2Client
from app.modules.backend.port import Snapshot
from app.repositories.task.downloads import (
    assign_submitted_gid,
    clear_terminal_download_gid,
    get_global_download_by_id,
)
from app.services.gateway import build_gateway_submission
from app.services.settings_service import get_aria2_bt_stop_timeout_seconds
from app.services.storage import get_task_download_dir

logger = logging.getLogger(__name__)

_ALLOWED_USER_OPTIONS = frozenset(
    ("out", "header", "max-connection-per-server", "http-user", "http-passwd")
)
_SERVER_PASSTHROUGH_OPTIONS = frozenset(("select-file",))


def _normalize_out_option(value: Any) -> str:
    out = str(value)
    if not out or out in {".", ".."} or "/" in out or "\\" in out:
        raise ValueError(
            "invalid out option: must be a filename without path separators"
        )
    return out


class Aria2BackendAdapter:
    """BackendPort implementation backed by aria2 RPC."""

    def __init__(self, client: Aria2Client) -> None:
        self._client = client

    async def submit(self, *, tid: int, uri: str, options: dict[str, Any]) -> str:
        """Submit a download to aria2 and persist the returned gid."""
        download = await get_global_download_by_id(tid)
        if download is None:
            raise ValueError(f"tid {tid} not found")

        resource_kind = str(download.get("resource_kind") or "")
        unknown_size = not bool(download.get("size_known"))

        submit_options = self._build_base_options(tid)

        gid: str
        if resource_kind == "torrent" and uri.startswith("base64:"):
            self._merge_user_and_server_options(submit_options, options)
            gid = await self._client.add_torrent(
                uri[len("base64:"):], [], submit_options
            )
        elif resource_kind == "http":
            mirrors = [str(item) for item in (options.get("mirrors") or [])]
            gateway_uris, gateway_options = build_gateway_submission(
                download_id=tid,
                source_uri=uri,
                options=options,
                source_uris=[uri, *mirrors],
            )
            submit_options.update(gateway_options)
            if unknown_size:
                submit_options["pause"] = "true"
            gid = await self._client.add_uri(gateway_uris, submit_options)
            # 多 mirror 场景：capability 中已带 mirrors，需要把剩余 gateway
            # uri 追加到当前 gid，保证 aria2 侧可见。
            if len(gateway_uris) > 1:
                try:
                    await self._client.change_uri(gid, 1, [], gateway_uris[1:])
                except Exception:
                    logger.warning(
                        "补发 mirror 失败 tid=%s gid=%s",
                        tid,
                        gid,
                        exc_info=True,
                    )
        else:
            self._merge_user_and_server_options(submit_options, options)
            if unknown_size and resource_kind == "magnet":
                submit_options["pause-metadata"] = "true"
            gid = await self._client.add_uri([uri], submit_options)

        status = "active" if resource_kind == "magnet" or not unknown_size else "waiting"
        updated = await assign_submitted_gid(download_id=tid, gid=gid, status=status)
        if updated is None:
            raise RuntimeError(f"failed to persist submitted gid for tid {tid}")
        return gid

    @staticmethod
    def _build_base_options(tid: int) -> dict[str, Any]:
        task_dir = get_task_download_dir(tid)
        return {
            "dir": str(task_dir),
            "seed-time": "0",
            "bt-stop-timeout": str(get_aria2_bt_stop_timeout_seconds()),
        }

    @staticmethod
    def _merge_user_and_server_options(
        submit_options: dict[str, Any], user_options: Mapping[str, Any] | None
    ) -> None:
        if not user_options:
            return
        for key in _ALLOWED_USER_OPTIONS:
            if key in user_options:
                if key == "out":
                    submit_options[key] = _normalize_out_option(user_options[key])
                else:
                    submit_options[key] = str(user_options[key])
        for key in _SERVER_PASSTHROUGH_OPTIONS:
            if key in user_options:
                submit_options[str(key)] = str(user_options[key])

    async def tell_many(self, tids: Sequence[int]) -> list[Snapshot]:
        """Fetch snapshots for the given tids from aria2."""
        snapshots: list[Snapshot] = []
        for tid in tids:
            download = await get_global_download_by_id(tid)
            if download is None:
                continue
            gid = download.get("aria2_gid")
            if not gid:
                continue
            try:
                raw = await self._client.tell_status(str(gid))
            except Exception:
                continue
            snapshots.append(
                Snapshot(
                    tid=tid,
                    status=str(raw.get("status", "")),
                    error_code=raw.get("errorCode"),
                    raw=raw,
                )
            )
        return snapshots

    async def _resolve_gid(self, tid: int) -> str:
        download = await get_global_download_by_id(tid)
        if download is None:
            raise ValueError(f"tid {tid} not found")
        gid = download.get("aria2_gid")
        if not gid:
            raise ValueError(f"tid {tid} has no aria2 gid")
        return str(gid)

    async def pause(self, tid: int) -> None:
        gid = await self._resolve_gid(tid)
        await self._client.pause(gid)

    async def unpause(self, tid: int) -> None:
        gid = await self._resolve_gid(tid)
        await self._client.unpause(gid)

    async def remove(self, tid: int) -> None:
        """Stop backend writer for ``tid`` when a gid is bound.

        Uses ``remove`` / ``remove_download_result`` only (not ``force_remove``)
        so physical reclaim stays within BackendPort semantics. Clears residual
        gid only after a successful stop and only for terminal rows.
        """
        try:
            gid = await self._resolve_gid(tid)
        except ValueError:
            # No gid (e.g. failed submit rollback) — nothing to stop.
            return

        writer_stopped = False
        try:
            await self._client.remove(gid)
            writer_stopped = True
        except Exception:
            try:
                await self._client.remove_download_result(gid)
                writer_stopped = True
            except Exception:
                logger.warning(
                    "backend remove failed tid=%s gid=%s", tid, gid, exc_info=True
                )
                return

        if not writer_stopped:
            return
        try:
            await clear_terminal_download_gid(tid, expected_gid=gid)
        except Exception:
            logger.warning(
                "clear terminal gid failed tid=%s gid=%s", tid, gid, exc_info=True
            )

    async def tell_status(self, gid: str) -> dict[str, Any]:
        return await self._client.tell_status(gid)

    async def pause_gid(self, gid: str) -> str:
        return await self._client.pause(gid)

    async def unpause_gid(self, gid: str) -> str:
        return await self._client.unpause(gid)

    async def tell_active(self) -> list[dict[str, Any]]:
        return await self._client.tell_active()

    async def tell_waiting(
        self, offset: int = 0, num: int = 1000
    ) -> list[dict[str, Any]]:
        return await self._client.tell_waiting(offset, num)

    async def tell_stopped(
        self, offset: int = 0, num: int = 1000
    ) -> list[dict[str, Any]]:
        return await self._client.tell_stopped(offset, num)

    async def force_remove_gid(self, gid: str) -> str:
        return await self._client.force_remove(gid)

    async def remove_download_result_gid(self, gid: str) -> str:
        return await self._client.remove_download_result(gid)

    async def join_submission(
        self, *, tid: int, gid: str, uris: list[str]
    ) -> None:
        """对已提交的 gid 重新下发 URI 列表（mirror/capability 补发）。"""
        if not uris:
            return
        await self._client.change_uri(gid, 1, [], uris)
