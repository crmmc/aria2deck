from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace as dataclasses_replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from ..application.downloads.models import (
    BackendSubmissionResult,
    DownloadObservation,
    ObservedFile,
    Submission,
)
from ..domain.downloads import submission_pause_options
from ..domain.errors import DomainError, ErrorCode
from ..domain.settings import SystemSettings
from .security import CredentialManager


def _uncertain(message: str = "aria2 提交结果无法确认") -> DomainError:
    return DomainError(ErrorCode.SUBMISSION_UNCERTAIN, message)


_MISSING = object()


class _RpcRejected(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class Aria2SubmissionClient:
    def __init__(
        self,
        *,
        settings: Callable[[], SystemSettings],
        credentials: CredentialManager,
        internal_base_url: str,
        download_directory: Path,
        timeout_seconds: float = 10.0,
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        self.settings = settings
        self.credentials = credentials
        self.internal_base_url = internal_base_url.rstrip("/")
        self.download_directory = download_directory
        self.timeout_seconds = timeout_seconds
        self.clock_ms = clock_ms

    async def _call(
        self,
        settings: SystemSettings,
        method: str,
        params: list[object],
        *,
        authenticate: bool = True,
    ) -> object:
        request_id = f"v2:{uuid.uuid4().hex}"
        actual_params = list(params)
        secret = settings.aria2_rpc_secret
        if authenticate and secret is not None:
            actual_params.insert(0, f"token:{secret}")
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": actual_params,
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(settings.aria2_rpc_url, json=body) as response:
                    if response.status != 200:
                        raise _uncertain()
                    raw = await response.read()
        except (TimeoutError, aiohttp.ClientError, OSError):
            raise _uncertain() from None
        try:
            payload: object = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise _uncertain("aria2 返回了无效响应") from None
        if (
            not isinstance(payload, dict)
            or payload.get("jsonrpc") != "2.0"
            or payload.get("id") != request_id
        ):
            raise _uncertain("aria2 响应身份不匹配")
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            raise _RpcRejected(code if isinstance(code, int) else -32603)
        if "result" not in payload:
            raise _uncertain("aria2 响应缺少结果")
        return payload["result"]

    def _internal_uri(self, *, attempt_id: int, source_index: int) -> str:
        return f"{self.internal_base_url}/api/v2/internal/fetch/{attempt_id}/{source_index}"

    def _call_spec(self, submission: Submission, trackers: tuple[str, ...]) -> dict[str, object]:
        source = submission.source
        options: dict[str, object] = dict(submission_pause_options(source.kind))
        options["gid"] = submission.gid
        options["dir"] = str(self.download_directory / str(submission.attempt_id))
        if trackers and source.kind in {"magnet", "torrent"}:
            options["bt-tracker"] = ",".join(trackers)
        params: list[object]
        if source.kind == "http":
            raw_uris = source.payload.get("uris")
            if not isinstance(raw_uris, list) or not raw_uris or not all(
                isinstance(uri, str) for uri in raw_uris
            ):
                raise DomainError(ErrorCode.INVALID_STATE, "HTTP 下载来源数据无效")
            uris = [
                self._internal_uri(attempt_id=submission.attempt_id, source_index=index)
                for index in range(len(raw_uris))
            ]
            options["out"] = source.display_name
            capability = self.credentials.sign_download_fetch(
                attempt_id=submission.attempt_id, source_index=0
            )
            options["header"] = [f"X-Mjiz-Fetch-Capability: {capability}"]
            params = [uris, options]
            method = "aria2.addUri"
        elif source.kind == "magnet":
            uri = source.payload.get("uri")
            if not isinstance(uri, str):
                raise DomainError(ErrorCode.INVALID_STATE, "Magnet 下载来源数据无效")
            # 用户级 out / max-connection-per-server 透传给 aria2（A9，旧版 magnet 路径同位）。
            if isinstance(source.options.get("out"), str):
                options["out"] = source.options["out"]
            if isinstance(source.options.get("max-connection-per-server"), int):
                options["max-connection-per-server"] = str(
                    source.options["max-connection-per-server"]
                )
            params = [[uri], options]
            method = "aria2.addUri"
        elif source.kind == "torrent":
            torrent = source.payload.get("torrent")
            if not isinstance(torrent, str):
                raise DomainError(ErrorCode.INVALID_STATE, "种子下载来源数据无效")
            if source.selected_file_indexes:
                options["select-file"] = ",".join(
                    str(index) for index in source.selected_file_indexes
                )
            params = [torrent, [], options]
            method = "aria2.addTorrent"
        else:
            raise DomainError(ErrorCode.INVALID_STATE, "下载来源类型无效")
        secret = self.settings().aria2_rpc_secret
        if secret is not None:
            params.insert(0, f"token:{secret}")
        return {"methodName": method, "params": params}

    async def submit_many(
        self, submissions: list[Submission], *, trackers: tuple[str, ...]
    ) -> list[BackendSubmissionResult]:
        if not submissions:
            return []
        settings = self.settings()
        calls = [self._call_spec(submission, trackers) for submission in submissions]
        try:
            result = await self._call(settings, "system.multicall", [calls], authenticate=False)
        except _RpcRejected:
            raise _uncertain("aria2 批量提交被拒绝") from None
        if not isinstance(result, list) or len(result) != len(submissions):
            raise _uncertain("aria2 批量响应数量不匹配")
        replies: list[BackendSubmissionResult] = []
        for submission, item in zip(submissions, result, strict=True):
            if isinstance(item, dict):
                fault = item.get("faultCode")
                if not isinstance(fault, int):
                    raise _uncertain("aria2 批量错误响应无效")
                replies.append(
                    BackendSubmissionResult(False, error_code=f"aria2_{fault}")
                )
            elif (
                isinstance(item, list)
                and len(item) == 1
                and isinstance(item[0], str)
                and item[0] == submission.gid
            ):
                replies.append(BackendSubmissionResult(True, gid=item[0]))
            else:
                raise _uncertain("aria2 批量响应身份不匹配")
        return replies

    async def confirm(self, *, gid: str) -> bool:
        try:
            result = await self._call(self.settings(), "aria2.tellStatus", [gid])
        except _RpcRejected:
            return False
        return isinstance(result, dict) and result.get("gid") == gid

    async def version(
        self, *, url: str | None = None, secret: str | None = None
    ) -> tuple[str, list[str]]:
        """aria2.getVersion：连接测试用，失败统一抛 DomainError（不可达/认证失败/超时）。

        传入表单值时用请求值探测（不落库）；None 表示沿用当前配置。
        """
        settings = self.settings()
        if url is not None or secret is not None:
            settings = dataclasses_replace(
                settings,
                aria2_rpc_url=url if url is not None else settings.aria2_rpc_url,
                aria2_rpc_secret=secret if secret is not None else settings.aria2_rpc_secret,
            )
        try:
            result = await self._call(settings, "aria2.getVersion", [])
        except _RpcRejected:
            raise _uncertain("aria2 连接测试请求被拒绝") from None
        if not isinstance(result, dict):
            raise _uncertain("aria2 版本响应无效")
        version = result.get("version")
        if not isinstance(version, str):
            raise _uncertain("aria2 版本响应无效")
        features_value = result.get("enabledFeatures")
        features = (
            [item for item in features_value if isinstance(item, str)]
            if isinstance(features_value, list)
            else []
        )
        return version, features

    async def stop(self, *, gid: str) -> None:
        settings = self.settings()
        try:
            result = await self._call(settings, "aria2.forceRemove", [gid])
            if result != gid:
                raise DomainError(ErrorCode.INVALID_STATE, "aria2 下载停止结果尚未确认")
            return
        except _RpcRejected:
            pass
        try:
            observed = await self._call(settings, "aria2.tellStatus", [gid])
        except _RpcRejected:
            raise DomainError(ErrorCode.INVALID_STATE, "aria2 下载停止结果尚未确认") from None
        if not isinstance(observed, dict) or observed.get("status") not in {
            "removed",
            "complete",
            "error",
        }:
            raise DomainError(ErrorCode.INVALID_STATE, "aria2 下载停止结果尚未确认")


    @staticmethod
    def _unavailable() -> DomainError:
        return DomainError(ErrorCode.RPC_UNAVAILABLE, "aria2 暂时不可用")

    async def _lifecycle_call(
        self, method: str, params: list[object], *, missing_ok: bool = False
    ) -> object | None:
        try:
            return await self._call(self.settings(), method, params)
        except _RpcRejected as error:
            if missing_ok and error.code == 1:
                return _MISSING
            raise self._unavailable() from None
        except DomainError:
            raise self._unavailable() from None

    @staticmethod
    def _number(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, str | int):
            raise Aria2SubmissionClient._unavailable()
        try:
            number = int(value)
        except ValueError:
            raise Aria2SubmissionClient._unavailable() from None
        if number < 0:
            raise Aria2SubmissionClient._unavailable()
        return number

    @staticmethod
    def _gids(value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list) or not all(
            isinstance(gid, str) and gid for gid in value
        ):
            raise Aria2SubmissionClient._unavailable()
        return tuple(value)

    def _parse_snapshot(self, result: object, gid: str) -> DownloadObservation:
        if not isinstance(result, dict) or result.get("gid") != gid:
            raise self._unavailable()
        status = result.get("status")
        if status not in {"active", "waiting", "paused", "complete", "error", "removed"}:
            raise self._unavailable()
        raw_files = result.get("files", [])
        if not isinstance(raw_files, list):
            raise self._unavailable()
        files: list[ObservedFile] = []
        for item in raw_files:
            if not isinstance(item, dict):
                raise self._unavailable()
            raw_path = item.get("path", "")
            if not isinstance(raw_path, str):
                raise self._unavailable()
            selected = item.get("selected", "true")
            if selected not in {"true", "false", True, False}:
                raise self._unavailable()
            files.append(
                ObservedFile(
                    name=raw_path.replace("\\", "/").rsplit("/", 1)[-1],
                    total_bytes=self._number(item.get("length", "0")),
                    completed_bytes=self._number(item.get("completedLength", "0")),
                    selected=selected in {"true", True},
                )
            )
        return DownloadObservation(
            gid=gid,
            observed_at_ms=self.clock_ms(),
            status=status,
            total_length=self._number(result.get("totalLength", "0")),
            completed_length=self._number(result.get("completedLength", "0")),
            download_speed=self._number(result.get("downloadSpeed", "0")),
            upload_speed=self._number(result.get("uploadSpeed", "0")),
            peer_count=self._number(result.get("numPeers", "0")),
            connection_count=self._number(result.get("connections", "0")),
            files=tuple(files),
            followed_by=self._gids(result.get("followedBy")),
            following=self._gids(result.get("following")),
        )

    async def snapshot(self, *, gid: str) -> DownloadObservation | None:
        result = await self._lifecycle_call("aria2.tellStatus", [gid], missing_ok=True)
        if result is _MISSING:
            return None
        return self._parse_snapshot(result, gid)

    async def unpause(self, *, gid: str) -> None:
        if await self._lifecycle_call("aria2.unpause", [gid]) != gid:
            raise self._unavailable()

    async def remove_result(self, *, gid: str) -> None:
        result = await self._lifecycle_call(
            "aria2.removeDownloadResult", [gid], missing_ok=True
        )
        if result is not _MISSING and result != "OK":
            raise self._unavailable()

    async def list_live_gids(self) -> list[str]:
        active = await self._lifecycle_call("aria2.tellActive", [])
        if not isinstance(active, list):
            raise self._unavailable()
        gids = [self._parse_snapshot(item, str(item.get("gid"))).gid for item in active if isinstance(item, dict)]
        if len(gids) != len(active):
            raise self._unavailable()
        offset = 0
        while True:
            waiting = await self._lifecycle_call("aria2.tellWaiting", [offset, 1000])
            if not isinstance(waiting, list):
                raise self._unavailable()
            parsed = [
                self._parse_snapshot(item, str(item.get("gid"))).gid
                for item in waiting
                if isinstance(item, dict)
            ]
            if len(parsed) != len(waiting):
                raise self._unavailable()
            gids.extend(parsed)
            if len(waiting) < 1000:
                return list(dict.fromkeys(gids))
            offset += len(waiting)

    async def listen(self, on_gid: Callable[[str], Awaitable[None]]) -> None:
        settings = self.settings()
        parsed = urlsplit(settings.aria2_rpc_url)
        if parsed.scheme not in {"http", "https"}:
            raise self._unavailable()
        url = urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc, parsed.path, parsed.query, ""))
        methods = {
            "aria2.onDownloadStart",
            "aria2.onDownloadPause",
            "aria2.onDownloadStop",
            "aria2.onDownloadComplete",
            "aria2.onDownloadError",
            "aria2.onBtDownloadComplete",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    url, timeout=aiohttp.ClientWSTimeout(ws_close=self.timeout_seconds)
                ) as socket:
                    async for message in socket:
                        if message.type is aiohttp.WSMsgType.ERROR:
                            raise self._unavailable()
                        if message.type is not aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            payload: object = json.loads(message.data)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(payload, dict) or payload.get("method") not in methods:
                            continue
                        params = payload.get("params")
                        if not isinstance(params, list) or not params:
                            continue
                        event = params[0]
                        if not isinstance(event, dict) or not isinstance(event.get("gid"), str):
                            continue
                        await on_gid(event["gid"])
        except (TimeoutError, aiohttp.ClientError, OSError):
            raise self._unavailable() from None
