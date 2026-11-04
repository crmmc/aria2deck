from __future__ import annotations

import base64

from aria2deck.application.downloads.models import (
    BackendSubmissionResult,
    Submission,
)


def bencode(value: object) -> bytes:
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, str):
        return bencode(value.encode())
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, tuple):
        return bencode(list(value))
    if isinstance(value, dict):
        items = sorted((key.encode() if isinstance(key, str) else key, item) for key, item in value.items())
        return b"d" + b"".join(bencode(key) + bencode(item) for key, item in items) + b"e"
    raise TypeError(type(value))


def make_torrent(
    files: list[tuple[str, int]] | None = None,
    *,
    name: str = "bundle",
    webseed: bool = False,
) -> str:
    if files is None:
        files = [("a.bin", 3)]
    info: dict[str, object] = {"name": name}
    if len(files) == 1:
        info["length"] = files[0][1]
    else:
        info["files"] = [
            {"length": size, "path": path.split("/")} for path, size in files
        ]
    root: dict[str, object] = {"info": info}
    if webseed:
        root["url-list"] = ["https://example.com/webseed"]
    return base64.b64encode(bencode(root)).decode()


class FakeSubmissionBackend:
    def __init__(self) -> None:
        self.submit_calls: list[list[Submission]] = []
        self.confirm_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.replies: list[BackendSubmissionResult] | None = None
        self.reject_indexes: set[int] = set()
        self.submit_error: Exception | None = None
        self.confirm_error: Exception | None = None
        self.stop_error: Exception | None = None
        self.confirm_results: dict[str, bool] = {}
        self.confirm_default = True
        self.mark_confirmed_on_submit = False

    async def submit_many(
        self, submissions: list[Submission], *, trackers: tuple[str, ...]
    ) -> list[BackendSubmissionResult]:
        self.submit_calls.append(submissions)
        if self.mark_confirmed_on_submit:
            for item in submissions:
                self.confirm_results[item.gid] = True
        if self.submit_error is not None:
            raise self.submit_error
        if self.replies is not None:
            return self.replies
        return [
            BackendSubmissionResult(False, error_code="aria2_1")
            if index in self.reject_indexes
            else BackendSubmissionResult(True, gid=item.gid)
            for index, item in enumerate(submissions)
        ]

    async def confirm(self, *, gid: str) -> bool:
        self.confirm_calls.append(gid)
        if self.confirm_error is not None:
            raise self.confirm_error
        return self.confirm_results.get(gid, self.confirm_default)

    async def stop(self, *, gid: str) -> None:
        self.stop_calls.append(gid)
        if self.stop_error is not None:
            raise self.stop_error


class FakeTrackers:
    def __init__(self, values: tuple[str, ...] = ()) -> None:
        self.values = values

    async def current(self) -> tuple[str, ...]:
        return self.values
