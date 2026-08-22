"""M18 Task 2: tracker 远程刷新容错 + 定时循环 + 手动刷新端点。"""

import asyncio

import pytest
from aiohttp import web
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services import settings_service, tracker_list_service


@pytest.fixture
def admin_client(client: TestClient, admin_session: str) -> TestClient:
    client.cookies.set(settings.session_cookie_name, admin_session)
    return client


async def _configure(payload: dict) -> None:
    await settings_service.update_api_settings(payload)


def _install_fake_fetch(monkeypatch, bodies: dict, failing: set[str] | None = None):
    failing = failing or set()

    async def fake_fetch(url: str) -> str:
        if url in failing:
            raise RuntimeError("boom")
        return bodies[url]

    monkeypatch.setattr(tracker_list_service, "_fetch_url", fake_fetch)


GOOD_URL = "https://trackers.example/good.txt"
BAD_URL = "https://trackers.example/bad.txt"


class TestRefreshRemoteTrackers:
    def test_success_merges_fixed_and_remote_and_persists(self, temp_db: str, monkeypatch):
        _install_fake_fetch(
            monkeypatch,
            {
                GOOD_URL: "udp://r1.example:80/announce\n",
                BAD_URL: "http://r2.example/announce",
            },
        )
        asyncio.run(
            _configure(
                {
                    "tracker_fixed_list": "udp://fixed.example:80/announce",
                    "tracker_remote_urls": f"{GOOD_URL}\n{BAD_URL}",
                }
            )
        )

        result = asyncio.run(tracker_list_service.refresh_remote_trackers())

        assert result["last_refresh_status"] == "ok"
        assert result["last_refresh_failed_urls"] == []
        assert result["entry_count"] == 3
        assert result["last_refresh_at_ms"] is not None
        assert (
            tracker_list_service.get_bt_tracker_option()
            == "udp://fixed.example:80/announce,udp://r1.example:80/announce,http://r2.example/announce"
        )
        status = asyncio.run(tracker_list_service.get_tracker_status())
        assert status["entry_count"] == 3

    def test_partial_failure_records_failed_urls(self, temp_db: str, monkeypatch):
        _install_fake_fetch(
            monkeypatch,
            {GOOD_URL: "udp://r1.example:80/announce\n"},
            failing={BAD_URL},
        )
        asyncio.run(
            _configure(
                {
                    "tracker_remote_urls": f"{GOOD_URL}\n{BAD_URL}",
                }
            )
        )

        result = asyncio.run(tracker_list_service.refresh_remote_trackers())

        assert result["last_refresh_status"] == "partial"
        assert result["last_refresh_failed_urls"] == [BAD_URL]
        assert result["entry_count"] == 1
        assert tracker_list_service.get_bt_tracker_option() == "udp://r1.example:80/announce"

    def test_all_failed_preserves_previous_result(self, temp_db: str, monkeypatch):
        _install_fake_fetch(monkeypatch, {GOOD_URL: "udp://r1.example:80/announce\n"})
        asyncio.run(
            _configure(
                {
                    "tracker_fixed_list": "udp://fixed.example:80/announce",
                    "tracker_remote_urls": GOOD_URL,
                }
            )
        )
        asyncio.run(tracker_list_service.refresh_remote_trackers())
        before = tracker_list_service.get_bt_tracker_option()

        _install_fake_fetch(monkeypatch, {}, failing={GOOD_URL})
        result = asyncio.run(tracker_list_service.refresh_remote_trackers())

        assert result["last_refresh_status"] == "failed"
        assert result["last_refresh_failed_urls"] == [GOOD_URL]
        assert tracker_list_service.get_bt_tracker_option() == before
        assert result["entry_count"] == 2  # 保留上次合并结果

    def test_invalid_remote_entries_filtered(self, temp_db: str, monkeypatch):
        _install_fake_fetch(
            monkeypatch,
            {GOOD_URL: "udp://ok.example:80/announce\nftp://bad.example/announce\njunk\n"},
        )
        asyncio.run(_configure({"tracker_remote_urls": GOOD_URL}))

        result = asyncio.run(tracker_list_service.refresh_remote_trackers())

        assert result["last_refresh_status"] == "ok"
        assert result["entry_count"] == 1
        assert tracker_list_service.get_bt_tracker_option() == "udp://ok.example:80/announce"

    def test_no_urls_configured_is_noop(self, temp_db: str, monkeypatch):
        called = False

        async def fake_fetch(url: str) -> str:
            nonlocal called
            called = True
            return ""

        monkeypatch.setattr(tracker_list_service, "_fetch_url", fake_fetch)
        asyncio.run(_configure({"tracker_remote_urls": ""}))

        result = asyncio.run(tracker_list_service.refresh_remote_trackers())

        assert called is False
        assert result["last_refresh_status"] == "never"

    def test_failed_refresh_then_success_updates_status(self, temp_db: str, monkeypatch):
        _install_fake_fetch(monkeypatch, {}, failing={GOOD_URL})
        asyncio.run(_configure({"tracker_remote_urls": GOOD_URL}))
        asyncio.run(tracker_list_service.refresh_remote_trackers())

        _install_fake_fetch(monkeypatch, {GOOD_URL: "udp://r1.example:80/announce\n"})
        result = asyncio.run(tracker_list_service.refresh_remote_trackers())

        assert result["last_refresh_status"] == "ok"
        assert result["last_refresh_failed_urls"] == []


