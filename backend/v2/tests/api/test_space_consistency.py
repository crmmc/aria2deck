"""回归（D-空间）：文件列表与任务统计必须共用同一用户可见空间口径。

修复前：files.space 用纯配额 available_user_bytes，stats.disk 用 visible_space
（叠加机器余量并带 limited），同一状态下两入口字段不一致。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.api.conftest import create_admin, login


class FixedMachineSpace:
    def __init__(self, free_bytes: int) -> None:
        self._free = free_bytes

    def machine_free_bytes(self) -> int:
        return self._free


def test_files_space_matches_stats_disk_under_tight_machine_headroom(
    client: TestClient,
) -> None:
    create_admin(client)
    login(client)
    ten_gib = 10 * 1024**3
    client.app.state.download_queries.machine_space = FixedMachineSpace(ten_gib)

    files = client.get("/api/v2/files?page=1&page_size=1")
    assert files.status_code == 200, files.text
    stats = client.get("/api/v2/tasks/stats")
    assert stats.status_code == 200, stats.text
    space = files.json()["space"]
    disk = stats.json()["disk"]

    # 默认 min_free_disk=1GiB：headroom=9GiB < 100GiB 配额，可见空间被磁盘收紧。
    headroom = ten_gib - 1024**3
    assert disk["available_bytes"] == headroom
    assert disk["limited"] is True
    # 两入口同一口径：字段值与限制标记一致。
    assert space["available_bytes"] == disk["available_bytes"]
    assert space["limited"] == disk["limited"]
    assert space["used_bytes"] == disk["used_bytes"] == 0
    assert space["reserved_bytes"] == disk["reserved_bytes"] == 0
    # total 自洽：quota_bytes（可见总量）- used - reserved == available，且不为负。
    assert space["quota_bytes"] == space["used_bytes"] + space["reserved_bytes"] + space["available_bytes"]


def test_files_space_matches_stats_disk_when_quota_is_the_limit(
    client: TestClient,
) -> None:
    create_admin(client)
    login(client)
    # 机器宽裕：headroom 远超配额，可见空间由配额决定，limited=False。
    client.app.state.download_queries.machine_space = FixedMachineSpace(10 * 1024**4)

    files = client.get("/api/v2/files?page=1&page_size=1")
    stats = client.get("/api/v2/tasks/stats")
    space = files.json()["space"]
    disk = stats.json()["disk"]

    assert disk["limited"] is False
    assert space["available_bytes"] == disk["available_bytes"]
    assert space["limited"] == disk["limited"] is False
