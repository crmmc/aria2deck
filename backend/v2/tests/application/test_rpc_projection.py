from __future__ import annotations

import pytest

from aria2deck.application.downloads.models import ObservedFile, TaskProjection
from aria2deck.application.downloads.rpc_projection import (
    VirtualGidSpace,
    aria2_status,
    basename_of,
    failure_message,
    global_stat_payload,
    option_payload,
    slice_window,
    tell_status_payload,
    uris_payload,
    version_payload,
)
from aria2deck.domain.downloads import SubscriptionStatus

TASK = TaskProjection(
    id=7,
    status=SubscriptionStatus.ACTIVE,
    status_label="下载中",
    name="movie.mkv",
    display_uri="https://example.com/展示链接",
    total_bytes=1000,
    completed_bytes=400,
    download_speed=128,
    upload_speed=16,
    peer_count=3,
    connection_count=5,
    files=(ObservedFile("folder/movie.mkv", 1000, 400, True),),
    observation_fresh=True,
    reserved_bytes=0,
    error_code=None,
    retryable=False,
    created_at_ms=1,
    updated_at_ms=2,
    finished_at_ms=None,
)


def test_virtual_gid_is_deterministic_and_prefix_isolated() -> None:
    space_a = VirtualGidSpace()
    again = VirtualGidSpace(space_a.prefix)
    assert space_a.virtual_gid(7) == again.virtual_gid(7)
    assert len(space_a.virtual_gid(7)) == 16
    assert space_a.virtual_gid(7) != space_a.virtual_gid(8)
    space_b = VirtualGidSpace()
    assert space_a.virtual_gid(7) != space_b.virtual_gid(7), "重启前缀不同，新旧不碰撞"


@pytest.mark.parametrize(
    "status,expected",
    [
        (SubscriptionStatus.QUEUED, "queued"),
        (SubscriptionStatus.ACTIVE, "active"),
        (SubscriptionStatus.WAITING, "waiting"),
        (SubscriptionStatus.PAUSED, "paused"),
        (SubscriptionStatus.COMPLETED, "complete"),
        (SubscriptionStatus.FAILED, "error"),
        (SubscriptionStatus.CANCELLED, "removed"),
        (SubscriptionStatus.EXPIRED, "removed"),
    ],
)
def test_status_mapping_table(status: SubscriptionStatus, expected: str) -> None:
    assert aria2_status(status) == expected


def test_tell_status_payload_stringifies_and_filters_keys() -> None:
    payload = tell_status_payload(TASK, "abcd1234abcd1234")
    assert payload["gid"] == "abcd1234abcd1234"
    assert payload["status"] == "active"
    for key in ("totalLength", "completedLength", "downloadSpeed", "uploadSpeed", "connections"):
        assert isinstance(payload[key], str)
    assert payload["totalLength"] == "1000"
    assert payload["connections"] == "5"
    files = payload["files"]
    assert isinstance(files, list)
    entry = files[0]
    assert isinstance(entry, dict)
    assert entry["path"] == "movie.mkv", "path 仅 basename"
    assert entry["length"] == "1000"
    assert entry["selected"] == "true"

    filtered = tell_status_payload(TASK, "g", keys=["gid", "status"])
    assert set(filtered) == {"gid", "status"}


def test_uris_payload_only_exposes_display_uri() -> None:
    payload = uris_payload(TASK)
    assert payload == [{"uri": "https://example.com/展示链接", "status": "used"}]
    import dataclasses

    assert uris_payload(dataclasses.replace(TASK, display_uri=None)) == []


def test_global_stat_and_version_and_option_payloads() -> None:
    counts = {status: 0 for status in SubscriptionStatus}
    counts[SubscriptionStatus.ACTIVE] = 2
    counts[SubscriptionStatus.WAITING] = 3
    counts[SubscriptionStatus.CANCELLED] = 1
    stat = global_stat_payload(counts, 6, 111, 22)
    assert stat == {
        "downloadSpeed": "111",
        "uploadSpeed": "22",
        "numActive": "2",
        "numWaiting": "3",
        "numStopped": "1",
        "numStoppedTotal": "6",
    }
    version = version_payload()
    assert version["version"] and "aria2.rpc.multiCall" in version["enabledFeatures"]
    options = option_payload()
    assert "dir" in options and "secret" not in " ".join(options).lower()


