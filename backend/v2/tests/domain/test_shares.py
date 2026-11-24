from __future__ import annotations

import pytest

from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.shares import (
    Share,
    ShareStatus,
    decide_download,
    ensure_share_capacity,
    hash_share_password,
    plan_share_download,
    require_password,
    share_status,
    validate_share_options,
    verify_share_password,
)


def test_share_status_distinguishes_revoked_expired_exhausted_and_file_delete() -> None:
    active = Share(expires_at_ms=2000, revoked_at_ms=None, max_downloads=2, download_count=0)
    assert share_status(active, 1000) is ShareStatus.ACTIVE
    assert share_status(active, 2000) is ShareStatus.EXPIRED
    assert share_status(Share(None, 1000, None, 0), 1) is ShareStatus.REVOKED
    assert share_status(Share(None, None, 2, 2), 1) is ShareStatus.EXHAUSTED
    assert share_status(Share(None, None, None, 0, file_pending_delete=True), 1) is ShareStatus.UNAVAILABLE


def test_permanent_share_can_have_no_expiry_or_download_limit() -> None:
    validate_share_options(expires_in_seconds=None, max_downloads=None, password=None)
    assert share_status(Share(None, None, None, 10000), 10**15) is ShareStatus.ACTIVE


@pytest.mark.parametrize("seconds", [0, -1, 30 * 24 * 3600 + 1])
def test_share_expiry_is_bounded(seconds: int) -> None:
    with pytest.raises(DomainError):
        validate_share_options(seconds, 1, None)


@pytest.mark.parametrize("downloads", [0, -1, 10001])
def test_share_download_limit_is_bounded(downloads: int) -> None:
    with pytest.raises(DomainError):
        validate_share_options(60, downloads, None)


def test_share_password_is_hashed_and_compared_without_storing_plaintext() -> None:
    digest = hash_share_password("code", "secret")
    assert digest != "secret"
    assert verify_share_password("code", "secret", digest)
    assert not verify_share_password("code", "wrong", digest)
    require_password(digest, "code", "secret")
    with pytest.raises(DomainError) as caught:
        require_password(digest, "code", None)
    assert caught.value.code is ErrorCode.MISSING_CREDENTIALS
    with pytest.raises(DomainError) as caught:
        require_password(digest, "code", "wrong")
    assert caught.value.code is ErrorCode.INVALID_CREDENTIALS
    with pytest.raises(DomainError):
        require_password(None, "code", "anything")


def test_share_password_length_is_bounded() -> None:
    validate_share_options(60, 1, "x" * 100)
    with pytest.raises(DomainError):
        validate_share_options(60, 1, "x" * 101)
    with pytest.raises(DomainError):
        validate_share_options(60, 1, "")


def test_share_capacity_has_a_fixed_per_file_limit() -> None:
    ensure_share_capacity(9)
    with pytest.raises(DomainError) as caught:
        ensure_share_capacity(10)
    assert caught.value.code is ErrorCode.SHARE_LIMIT_EXCEEDED
    with pytest.raises(DomainError):
        ensure_share_capacity(-1)


@pytest.mark.parametrize(
    "header,start,end,status,count",
    [
        (None, 0, 99, 200, True),
        ("bytes=0-99", 0, 99, 206, True),
        ("bytes=0-49", 0, 49, 206, False),
        ("bytes=50-", 50, 99, 206, False),
        ("bytes=-100", 0, 99, 206, True),
        ("bytes=-20", 80, 99, 206, False),
        ("bytes=90-200", 90, 99, 206, False),
        ("items=0-10", 0, 99, 200, True),
        ("bytes=0-10,20-30", 0, 99, 200, True),
    ],
)
def test_range_counting_only_charges_for_the_complete_entity(
    header: str | None, start: int, end: int, status: int, count: bool
) -> None:
    decision = decide_download(100, header)
    assert (decision.start, decision.end) == (start, end)
    assert decision.status_code == status
    assert decision.count_download is count
    assert decision.content_length == end - start + 1


@pytest.mark.parametrize("header", ["bytes=", "bytes=a-b", "bytes=-0", "bytes=100-", "bytes=20-10", "bytes=0-1,,"])
def test_invalid_range_never_consumes_a_download(header: str) -> None:
    with pytest.raises(DomainError) as caught:
        decide_download(100, header)
    assert caught.value.code is ErrorCode.RANGE_NOT_SATISFIABLE


def test_empty_file_rejects_any_range_but_allows_full_download() -> None:
    assert decide_download(0, None).content_length == 0
    with pytest.raises(DomainError):
        decide_download(0, "bytes=0-")


def test_share_download_checks_availability_before_range_or_count() -> None:
    expired = Share(expires_at_ms=10, revoked_at_ms=None, max_downloads=1, download_count=0)
    with pytest.raises(DomainError) as caught:
        plan_share_download(expired, now_ms=10, size=100, range_header="bytes=bad")
    assert caught.value.code is ErrorCode.SHARE_EXPIRED
    available = Share(None, None, 1, 0)
    assert plan_share_download(available, now_ms=0, size=100, range_header=None).count_download


def test_negative_sizes_and_share_counts_are_rejected() -> None:
    with pytest.raises(DomainError):
        decide_download(-1, None)
    with pytest.raises(DomainError):
        share_status(Share(None, None, 1, -1), 0)


def test_non_numeric_range_end_is_rejected() -> None:
    with pytest.raises(DomainError) as caught:
        decide_download(100, "bytes=0-end")
    assert caught.value.code is ErrorCode.RANGE_NOT_SATISFIABLE
