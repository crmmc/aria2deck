"""M18 Task 1: tracker 列表注入 — 解析/合并纯函数与配置校验/保存语义。"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services import tracker_list_service
from app.services.tracker_list_service import (
    MAX_MERGED_TRACKER_COUNT,
    merge_trackers,
    parse_tracker_lines,
)


@pytest.fixture
def admin_client(client: TestClient, admin_session: str) -> TestClient:
    client.cookies.set(settings.session_cookie_name, admin_session)
    return client


class TestParseTrackerLines:
    def test_splits_newline_and_comma_and_rejects_bad_scheme(self):
        raw = (
            "udp://t1.example:6969/announce\n"
            "http://t2.example/announce, https://t3.example/announce\n"
            "\n"
            "   \n"
            "ftp://bad.example/announce\n"
            "notaurl\n"
        )
        valid, invalid_count = parse_tracker_lines(raw)
        assert valid == [
            "udp://t1.example:6969/announce",
            "http://t2.example/announce",
            "https://t3.example/announce",
        ]
        assert invalid_count == 2

    def test_rejects_overlong_entry(self):
        long_tracker = "udp://" + "a" * 3000 + ".example:80/announce"
        valid, invalid_count = parse_tracker_lines(long_tracker)
        assert valid == []
        assert invalid_count == 1

    def test_empty_input(self):
        assert parse_tracker_lines("") == ([], 0)
        assert parse_tracker_lines("\n , \n") == ([], 0)


class TestMergeTrackers:
    def test_fixed_first_dedupe_preserve_order(self):
        merged = merge_trackers(
            ["udp://a/announce", "http://b/announce", "udp://a/announce"],
            [["http://c/announce", "udp://a/announce"], ["udp://d/announce"]],
        )
        assert merged == [
            "udp://a/announce",
            "http://b/announce",
            "http://c/announce",
            "udp://d/announce",
        ]

    def test_truncates_at_max_count(self):
        fixed = [f"udp://f{i}.example/announce" for i in range(3000)]
        remote = [f"udp://r{i}.example/announce" for i in range(3000)]
        merged = merge_trackers(fixed, [remote])
        assert len(merged) == MAX_MERGED_TRACKER_COUNT == 5000
        assert merged[:3000] == fixed
        assert merged[3000:] == remote[:2000]


class TestTrackerConfigValidation:
    def test_interval_below_five_rejected(self, admin_client: TestClient):
        response = admin_client.put(
            "/api/config", json={"tracker_refresh_interval_minutes": 3}
        )
        assert response.status_code == 400
        assert "tracker 刷新间隔" in response.json()["detail"]

    def test_interval_zero_and_five_accepted(self, admin_client: TestClient):
        for value in (0, 5):
            response = admin_client.put(
                "/api/config", json={"tracker_refresh_interval_minutes": value}
            )
            assert response.status_code == 200
            assert response.json()["tracker_refresh_interval_minutes"] == value

    def test_remote_url_bad_scheme_rejected(self, admin_client: TestClient):
        response = admin_client.put(
            "/api/config", json={"tracker_remote_urls": "udp://t.example/list.txt"}
        )
        assert response.status_code == 400
        assert "远程 tracker URL" in response.json()["detail"]

    def test_remote_url_ssrf_rejected(self, admin_client: TestClient):
        response = admin_client.put(
            "/api/config",
            json={"tracker_remote_urls": "https://93.184.216.34/ok.txt\nhttp://127.0.0.1:8000/trackers.txt"},
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "127.0.0.1" in detail

    def test_fixed_tracker_bad_scheme_rejected(self, admin_client: TestClient):
        response = admin_client.put(
            "/api/config",
            json={"tracker_fixed_list": "udp://ok.example:80/announce\nftp://bad.example/announce"},
        )
        assert response.status_code == 400
        assert "固定 tracker" in response.json()["detail"]

    def test_fixed_tracker_unresolvable_domain_accepted(self, admin_client: TestClient):
        response = admin_client.put(
            "/api/config",
            json={"tracker_fixed_list": "udp://nonexistent-domain.invalid:6969/announce"},
        )
        assert response.status_code == 200

    def test_get_config_includes_tracker_status_never(self, admin_client: TestClient):
        response = admin_client.get("/api/config")
        assert response.status_code == 200
        status = response.json()["tracker_status"]
        assert status["last_refresh_status"] == "never"
        assert status["entry_count"] == 0
        assert status["last_refresh_failed_urls"] == []
        assert status["last_refresh_at_ms"] is None


class TestSaveRebuildsCache:
    def test_save_fixed_list_applies_immediately(self, admin_client: TestClient):
        fixed = "udp://t1.example:6969/announce\nhttp://t2.example/announce"
        response = admin_client.put("/api/config", json={"tracker_fixed_list": fixed})
        assert response.status_code == 200
        assert (
            tracker_list_service.get_bt_tracker_option()
            == "udp://t1.example:6969/announce,http://t2.example/announce"
        )

    def test_load_from_db_after_restart(self, admin_client: TestClient):
        admin_client.put(
            "/api/config",
            json={"tracker_fixed_list": "udp://restart.example:80/announce"},
        )
        tracker_list_service.reset_tracker_cache()
        assert tracker_list_service.get_bt_tracker_option() is None
        asyncio.run(tracker_list_service.load_from_db())
        assert (
            tracker_list_service.get_bt_tracker_option()
            == "udp://restart.example:80/announce"
        )

    def test_empty_cache_means_no_injection(self, temp_db: str):
        tracker_list_service.reset_tracker_cache()
        asyncio.run(tracker_list_service.load_from_db())
        assert tracker_list_service.get_bt_tracker_option() is None
