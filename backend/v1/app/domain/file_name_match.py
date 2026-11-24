from __future__ import annotations

PREFIX_RANK = 0
CONTAINS_RANK = 1
SUBSEQUENCE_RANK = 2


def rank_file_name(keyword: str, name: str) -> int | None:
    """按相关度对文件名打分：0 前缀、1 连续包含、2 子序列，未命中返回 None。

    调用方负责 trim；函数内对 keyword 与 name 做 casefold。
    """
    key = keyword.casefold()
    target = name.casefold()
    if not key:
        return None
    if target.startswith(key):
        return PREFIX_RANK
    if key in target:
        return CONTAINS_RANK
    cursor = iter(target)
    if all(char in cursor for char in key):
        return SUBSEQUENCE_RANK
    return None
