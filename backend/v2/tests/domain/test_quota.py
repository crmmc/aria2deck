from __future__ import annotations

import pytest

from aria2deck.domain.downloads import AttemptStatus
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.packs import PackStatus
from aria2deck.domain.quota import (
    PhysicalDownload,
    PhysicalPack,
    Quota,
    available_machine_bytes,
    available_user_bytes,
    complete_download,
    rebuild_quota,
    release_reservation,
    reserve,
    visible_available_bytes,
)


def test_user_and_machine_budgets_are_independent() -> None:
    account = Quota(quota_bytes=100, used_bytes=20, reserved_bytes=30)
    assert available_user_bytes(account) == 50
    assert visible_available_bytes(account, machine_available_bytes=20) == 20
    assert reserve(account, 20, machine_available_bytes=20).reserved_bytes == 50
    with pytest.raises(DomainError) as caught:
        reserve(account, 21, machine_available_bytes=20)
    assert caught.value.code is ErrorCode.PHYSICAL_SPACE_EXCEEDED
    with pytest.raises(DomainError) as caught:
        reserve(account, 51, machine_available_bytes=1000)
    assert caught.value.code is ErrorCode.QUOTA_EXCEEDED


def test_zero_quota_and_exact_fit_are_explicit() -> None:
    zero = Quota(quota_bytes=0, used_bytes=0, reserved_bytes=0)
    assert reserve(zero, 0, machine_available_bytes=0) == zero
    with pytest.raises(DomainError):
        reserve(zero, 1, machine_available_bytes=1)
    exact = reserve(Quota(100, 90, 0), 10, machine_available_bytes=10)
    assert available_user_bytes(exact) == 0


@pytest.mark.parametrize(
    "values",
    [(-1, 0, 0), (100, -1, 0), (100, 0, -1), (10, 8, 3)],
)
def test_quota_account_rejects_invalid_or_drifted_values(values: tuple[int, int, int]) -> None:
    with pytest.raises(DomainError):
        Quota(*values)


def test_release_and_completion_move_the_same_reservation_exactly_once() -> None:
    account = Quota(quota_bytes=100, used_bytes=10, reserved_bytes=40)
    assert release_reservation(account, 15) == Quota(100, 10, 25)
    assert complete_download(account, 40, content_size=30, already_owned=False) == Quota(100, 40, 0)
    assert complete_download(account, 40, content_size=30, already_owned=True) == Quota(100, 10, 0)
    with pytest.raises(DomainError):
        release_reservation(account, 41)
    with pytest.raises(DomainError):
        complete_download(account, 40, content_size=100, already_owned=False)


@pytest.mark.parametrize("amount", [-1, 51])
def test_completion_rejects_invalid_reserved_amount(amount: int) -> None:
    with pytest.raises(DomainError):
        complete_download(Quota(100, 0, 50), amount, content_size=1, already_owned=False)


def test_completion_rejects_negative_content_size() -> None:
    with pytest.raises(DomainError):
        complete_download(Quota(100, 0, 10), 5, content_size=-1, already_owned=False)


def test_rebuild_uses_authoritative_files_and_active_reservations() -> None:
    assert rebuild_quota(100, [10, 20], [3, 4], [5]) == Quota(100, 30, 12)
    with pytest.raises(DomainError):
        rebuild_quota(10, [8], [3], [])
    with pytest.raises(DomainError):
        rebuild_quota(100, [-1], [], [])


@pytest.mark.parametrize(
    "status,total,completed,committed,expected",
    [
        (AttemptStatus.QUEUED, 100, 20, 100, 80),
        (AttemptStatus.ACTIVE, 100, 20, 100, 80),
        (AttemptStatus.WAITING, 100, 20, 100, 0),
        (AttemptStatus.PAUSED, 100, 20, 100, 0),
        (AttemptStatus.COMPLETED, 100, 100, 100, 0),
        (AttemptStatus.ACTIVE, None, 0, 0, 0),
        (AttemptStatus.ACTIVE, None, 20, 100, 80),
        (AttemptStatus.ACTIVE, 100, 20, 120, 100),
    ],
)
def test_physical_download_budget_is_not_user_quota(
    status: AttemptStatus,
    total: int | None,
    completed: int,
    committed: int,
    expected: int,
) -> None:
    item = PhysicalDownload(
        status=status,
        total_length=total,
        completed_length=completed,
        committed_bytes=committed,
    )
    assert item.reserved_bytes == expected


def test_physical_download_rejects_invalid_progress() -> None:
    with pytest.raises(DomainError):
        _ = PhysicalDownload(AttemptStatus.ACTIVE, 10, 11).reserved_bytes
    with pytest.raises(DomainError):
        _ = PhysicalDownload(AttemptStatus.ACTIVE, -1, 0).reserved_bytes
    with pytest.raises(DomainError):
        _ = PhysicalDownload(AttemptStatus.ACTIVE, 10, 0, -1).reserved_bytes


@pytest.mark.parametrize(
    "status,reserved,prepared,expected",
    [
        (PackStatus.PENDING, 100, 0, 100),
        (PackStatus.PACKING, 100, 20, 80),
        (PackStatus.DONE, 100, 100, 0),
        (PackStatus.FAILED, 100, 0, 0),
    ],
)
def test_physical_pack_budget_tracks_only_unmaterialized_output(
    status: PackStatus, reserved: int, prepared: int, expected: int
) -> None:
    assert PhysicalPack(status, reserved, prepared).reserved_bytes == expected


def test_physical_pack_rejects_invalid_values() -> None:
    with pytest.raises(DomainError):
        _ = PhysicalPack(PackStatus.PACKING, -1, 0).reserved_bytes
    with pytest.raises(DomainError):
        _ = PhysicalPack(PackStatus.PACKING, 10, -1).reserved_bytes


def test_machine_available_never_becomes_negative() -> None:
    downloads = [PhysicalDownload(AttemptStatus.ACTIVE, 100, 25)]
    packs = [PhysicalPack(PackStatus.PENDING, 50, 0)]
    assert available_machine_bytes(1000, 100, downloads, packs) == 775
    assert available_machine_bytes(100, 100, downloads, packs) == 0
    with pytest.raises(DomainError):
        available_machine_bytes(-1, 0, [], [])
    with pytest.raises(DomainError):
        available_machine_bytes(1, -1, [], [])


def test_quota_inputs_reject_negative_reservation_and_machine_space() -> None:
    account = Quota(100, 0, 0)
    with pytest.raises(DomainError):
        reserve(account, -1, machine_available_bytes=100)
    with pytest.raises(DomainError):
        visible_available_bytes(account, machine_available_bytes=-1)
    with pytest.raises(DomainError):
        reserve(account, 0, machine_available_bytes=-1)
