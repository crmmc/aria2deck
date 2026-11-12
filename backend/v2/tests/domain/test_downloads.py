from __future__ import annotations

import base64

import pytest

from aria2deck.domain.downloads import (
    AttemptStatus,
    RegistrationOutcome,
    SubscriptionStatus,
    decide_registration,
    derive_planned_gid,
    normalize_resource,
    plan_cancellation,
    reconciled_total_length,
    require_gid_match,
    require_orphan_cleanup_safe,
    require_retryable,
    torrent_resource_key,
    transition_attempt,
    transition_subscription,
)
from aria2deck.domain.errors import DomainError, ErrorCode


@pytest.mark.parametrize(
    "current,target",
    [
        (AttemptStatus.QUEUED, AttemptStatus.ACTIVE),
        (AttemptStatus.ACTIVE, AttemptStatus.PAUSED),
        (AttemptStatus.PAUSED, AttemptStatus.WAITING),
        (AttemptStatus.WAITING, AttemptStatus.QUEUED),
        (AttemptStatus.QUEUED, AttemptStatus.COMPLETED),
        (AttemptStatus.ACTIVE, AttemptStatus.COMPLETED),
        (AttemptStatus.WAITING, AttemptStatus.COMPLETED),
        (AttemptStatus.PAUSED, AttemptStatus.COMPLETED),
        (AttemptStatus.ACTIVE, AttemptStatus.FAILED),
        (AttemptStatus.QUEUED, AttemptStatus.CANCELLED),
    ],
)
def test_attempt_state_machine_allows_lifecycleTransitions(
    current: AttemptStatus, target: AttemptStatus
) -> None:
    assert transition_attempt(current, target) is target


@pytest.mark.parametrize("status", list(AttemptStatus))
def test_duplicate_attempt_event_is_idempotent(status: AttemptStatus) -> None:
    assert transition_attempt(status, status) is status


@pytest.mark.parametrize(
    "current,target",
    [
        (AttemptStatus.COMPLETED, AttemptStatus.ACTIVE),
        (AttemptStatus.FAILED, AttemptStatus.QUEUED),
        (AttemptStatus.CANCELLED, AttemptStatus.ACTIVE),
    ],
)
def test_attempt_state_machine_rejects_invalid_or_terminal_transition(
    current: AttemptStatus, target: AttemptStatus
) -> None:
    with pytest.raises(DomainError) as caught:
        transition_attempt(current, target)
    assert caught.value.code is ErrorCode.INVALID_STATE


@pytest.mark.parametrize(
    "current,target",
    [
        (SubscriptionStatus.QUEUED, SubscriptionStatus.ACTIVE),
        (SubscriptionStatus.ACTIVE, SubscriptionStatus.COMPLETED),
        (SubscriptionStatus.WAITING, SubscriptionStatus.COMPLETED),
        (SubscriptionStatus.PAUSED, SubscriptionStatus.COMPLETED),
        (SubscriptionStatus.FAILED, SubscriptionStatus.EXPIRED),
        (SubscriptionStatus.CANCELLED, SubscriptionStatus.EXPIRED),
        (SubscriptionStatus.COMPLETED, SubscriptionStatus.EXPIRED),
        (SubscriptionStatus.PAUSED, SubscriptionStatus.CANCELLED),
    ],
)
def test_subscription_state_machine_has_independent_history_state(
    current: SubscriptionStatus, target: SubscriptionStatus
) -> None:
    assert transition_subscription(current, target) is target


@pytest.mark.parametrize("status", list(SubscriptionStatus))
def test_duplicate_subscription_event_is_idempotent(status: SubscriptionStatus) -> None:
    assert transition_subscription(status, status) is status


@pytest.mark.parametrize(
    "current,target",
    [
        (SubscriptionStatus.EXPIRED, SubscriptionStatus.ACTIVE),
        (SubscriptionStatus.COMPLETED, SubscriptionStatus.FAILED),
        (SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRED),
    ],
)
def test_subscription_state_machine_rejects_invalid_transition(
    current: SubscriptionStatus, target: SubscriptionStatus
) -> None:
    with pytest.raises(DomainError):
        transition_subscription(current, target)