def test_basename_handles_nested_and_windows_separators() -> None:
    assert basename_of("a/b/c.bin") == "c.bin"
    assert basename_of("c.bin") == "c.bin"
    assert basename_of("a\\b\\c.bin") == "c.bin"


def test_slice_window_supports_negative_offset() -> None:
    items = list(range(5))
    assert slice_window(items, -2, 5) == [3, 4]
    assert slice_window(items, 1, 2) == [1, 2]
    assert slice_window(items, 99, 5) == []
    assert slice_window(items, 0, 0) == []  # num=0 → 空（aria2 语义）
    assert slice_window(items, 0, 99) == items


@pytest.mark.parametrize(
    "code,expected",
    [
        ("upstream_failure", "上游下载失败"),
        ("download_unavailable", "下载不可用"),
        ("rpc_unavailable", "aria2 连接不可用"),
        ("aria2_rejected", "aria2 拒绝了操作"),
        ("task_too_large", "任务超出大小限制"),
        ("quota_exceeded", "存储配额不足"),
        ("physical_space_exceeded", "机器空间不足"),
        ("submission_uncertain", "提交结果不可确认"),
        ("recovery_unsafe", "恢复不安全"),
        ("invalid_input", "输入无效"),
        ("invalid_state", "状态无效"),
        ("content_busy", "内容被占用"),
        ("admission_paused", "系统暂停期间任务未提交"),
        ("metadata_admission_paused", "系统暂停期间元数据未提交"),
        # aria2 数字 fault 码并入统一拒绝文案。
        ("aria2_1", "aria2 拒绝了操作"),
        ("aria2_17", "aria2 拒绝了操作"),
        # 未知但形态安全的码原样回显（受控 snake_case 域，可排障）。
        ("custom_extension_code", "custom_extension_code"),
    ],
)
def test_failure_message_maps_controlled_codes_to_chinese(code: str, expected: str) -> None:
    assert failure_message(code) == expected
    assert failure_message(None) == ""


def test_failed_task_projects_localized_error_message() -> None:
    """B2：tellStatus 失败任务输出中文失败原因，errorCode 保留原始码。"""
    import dataclasses

    failed = dataclasses.replace(TASK, status=SubscriptionStatus.FAILED, error_code="upstream_failure")
    payload = tell_status_payload(failed, "g")
    assert payload["status"] == "error"
    assert payload["errorCode"] == "upstream_failure"
    assert payload["errorMessage"] == "上游下载失败"


@pytest.mark.parametrize(
    "status",
    [
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.COMPLETED,
        SubscriptionStatus.CANCELLED,
        SubscriptionStatus.EXPIRED,
    ],
)
def test_non_failed_statuses_keep_error_message_empty(status: SubscriptionStatus) -> None:
    """旧版同语义：errorMessage 仅 error 状态输出，其余状态（含残留 error_code）恒空串。"""
    import dataclasses

    payload = tell_status_payload(
        dataclasses.replace(TASK, status=status, error_code="upstream_failure"), "g"
    )
    assert payload["errorMessage"] == ""


def test_unsafe_error_code_text_never_leaks() -> None:
    """负断言：路径、URI、异常原文等非受控形态一律脱敏为通用文案，原文零出现。"""
    import dataclasses

    unsafe_values = (
        "/data/downloads/secret.iso",
        "https://internal.example/fetch/abc",
        "ValueError: boom at app/service.py:42",
        "a" * 200,
        "UPPER-CASE Leak",
    )
    for unsafe in unsafe_values:
        projection = dataclasses.replace(
            TASK, status=SubscriptionStatus.FAILED, error_code=unsafe
        )
        message = tell_status_payload(projection, "g")["errorMessage"]
        assert message == "下载失败"
        assert unsafe not in message
