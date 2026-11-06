from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .downloads import AttemptStatus
from .errors import DomainError, ErrorCode
from .packs import PackStatus


@dataclass(frozen=True, slots=True)
class Quota:
    quota_bytes: int
    used_bytes: int
    reserved_bytes: int

    def __post_init__(self) -> None:
        if min(self.quota_bytes, self.used_bytes, self.reserved_bytes) < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "配额账目不能为负数")
        if self.used_bytes + self.reserved_bytes > self.quota_bytes:
            raise DomainError(ErrorCode.QUOTA_EXCEEDED, "配额账目已超过用户上限")


def available_user_bytes(account: Quota) -> int:
    return account.quota_bytes - account.used_bytes - account.reserved_bytes


def visible_available_bytes(account: Quota, *, machine_available_bytes: int) -> int:
    if machine_available_bytes < 0:
        raise DomainError(ErrorCode.INVALID_INPUT, "机器可用空间不能为负数")
    return min(available_user_bytes(account), machine_available_bytes)


@dataclass(frozen=True, slots=True)
class VisibleSpace:
    """用户可见磁盘口径（旧版 STAT-01 同口径）：机器余量不足时收紧总量并置 limited。"""

    total_bytes: int
    used_bytes: int
    reserved_bytes: int
    available_bytes: int
    limited: bool


def visible_space(account: Quota, *, machine_available_bytes: int) -> VisibleSpace:
    if machine_available_bytes < 0:
        raise DomainError(ErrorCode.INVALID_INPUT, "机器可用空间不能为负数")
    headroom = machine_available_bytes
    quota_available = available_user_bytes(account)
    limited = headroom < quota_available
    available = min(quota_available, headroom)
    total = (
        account.used_bytes + account.reserved_bytes + available
        if limited
        else account.quota_bytes
    )
    return VisibleSpace(
        total_bytes=total,
        used_bytes=account.used_bytes,
        reserved_bytes=account.reserved_bytes,
        available_bytes=available,
        limited=limited,
    )


def reserve(account: Quota, amount: int, *, machine_available_bytes: int) -> Quota:
    if amount < 0 or machine_available_bytes < 0:
        raise DomainError(ErrorCode.INVALID_INPUT, "预留空间和机器余量不能为负数")
    if amount > available_user_bytes(account):
        raise DomainError(ErrorCode.QUOTA_EXCEEDED, "用户配额不足")
    if amount > machine_available_bytes:
        raise DomainError(ErrorCode.PHYSICAL_SPACE_EXCEEDED, "机器可用空间不足")
    return Quota(account.quota_bytes, account.used_bytes, account.reserved_bytes + amount)


def release_reservation(account: Quota, amount: int) -> Quota:
    if amount < 0 or amount > account.reserved_bytes:
        raise DomainError(ErrorCode.INVALID_STATE, "释放的预留空间超过实际账目")
    return Quota(account.quota_bytes, account.used_bytes, account.reserved_bytes - amount)


def complete_download(
    account: Quota,
    reserved_bytes: int,
    *,
    content_size: int,
    already_owned: bool,
) -> Quota:
    if content_size < 0:
        raise DomainError(ErrorCode.INVALID_INPUT, "内容大小不能为负数")
    without_reservation = release_reservation(account, reserved_bytes)
    used = without_reservation.used_bytes + (0 if already_owned else content_size)
    return Quota(without_reservation.quota_bytes, used, without_reservation.reserved_bytes)


def rebuild_quota(
    quota_bytes: int,
    file_sizes: Iterable[int],
    download_reservations: Iterable[int],
    pack_reservations: Iterable[int],
) -> Quota:
    files = tuple(file_sizes)
    downloads = tuple(download_reservations)
    packs = tuple(pack_reservations)
    values = [*files, *downloads, *packs]
    if any(value < 0 for value in values):
        raise DomainError(ErrorCode.INVALID_INPUT, "权威账目不能包含负数")
    used = sum(files)
    reserved = sum(downloads) + sum(packs)
    return Quota(quota_bytes, used, reserved)


@dataclass(frozen=True, slots=True)
class PhysicalDownload:
    status: AttemptStatus
    total_length: int | None
    completed_length: int

    @property
    def reserved_bytes(self) -> int:
        if self.total_length is not None and self.total_length < 0 or self.completed_length < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "物理下载账目不能为负数")
        if self.total_length is not None and self.completed_length > self.total_length:
            raise DomainError(ErrorCode.INVALID_INPUT, "完成大小不能超过总大小")
        if self.status not in {AttemptStatus.QUEUED, AttemptStatus.ACTIVE}:
            return 0
        if self.total_length is None:
            return 0
        return self.total_length - self.completed_length


@dataclass(frozen=True, slots=True)
class PhysicalPack:
    status: PackStatus
    output_reservation: int
    prepared_bytes: int

    @property
    def reserved_bytes(self) -> int:
        if self.output_reservation < 0 or self.prepared_bytes < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "物理打包账目不能为负数")
        if self.status not in {PackStatus.PENDING, PackStatus.PACKING}:
            return 0
        return max(self.output_reservation - self.prepared_bytes, 0)


def available_machine_bytes(
    disk_free_bytes: int,
    min_free_bytes: int,
    downloads: Iterable[PhysicalDownload],
    packs: Iterable[PhysicalPack],
) -> int:
    if disk_free_bytes < 0 or min_free_bytes < 0:
        raise DomainError(ErrorCode.INVALID_INPUT, "磁盘空间不能为负数")
    committed = sum(item.reserved_bytes for item in downloads) + sum(
        item.reserved_bytes for item in packs
    )
    return max(disk_free_bytes - min_free_bytes - committed, 0)
