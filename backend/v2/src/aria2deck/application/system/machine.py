from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol


class MachineSpacePort(Protocol):
    def machine_disk_usage(self) -> tuple[int, int, int]:
        """(total, used, free) of the filesystem holding the download directory."""
        ...

    def download_used_bytes(self) -> int:
        """Bytes occupied by the download directory itself (下载占用)."""
        ...


@dataclass(frozen=True, slots=True)
class MachineStatsView:
    total_bytes: int
    used_bytes: int
    free_bytes: int
    download_used_bytes: int
    system_used_bytes: int


class MachineStatsService:
    """E1 管理员机器磁盘统计：总量/已用/可用 + 下载目录占用（只读快照）。"""

    def __init__(self, machine_space: MachineSpacePort) -> None:
        self._machine_space = machine_space

    async def stats(self) -> MachineStatsView:
        total, used, free = await asyncio.to_thread(self._machine_space.machine_disk_usage)
        download_used = await asyncio.to_thread(self._machine_space.download_used_bytes)
        system_used = max(0, used - download_used)
        return MachineStatsView(
            total_bytes=total,
            used_bytes=used,
            free_bytes=free,
            download_used_bytes=download_used,
            system_used_bytes=system_used,
        )