@pytest.mark.parametrize(
    "terminal",
    [AttemptStatus.COMPLETED, AttemptStatus.FAILED, AttemptStatus.CANCELLED],
)
@pytest.mark.parametrize(
    "target",
    [AttemptStatus.QUEUED, AttemptStatus.ACTIVE, AttemptStatus.WAITING, AttemptStatus.PAUSED],
)
def test_terminal_attempt_cannot_be_reactivated(
    terminal: AttemptStatus, target: AttemptStatus
) -> None:
    with pytest.raises(DomainError) as caught:
        transition_attempt(terminal, target)
    assert caught.value.code is ErrorCode.INVALID_STATE


@pytest.mark.parametrize(
    "terminal",
    [
        SubscriptionStatus.COMPLETED,
        SubscriptionStatus.FAILED,
        SubscriptionStatus.CANCELLED,
        SubscriptionStatus.EXPIRED,
    ],
)
@pytest.mark.parametrize(
    "target",
    [
        SubscriptionStatus.QUEUED,
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.WAITING,
        SubscriptionStatus.PAUSED,
    ],
)
def test_terminal_subscription_cannot_be_reactivated(
    terminal: SubscriptionStatus, target: SubscriptionStatus
) -> None:
    with pytest.raises(DomainError) as caught:
        transition_subscription(terminal, target)
    assert caught.value.code is ErrorCode.INVALID_STATE


@pytest.mark.parametrize(
    "status,allowed",
    [
        (SubscriptionStatus.FAILED, True),
        (SubscriptionStatus.CANCELLED, True),
        (SubscriptionStatus.COMPLETED, False),
        (SubscriptionStatus.EXPIRED, False),
        (SubscriptionStatus.ACTIVE, False),
    ],
)
def test_retry_only_creates_a_new_attempt_for_failed_or_cancelled_history(
    status: SubscriptionStatus, allowed: bool
) -> None:
    if allowed:
        require_retryable(status)
    else:
        with pytest.raises(DomainError) as caught:
            require_retryable(status)
        assert caught.value.code is ErrorCode.INVALID_STATE


@pytest.mark.parametrize(
    "attempt,subscription,content,expected,submit",
    [
        (None, None, False, RegistrationOutcome.CREATED, True),
        (AttemptStatus.ACTIVE, None, False, RegistrationOutcome.JOINED_LIVE, False),
        (None, None, True, RegistrationOutcome.ATTACHED_COMPLETED, False),
        (AttemptStatus.ACTIVE, SubscriptionStatus.QUEUED, False, RegistrationOutcome.DUPLICATE, False),
        (AttemptStatus.COMPLETED, SubscriptionStatus.COMPLETED, False, RegistrationOutcome.DUPLICATE, False),
        (AttemptStatus.FAILED, SubscriptionStatus.FAILED, False, RegistrationOutcome.CREATED, True),
        (AttemptStatus.ACTIVE, SubscriptionStatus.CANCELLED, False, RegistrationOutcome.JOINED_LIVE, False),
        (AttemptStatus.COMPLETED, None, False, RegistrationOutcome.CREATED, True),
        (AttemptStatus.FAILED, None, False, RegistrationOutcome.CREATED, True),
        (AttemptStatus.CANCELLED, None, False, RegistrationOutcome.CREATED, True),
        (AttemptStatus.COMPLETED, None, True, RegistrationOutcome.ATTACHED_COMPLETED, False),
    ],
)
def test_registration_owns_dedup_and_idempotency_decision(
    attempt: AttemptStatus | None,
    subscription: SubscriptionStatus | None,
    content: bool,
    expected: RegistrationOutcome,
    submit: bool,
) -> None:
    decision = decide_registration(
        attempt_status=attempt,
        subscription_status=subscription,
        content_available=content,
    )
    assert decision.outcome is expected
    assert decision.should_submit is submit


def test_cancellation_releases_only_the_current_user_and_stops_last_attempt() -> None:
    shared = plan_cancellation(
        SubscriptionStatus.ACTIVE,
        remaining_subscriptions=1,
        reserved_bytes=50,
    )
    last = plan_cancellation(
        SubscriptionStatus.PAUSED,
        remaining_subscriptions=0,
        reserved_bytes=25,
    )

    assert shared.release_reserved == 50
    assert shared.stop_attempt is False
    assert shared.attempt_status is None
    assert last.release_reserved == 25
    assert last.stop_attempt is True
    assert last.attempt_status is AttemptStatus.CANCELLED


