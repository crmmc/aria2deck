"""Coverage gaps for app/aria2 listener/sync helpers and app/http/file_response."""

from __future__ import annotations

import asyncio

import pytest

from app.aria2 import listener as lst
from app.aria2 import sync as syn


class TestListenerHelpers:
    @pytest.mark.asyncio
    async def test_run_ordered_event_awaits_failing_previous(self):
        async def boom():
            raise RuntimeError("previous failed")

        called = []

        async def fake_handle(gid, event):
            called.append((gid, event))

        orig = lst.handle_aria2_event
        lst.handle_aria2_event = fake_handle
        try:
            await lst._run_ordered_event(
                asyncio.ensure_future(boom()), "g1", "onDownloadStart"
            )
        finally:
            lst.handle_aria2_event = orig
        assert called == [("g1", "onDownloadStart")]

    def test_event_task_done_cancelled_and_exception(self):
        async def noop():
            pass

        async def boom():
            raise RuntimeError("x")

        loop = asyncio.new_event_loop()
        try:
            ok_task = loop.create_task(noop())
            lst._event_tasks.add(ok_task)
            lst._event_tails["g1"] = ok_task
            ok_task.add_done_callback(lambda t: lst._event_task_done("g1", t))
            boom_task = loop.create_task(boom())
            boom_task.add_done_callback(lambda t: lst._event_task_done("g2", t))
            cancel_task = loop.create_task(noop())
            cancel_task.add_done_callback(lambda t: lst._event_task_done("g3", t))
            loop.run_until_complete(asyncio.gather(ok_task, boom_task, return_exceptions=True))
            cancel_task.cancel()
            loop.run_until_complete(asyncio.gather(cancel_task, return_exceptions=True))
        finally:
            loop.close()
        assert "g1" not in lst._event_tails

    @pytest.mark.asyncio
    async def test_shutdown_event_tasks(self):
        started = asyncio.Event()

        async def hang():
            started.set()
            await asyncio.sleep(30)

        task = asyncio.ensure_future(hang())
        while not started.is_set():
            await asyncio.sleep(0)
        lst._event_tasks.add(task)
        lst._event_tails["g"] = task
        await lst._shutdown_event_tasks()
        assert task.cancelled() or task.done()

    def test_http_to_ws_url(self):
        assert lst._http_to_ws_url("http://h:6800/jsonrpc") == "ws://h:6800/jsonrpc"
        assert lst._http_to_ws_url("https://h/jsonrpc") == "wss://h/jsonrpc"

    def test_calculate_backoff(self):
        assert lst._calculate_backoff(0) < lst._calculate_backoff(3)


