from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from urllib.request import Request, urlopen

from ..domain.errors import DomainError, ErrorCode
from ..domain.settings import SystemSettings


def _stop_failed() -> DomainError:
    return DomainError(ErrorCode.INVALID_STATE, "aria2 下载停止结果尚未确认")


class Aria2DownloadControl:
    def __init__(
        self, *, settings: Callable[[], SystemSettings], timeout_seconds: float = 10.0
    ) -> None:
        self.settings = settings
        self.timeout_seconds = timeout_seconds

    async def stop(self, *, gid: str) -> None:
        settings = self.settings()
        try:
            result = await asyncio.to_thread(
                self._call, settings, "aria2.forceRemove", gid
            )
        except DomainError as error:
            observed = await asyncio.to_thread(
                self._call, settings, "aria2.tellStatus", gid
            )
            if isinstance(observed, dict) and observed.get("status") in {
                "removed",
                "complete",
                "error",
            }:
                return
            raise error
        if result != gid:
            raise _stop_failed()

    def _call(self, settings: SystemSettings, method: str, gid: str) -> object:
        request_id = f"account-cleanup:{method}"
        params = [gid]
        if settings.aria2_rpc_secret is not None:
            params.insert(0, f"token:{settings.aria2_rpc_secret}")
        request = Request(  # noqa: S310  # 配置入口仅允许 HTTP(S) 地址
            settings.aria2_rpc_url,
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(  # noqa: S310  # 配置入口仅允许 HTTP(S) 地址
                request, timeout=self.timeout_seconds
            ) as response:
                body = response.read()
        except OSError:
            raise _stop_failed() from None
        try:
            payload: object = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise _stop_failed() from None
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            raise _stop_failed()
        if payload.get("id") != request_id or "error" in payload or "result" not in payload:
            raise _stop_failed()
        return payload["result"]
