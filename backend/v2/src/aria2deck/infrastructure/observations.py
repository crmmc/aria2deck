from __future__ import annotations

from ..application.downloads.models import DownloadObservation


class InMemoryDownloadObservations:
    """由生命周期轮询/监听写入；重启后为空，由下一轮轮询重建。"""

    def __init__(self) -> None:
        self._items: dict[str, DownloadObservation] = {}

    def put(self, observation: DownloadObservation) -> None:
        self._items[observation.gid] = observation

    def get(self, gid: str) -> DownloadObservation | None:
        return self._items.get(gid)

    def discard(self, gid: str) -> None:
        self._items.pop(gid, None)
