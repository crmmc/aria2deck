from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings


@dataclass
class ApplicationSingletonLease:
    path: Path
    fd: int

    @classmethod
    def acquire(cls) -> ApplicationSingletonLease:
        path = Path(f"{settings.database_path}.instance.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.fsync(fd)
        except BlockingIOError as exc:
            os.close(fd)
            raise RuntimeError("检测到另一 Aria2Deck 实例，当前部署仅支持单 worker") from exc
        except Exception:
            os.close(fd)
            raise
        return cls(path=path, fd=fd)

    def release(self) -> None:
        if self.fd < 0:
            return
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        self.fd = -1