@pytest.mark.parametrize("remaining", [-1])
def test_cancellation_rejects_impossible_subscriber_count(remaining: int) -> None:
    with pytest.raises(DomainError) as caught:
        plan_cancellation(
            SubscriptionStatus.ACTIVE,
            remaining_subscriptions=remaining,
            reserved_bytes=0,
        )
    assert caught.value.code is ErrorCode.INVALID_INPUT


def test_cancellation_rejects_terminal_subscription() -> None:
    with pytest.raises(DomainError) as caught:
        plan_cancellation(
            SubscriptionStatus.COMPLETED,
            remaining_subscriptions=0,
            reserved_bytes=0,
        )
    assert caught.value.code is ErrorCode.INVALID_STATE


def test_cancellation_rejects_negative_reservation_before_any_release() -> None:
    with pytest.raises(DomainError):
        plan_cancellation(
            SubscriptionStatus.ACTIVE,
            remaining_subscriptions=0,
            reserved_bytes=-1,
        )


@pytest.mark.parametrize(
    "uri,kind,canonical",
    [
        (" https://example.com/a.iso?token=hidden#part ", "http", "https://example.com/a.iso?token=hidden"),
        ("magnet:?xt=urn:btih:" + "A" * 40, "magnet", "magnet:?xt=urn:btih:" + "a" * 40),
    ],
)
def test_resource_normalization_is_deterministic_and_display_is_sanitized(
    uri: str, kind: str, canonical: str
) -> None:
    resource = normalize_resource(uri)
    assert resource.kind == kind
    assert resource.canonical_uri == canonical
    assert resource.resource_key.startswith(f"{kind}:")
    assert "token=hidden" not in resource.display_uri
    if kind == "http":
        assert resource.display_uri == "https://example.com/a.iso"


def test_base32_magnet_is_normalized_to_the_same_info_hash() -> None:
    raw = bytes(range(20))
    hex_magnet = normalize_resource("magnet:?xt=urn:btih:" + raw.hex())
    base32_magnet = normalize_resource(
        "magnet:?xt=urn:btih:" + base64.b32encode(raw).decode("ascii")
    )
    assert base32_magnet.resource_key == hex_magnet.resource_key


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "ftp://example.com/file",
        "file:///tmp/file",
        "https:///missing-host",
        "magnet:?xt=urn:btih:not-a-hash",
        "magnet:?dn=no-info-hash",
        "http://example.com/\nheaders",
    ],
)
def test_resource_normalization_rejects_unsupported_or_ambiguous_input(uri: str) -> None:
    with pytest.raises(DomainError) as caught:
        normalize_resource(uri)
    assert caught.value.code is ErrorCode.INVALID_INPUT


def test_torrent_selection_changes_identity_and_rejects_bad_selection() -> None:
    info_hash = "ab" * 20
    all_files = torrent_resource_key(info_hash, None)
    selected = torrent_resource_key(info_hash, [3, 1, 2])
    assert selected == torrent_resource_key(info_hash.upper(), [1, 2, 3])
    assert selected != all_files
    for indexes in ([], [1, 1], [-1]):
        with pytest.raises(DomainError):
            torrent_resource_key(info_hash, indexes)
    with pytest.raises(DomainError):
        torrent_resource_key("bad", None)


def test_planned_gid_is_stable_but_attempt_and_pepper_scoped() -> None:
    first = derive_planned_gid(1, pepper="test-pepper")
    assert first == derive_planned_gid(1, pepper="test-pepper")
    assert len(first) == 16
    assert first != derive_planned_gid(2, pepper="test-pepper")
    assert first != derive_planned_gid(1, pepper="other-test-pepper")
    with pytest.raises(DomainError):
        derive_planned_gid(0, pepper="test-pepper")
    with pytest.raises(DomainError):
        derive_planned_gid(1, pepper="")


def test_gid_fencing_rejects_an_old_attempt_event() -> None:
    require_gid_match(1, expected="abc", observed="abc")
    with pytest.raises(DomainError) as caught:
        require_gid_match(1, expected="abc", observed="old")
    assert caught.value.code is ErrorCode.STALE_EVENT


def test_unknown_recovery_candidates_block_destructive_cleanup() -> None:
    require_orphan_cleanup_safe(candidates_complete=True)
    with pytest.raises(DomainError) as caught:
        require_orphan_cleanup_safe(candidates_complete=False)
    assert caught.value.code is ErrorCode.RECOVERY_UNSAFE


