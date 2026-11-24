"""Shared formatting for comparison-style error messages (M17).

All quota/disk/limit errors that carry decision inputs must build their
message text through these helpers so wording stays uniform across paths.
"""

from __future__ import annotations


def fmt_gb(n: int) -> str:
    """字节数格式化为 GB 显示（与既有 HTTP 路径格式一致）。"""
    return f"{int(n) / 1024**3:.2f} GB"


def fmt_count(n: int) -> str:
    """纯计数（连接数等非字节量）格式化。"""
    return str(int(n))


def over_limit(resource: str, actual: int, verb_limit: str, limit: int) -> str:
    """统一比较类报错格式：{资源} {实际值} GB {判定词}{阈值标签} {阈值} GB。"""
    return f"{resource} {fmt_gb(actual)} {verb_limit} {fmt_gb(limit)}"
