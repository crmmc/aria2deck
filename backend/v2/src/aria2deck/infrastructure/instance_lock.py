from __future__ import annotations

import fcntl
import os
from pathlib import Path

from ..domain.errors import DomainError, ErrorCode


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._descriptor: int | None = None

    def acquire(self) -> None:
        if self._descriptor is not None:
            return
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as error:
            os.close(descriptor)
            if isinstance(error, BlockingIOError):
                raise DomainError(ErrorCode.INVALID_STATE, "另一个 V2 实例正在运行") from None
            raise
        self._descriptor = descriptor

    def release(self) -> None:
        if self._descriptor is None:
            return
        descriptor = self._descriptor
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