@pytest.mark.parametrize(
    "current,observed,expected",
    [(None, None, None), (None, 10, 10), (10, 8, 10), (10, 12, 12), (10, None, 10)],
)
def test_known_size_never_shrinks(
    current: int | None, observed: int | None, expected: int | None
) -> None:
    assert reconciled_total_length(current, observed) == expected


@pytest.mark.parametrize("current,observed", [(-1, None), (None, -1)])
def test_size_reconciliation_rejects_negative_values(
    current: int | None, observed: int | None
) -> None:
    with pytest.raises(DomainError):
        reconciled_total_length(current, observed)


def test_http_port_is_validated_and_kept_in_safe_display() -> None:
    resource = normalize_resource("https://example.com:8443/a")
    assert resource.display_uri == "https://example.com:8443/a"
    with pytest.raises(DomainError):
        normalize_resource("https://example.com:not-a-port/a")


def test_torrent_selection_must_be_an_index_sequence() -> None:
    with pytest.raises(DomainError):
        torrent_resource_key("ab" * 20, "1,2")


def test_base32_alphabet_is_validated_by_the_decoder() -> None:
    with pytest.raises(DomainError) as caught:
        normalize_resource("magnet:?xt=urn:btih:" + "0" * 32)
    assert caught.value.code is ErrorCode.INVALID_INPUT


def test_malformed_network_location_is_a_stable_error() -> None:
    with pytest.raises(DomainError) as caught:
        normalize_resource("http://[broken")
    assert caught.value.code is ErrorCode.INVALID_INPUT


def test_ipv6_display_keeps_address_brackets() -> None:
    assert normalize_resource("http://[::1]:8080/a").display_uri == "http://[::1]:8080/a"
    assert normalize_resource("http://[::1]/a").display_uri == "http://[::1]/a"


# --- 回归（D1/D7）：用户级命名分配纯函数（修复前缺失，import 失败即为目标失败） ---


def test_allocate_download_display_name_suffix_matrix() -> None:
    from aria2deck.domain.downloads import allocate_download_display_name

    cases = [
        # (请求名, 已占用集合, 允许改名, 期望结果)
        ("ubuntu.iso", {"ubuntu.iso"}, True, "ubuntu (2).iso"),
        ("ubuntu.iso", {"ubuntu.iso", "ubuntu (2).iso"}, True, "ubuntu (3).iso"),
        ("pack.tar.zst", {"pack.tar.zst"}, True, "pack (2).tar.zst"),
        ("download", {"download"}, True, "download (2)"),
        ("ubuntu (2).iso", {"ubuntu (2).iso"}, True, "ubuntu (3).iso"),
        (".env", {".env"}, True, ".env (2)"),
        ("data.2024.csv", {"data.2024.csv"}, True, "data (2).2024.csv"),
        ("free.bin", set(), True, "free.bin"),
        ("free.bin", {"other.bin"}, True, "free.bin"),
    ]
    for requested, occupied, allow_rename, expected in cases:
        assert (
            allocate_download_display_name(requested, occupied, allow_rename) == expected
        ), f"requested={requested!r} occupied={occupied!r}"


def test_allocate_download_display_name_strict_conflict_and_exhaustion() -> None:
    from aria2deck.domain.downloads import allocate_download_display_name

    with pytest.raises(DomainError) as caught:
        allocate_download_display_name("busy.bin", {"busy.bin"}, False)
    assert caught.value.code is ErrorCode.DUPLICATE_RESOURCE
    assert "同名" in str(caught.value)

    occupied = {"f.bin"} | {f"f ({index}).bin" for index in range(2, 10_002)}
    with pytest.raises(DomainError) as caught:
        allocate_download_display_name("f.bin", occupied, True, max_attempts=10_000)
    assert caught.value.code is ErrorCode.DUPLICATE_RESOURCE


def test_allocate_download_display_name_keeps_200_character_limit() -> None:
    from aria2deck.domain.downloads import allocate_download_display_name

    long_name = "长" * 100 + "a" * 96 + ".iso"
    assert len(long_name) == 200
    allocated = allocate_download_display_name(long_name, {long_name}, True)
    assert allocated != long_name
    assert len(allocated) <= 200
    assert allocated.endswith(".iso")