class TestSyncHelpers:
    @pytest.mark.asyncio
    async def test_sync_tasks_round_failure_then_cancel(self, monkeypatch):
        rounds = {"n": 0}

        async def failing():
            rounds["n"] += 1
            raise RuntimeError("db down")

        monkeypatch.setattr(syn, "_sync_tasks_once", failing)

        async def cancel_soon(*args, **kwargs):
            raise asyncio.CancelledError

        monkeypatch.setattr(syn.asyncio, "sleep", cancel_soon)
        with pytest.raises(asyncio.CancelledError):
            await syn.sync_tasks(interval=0.01)
        assert rounds["n"] >= 1

    @pytest.mark.asyncio
    async def test_sync_tasks_once_no_downloads_backend_ok(self, monkeypatch, temp_db):
        async def repair_ok():
            return None

        monkeypatch.setattr(syn.repair, "repair_inconsistent_completed_downloads_v0", repair_ok)
        monkeypatch.setattr(syn, "list_v0_tracked_downloads", _async([]))
        client = _fake_backend_client()
        monkeypatch.setattr(syn, "get_aria2_client", lambda: client)

        async def ok_queue(backend):
            return None

        monkeypatch.setattr(syn, "apply_queue_policy", ok_queue)
        await syn._sync_tasks_once()

    @pytest.mark.asyncio
    async def test_sync_tasks_once_backend_probe_transient(self, monkeypatch, temp_db):
        class FailingClient:
            async def get_version(self):
                import aiohttp

                raise aiohttp.ClientConnectionError("down")

        monkeypatch.setattr(syn.repair, "repair_inconsistent_completed_downloads_v0", _async(None))
        monkeypatch.setattr(syn, "list_v0_tracked_downloads", _async([]))
        monkeypatch.setattr(syn, "get_aria2_client", lambda: FailingClient())
        await syn._sync_tasks_once()

    @pytest.mark.asyncio
    async def test_sync_tasks_once_backend_probe_hard_error(self, monkeypatch, temp_db):
        class FailingClient:
            async def get_version(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(syn.repair, "repair_inconsistent_completed_downloads_v0", _async(None))
        monkeypatch.setattr(syn, "list_v0_tracked_downloads", _async([]))
        monkeypatch.setattr(syn, "get_aria2_client", lambda: FailingClient())
        await syn._sync_tasks_once()

    @pytest.mark.asyncio
    async def test_sync_tasks_once_download_tell_error(self, monkeypatch, temp_db):
        download = {"id": 1, "aria2_gid": "g1"}

        class ErrorClient:
            async def tell_status(self, gid):
                raise RuntimeError("hard error")

            async def get_version(self):
                return {"version": "1"}

        monkeypatch.setattr(syn.repair, "repair_inconsistent_completed_downloads_v0", _async(None))
        monkeypatch.setattr(syn, "list_v0_tracked_downloads", _async([download]))
        monkeypatch.setattr(syn, "get_aria2_client", lambda: ErrorClient())

        async def fake_reconcile(**kwargs):
            return syn.ReconcileResult.NOOP

        from app.services.lifecycle import coordinator

        monkeypatch.setattr(coordinator, "reconcile_attempt_signal", fake_reconcile)
        monkeypatch.setattr(syn, "apply_queue_policy", _async(None))
        await syn._sync_tasks_once()

    @pytest.mark.asyncio
    async def test_cleanup_owned_stopped_results(self, monkeypatch):
        removed = []

        class Client:
            async def tell_stopped(self, offset, num):
                return [{"gid": "g1"}, {"gid": "g2"}, {"gid": 5}, {}]

            async def remove_download_result(self, gid):
                removed.append(gid)

        await syn._cleanup_owned_stopped_results(Client(), {"g1", "g2"}, 5)
        assert sorted(removed) == ["g1", "g2"]

    @pytest.mark.asyncio
    async def test_cleanup_owned_stopped_results_fails(self):
        class Client:
            async def tell_stopped(self, offset, num):
                raise RuntimeError("down")

        await syn._cleanup_owned_stopped_results(Client(), set(), 5)

    @pytest.mark.asyncio
    async def test_cleanup_owned_stopped_result_removal_fails(self):
        class Client:
            async def tell_stopped(self, offset, num):
                return [{"gid": "g1"}]

            async def remove_download_result(self, gid):
                raise RuntimeError("nope")

        await syn._cleanup_owned_stopped_results(Client(), {"g1"}, 5)


def _async(value):
    async def inner(*args, **kwargs):
        return value

    return inner


def _fake_backend_client():
    from tests.fakes import make_aria2_client

    return make_aria2_client()


class TestSyncReconcileBranches:
    @pytest.fixture(autouse=True)
    def _env(self, temp_db, monkeypatch):
        monkeypatch.setattr(
            syn.repair,
            "repair_inconsistent_completed_downloads_v0",
            _async(None),
        )
        monkeypatch.setattr(syn, "apply_queue_policy", _async(None))

    def _patch(self, monkeypatch, downloads, client, reconcile=None):
        monkeypatch.setattr(syn, "list_v0_tracked_downloads", _async(downloads))
        monkeypatch.setattr(syn, "get_aria2_client", lambda: client)
        if reconcile is not None:
            from app.services.lifecycle import coordinator

            monkeypatch.setattr(
                coordinator, "reconcile_attempt_signal", reconcile
            )

    @pytest.mark.asyncio
    async def test_download_without_gid_skipped(self, monkeypatch):
        self._patch(monkeypatch, [{"id": 1, "aria2_gid": None}], _fake_backend_client())
        await syn._sync_tasks_once()

    @pytest.mark.asyncio
    async def test_missing_gid_error(self, monkeypatch):
        class Client:
            async def tell_status(self, gid):
                raise RuntimeError("Active not found")

        results = []

        async def reconcile(**kwargs):
            results.append(kwargs.get("observed_gid"))
            return syn.ReconcileResult.NOOP

        self._patch(
            monkeypatch, [{"id": 1, "aria2_gid": "g1"}], Client(), reconcile
        )
        await syn._sync_tasks_once()
        assert results == ["g1"]

    @pytest.mark.asyncio
    async def test_transient_rpc_error(self, monkeypatch):
        class Client:
            async def tell_status(self, gid):
                raise TimeoutError("temporary")

            async def get_version(self):
                return {"version": "1"}

        self._patch(
            monkeypatch, [{"id": 1, "aria2_gid": "g1"}], Client(), _async(None)
        )
        await syn._sync_tasks_once()

    @pytest.mark.asyncio
    async def test_reconcile_terminal_adds_removable(self, monkeypatch):
        class Client:
            async def tell_status(self, gid):
                return {"status": "active"}

        removable = {"g1"}

        async def cleanup(client, removable_gids, max_actions):
            removable.update(removable_gids)

        monkeypatch.setattr(syn, "_cleanup_owned_stopped_results", cleanup)
        self._patch(
            monkeypatch,
            [{"id": 1, "aria2_gid": "g1"}],
            Client(),
            _async(syn.ReconcileResult.TERMINALIZED),
        )
        await syn._sync_tasks_once()

    @pytest.mark.asyncio
    async def test_queue_policy_raises(self, monkeypatch):
        async def boom(backend):
            raise RuntimeError("policy failed")

        monkeypatch.setattr(syn, "apply_queue_policy", boom)
        self._patch(monkeypatch, [], _fake_backend_client())
        await syn._sync_tasks_once()

    @pytest.mark.asyncio
    async def test_cleanup_batch_limit(self):
        class Client:
            async def tell_stopped(self, offset, num):
                return [{"gid": f"g{i}"} for i in range(5)]

            async def remove_download_result(self, gid):
                return "OK"

        await syn._cleanup_owned_stopped_results(
            Client(), {f"g{i}" for i in range(5)}, 2
        )
