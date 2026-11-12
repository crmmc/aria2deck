"""回归（D-大小）：max_task_size 必须对创建时已知大小的任务生效。

修复前只有 HTTP 内部网关检查 max_task_size；Torrent 创建路径（大小已知）
不检查，超限任务被受理。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.api.conftest import create_admin, login
from tests.api.test_download_writes import install_backend
from tests.download_fakes import make_torrent


def _set_max_task_size(client: TestClient, value: int) -> None:
    response = client.patch("/api/v2/settings", json={"max_task_size": value})
    assert response.status_code == 200, response.text


def test_torrent_create_rejects_known_size_over_max_task_size(
    client: TestClient,
) -> None:
    create_admin(client)
    login(client)
    install_backend(client)
    _set_max_task_size(client, 100)

    response = client.post(
        "/api/v2/tasks/torrent",
        json={"torrent": make_torrent([("big.bin", 101)], name="big")},
    )
    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "task_too_large"


def test_torrent_create_accepts_size_exactly_at_max_task_size(
    client: TestClient,
) -> None:
    create_admin(client)
    login(client)
    install_backend(client)
    _set_max_task_size(client, 100)

    response = client.post(
        "/api/v2/tasks/torrent",
        json={"torrent": make_torrent([("edge.bin", 100)], name="edge")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] is True


def test_max_task_size_zero_disables_the_limit(client: TestClient) -> None:
    create_admin(client)
    login(client)
    install_backend(client)
    _set_max_task_size(client, 0)

    response = client.post(
        "/api/v2/tasks/torrent",
        json={"torrent": make_torrent([("big.bin", 10 * 1024**2)], name="big")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] is True


def test_http_create_with_unknown_size_still_accepted(client: TestClient) -> None:
    create_admin(client)
    login(client)
    install_backend(client)
    _set_max_task_size(client, 100)

    # 创建时大小未知：不得拒绝；上限在观测确认后执行。
    response = client.post(
        "/api/v2/tasks",
        json={"tasks": [{"uri": "https://example.com/later-known.bin"}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["accepted"] is True
