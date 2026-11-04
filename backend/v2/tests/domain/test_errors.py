from __future__ import annotations

import pytest

from aria2deck.domain.errors import DomainError, ErrorCode


def test_domain_error_has_stable_machine_code_and_chinese_message() -> None:
    error = DomainError(ErrorCode.QUOTA_EXCEEDED, "用户配额不足")

    assert error.code is ErrorCode.QUOTA_EXCEEDED
    assert error.code.value == "quota_exceeded"
    assert str(error) == "用户配额不足"


@pytest.mark.parametrize("code", list(ErrorCode))
def test_every_error_code_is_stable_and_explicit(code: ErrorCode) -> None:
    assert code.value == code.value.lower()
    assert code.value
