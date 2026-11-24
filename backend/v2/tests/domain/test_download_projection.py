from __future__ import annotations

import pytest

from aria2deck.domain.downloads import (
    AttemptStatus,
    SubscriptionStatus,
    can_retry,
    effective_task_status,
    task_status_label,
)


@pytest.mark.parametrize(
    "subscription,attempt,expected",
    [
        (SubscriptionStatus.ACTIVE, AttemptStatus.ACTIVE, SubscriptionStatus.ACTIVE),
        (SubscriptionStatus.QUEUED, AttemptStatus.ACTIVE, SubscriptionStatus.QUEUED),
        (SubscriptionStatus.CANCELLED, AttemptStatus.ACTIVE, SubscriptionStatus.CANCELLED),
        (SubscriptionStatus.COMPLETED, AttemptStatus.FAILED, SubscriptionStatus.COMPLETED),
        (SubscriptionStatus.ACTIVE, AttemptStatus.COMPLETED, SubscriptionStatus.COMPLETED),
        (SubscriptionStatus.WAITING, AttemptStatus.FAILED, SubscriptionStatus.FAILED),
        (SubscriptionStatus.PAUSED, AttemptStatus.CANCELLED, SubscriptionStatus.CANCELLED),
        (SubscriptionStatus.EXPIRED, AttemptStatus.COMPLETED, SubscriptionStatus.EXPIRED),
    ],
)
def test_effective_status_uses_user_terminal_then_attempt_terminal(
    subscription: SubscriptionStatus,
    attempt: AttemptStatus,
    expected: SubscriptionStatus,
) -> None:
    assert effective_task_status(subscription, attempt) is expected


@pytest.mark.parametrize(
    "status,label",
    [
        (SubscriptionStatus.QUEUED, "排队中"),
        (SubscriptionStatus.ACTIVE, "下载中"),
        (SubscriptionStatus.WAITING, "等待中"),
        (SubscriptionStatus.PAUSED, "已暂停"),
        (SubscriptionStatus.COMPLETED, "已完成"),
        (SubscriptionStatus.FAILED, "已失败"),
        (SubscriptionStatus.CANCELLED, "已取消"),
        (SubscriptionStatus.EXPIRED, "已过期"),
    ],
)
def test_task_status_labels_cover_every_public_state(
    status: SubscriptionStatus, label: str
) -> None:
    assert task_status_label(status) == label


def test_ingestion_label_is_only_used_for_an_active_database_task() -> None:
    assert task_status_label(SubscriptionStatus.ACTIVE, ingestion_pending=True) == "入库中"
    assert task_status_label(SubscriptionStatus.COMPLETED, ingestion_pending=True) == "已完成"


@pytest.mark.parametrize(
    "status,expected",
    [
        (SubscriptionStatus.FAILED, True),
        (SubscriptionStatus.CANCELLED, True),
        (SubscriptionStatus.ACTIVE, False),
        (SubscriptionStatus.COMPLETED, False),
        (SubscriptionStatus.EXPIRED, False),
    ],
)
def test_retryability_is_derived_from_the_effective_user_status(
    status: SubscriptionStatus, expected: bool
) -> None:
    assert can_retry(status) is expected
