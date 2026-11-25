"""V1 → V2 离线迁移工具。

只在数据所在机器本地运行，不联网、不改 V1、只写全新的 V2 根目录。
"""

from __future__ import annotations


class MigrationAbort(RuntimeError):
    """迁移中止：附带失败位置与逐条明细，由 CLI 转成报告并以退出码 1 结束。"""

    def __init__(self, message: str, details: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


__all__ = ["MigrationAbort"]
