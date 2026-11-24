from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ...domain.system import backend_is_degraded


@dataclass(frozen=True, slots=True)
class BackendStatus:
    status: Literal["ok", "degraded"]
    message: str


class BackendConnectivity:
    def __init__(self) -> None:
        self._consecutive_failures = 0

    def record_success(self) -> None:
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        self._consecutive_failures += 1

    def status(self, *, is_admin: bool) -> BackendStatus:
        if not backend_is_degraded(self._consecutive_failures):
            return BackendStatus("ok", "下载后端连接正常")
        message = (
            "下载后端连续连接失败，请检查系统设置中的 aria2 配置"
            if is_admin
            else "下载后端暂时不可用，请联系管理员"
        )
        return BackendStatus("degraded", message)
