from __future__ import annotations

import pytest

from app.domain.file_name_match import rank_file_name


@pytest.mark.parametrize(
    ("keyword", "name", "expected"),
    [
        # Queen 三档：前缀 / 连续包含 / 子序列，期望写死
        ("Queen", "Queen.zip", 0),
        ("Queen", "xxQueen.zip", 1),
        ("Queen", "Q_u_e_e_n.zip", 2),
        # 连续包含不得落到子序列档（非 Queen 例子再钉一次）
        ("abc", "xxabcxx", 1),
        # casefold：大小写不敏感
        ("QUEEN", "queen.zip", 0),
        ("queen", "XXQUEEN.ZIP", 1),
        # 中文：调用方已 trim，函数内无需处理空白
        (" 女王 ".strip(), "女王写真.zip", 0),
        ("女王", "我的女王合集.zip", 1),
        ("女王", "女-王.zip", 2),
        # 未命中
        ("Queen", "readme.txt", None),
        ("abc", "xyz", None),
        # 空关键词钉死为 None
        ("", "Queen.zip", None),
        # 关键词比名称长必然未命中
        ("QueenZip", "Queen", None),
    ],
)
def test_rank_file_name(keyword: str, name: str, expected: int | None) -> None:
    assert rank_file_name(keyword, name) == expected