class TestFetchUrlLimits:
    def test_rejects_oversized_body_and_non_200(self):
        async def big_handler(request):
            return web.Response(body=b"x" * (1024 * 1024 + 1))

        async def ok_handler(request):
            return web.Response(text="udp://ok.example/announce")

        async def scenario():
            app = web.Application()
            app.router.add_get("/big", big_handler)
            app.router.add_get("/ok", ok_handler)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = runner.addresses[0][1]
            try:
                with pytest.raises(ValueError):
                    await tracker_list_service._fetch_url(f"http://127.0.0.1:{port}/big")
                from aiohttp import ClientResponseError

                with pytest.raises(ClientResponseError):
                    await tracker_list_service._fetch_url(
                        f"http://127.0.0.1:{port}/missing"
                    )
                body = await tracker_list_service._fetch_url(
                    f"http://127.0.0.1:{port}/ok"
                )
                assert body == "udp://ok.example/announce"
            finally:
                await runner.cleanup()

        asyncio.run(scenario())


class TestRefresherLoop:
    def test_interval_zero_skips_refresh(self, temp_db: str, monkeypatch):
        calls = []

        async def fake_refresh():
            calls.append(1)

        monkeypatch.setattr(tracker_list_service, "refresh_remote_trackers", fake_refresh)
        asyncio.run(_configure({"tracker_refresh_interval_minutes": 0}))

        delay = asyncio.run(tracker_list_service._refresher_iteration())

        assert delay == tracker_list_service.REFRESHER_IDLE_SLEEP_SECONDS
        assert calls == []

    def test_interval_positive_triggers_refresh(self, temp_db: str, monkeypatch):
        calls = []

        async def fake_refresh():
            calls.append(1)

        monkeypatch.setattr(tracker_list_service, "refresh_remote_trackers", fake_refresh)
        asyncio.run(_configure({"tracker_refresh_interval_minutes": 5}))

        delay = asyncio.run(tracker_list_service._refresher_iteration())

        assert delay == 5 * 60
        assert calls == [1]


class TestManualRefreshEndpoint:
    def test_admin_can_trigger_refresh(self, admin_client: TestClient, monkeypatch):
        async def fake_refresh():
            return {
                "entry_count": 3,
                "updated_at_ms": 1,
                "last_refresh_at_ms": 2,
                "last_refresh_status": "ok",
                "last_refresh_failed_urls": [],
            }

        monkeypatch.setattr(tracker_list_service, "refresh_remote_trackers", fake_refresh)

        response = admin_client.post("/api/config/trackers/refresh")

        assert response.status_code == 200
        body = response.json()
        assert body["last_refresh_status"] == "ok"
        assert body["entry_count"] == 3
        assert body["last_refresh_failed_urls"] == []

    def test_non_admin_forbidden(self, authenticated_client: TestClient):
        response = authenticated_client.post("/api/config/trackers/refresh")
        assert response.status_code == 403


class TestSaveTriggersAsyncRefresh:
    def test_tracker_save_schedules_refresh_task(self, temp_db: str, monkeypatch):
        calls = []

        async def fake_refresh():
            calls.append(1)

        monkeypatch.setattr(tracker_list_service, "refresh_remote_trackers", fake_refresh)

        async def scenario():
            await settings_service.update_api_settings_with_runtime_refresh(
                {"tracker_fixed_list": "udp://x.example:80/announce"}
            )
            await asyncio.sleep(0)

        asyncio.run(scenario())
        assert calls == [1]

    def test_non_tracker_save_does_not_schedule(self, temp_db: str, monkeypatch):
        calls = []

        async def fake_refresh():
            calls.append(1)

        monkeypatch.setattr(tracker_list_service, "refresh_remote_trackers", fake_refresh)

        async def scenario():
            await settings_service.update_api_settings_with_runtime_refresh(
                {"site_title": "新标题"}
            )
            await asyncio.sleep(0)

        asyncio.run(scenario())
        assert calls == []
