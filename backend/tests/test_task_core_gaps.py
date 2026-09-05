"""Coverage gaps for app/modules/task_core (policy / submit / sync / unref) and user_ref."""

from __future__ import annotations

import pytest

from app.modules.backend.port import Snapshot
from app.modules.task_core import policy as pol
from app.modules.task_core import submit as sub
from app.modules.task_core import sync as syn
from app.modules.task_core import unref as unf
from app.modules.task_core.policy import Decision, QuotaContext


class FakeBackend:
    def __init__(self, *, unpause_error=None, unpause_status="active"):
        self.unpause_error = unpause_error
        self.unpause_status = unpause_status
        self.paused = []
        self.unpaused = []

    async def unpause(self, tid):
        if self.unpause_error:
            raise self.unpause_error
        self.unpaused.append(tid)

    async def pause(self, tid):
        self.paused.append(tid)

    async def tell_status(self, gid):
        return {"status": self.unpause_status}


def _patch_downloads(monkeypatch, rows_by_tid):
    async def fake_get(tid):
        return rows_by_tid.get(tid)

    monkeypatch.setattr(pol, "get_global_download_by_id", fake_get)
    monkeypatch.setattr(syn, "get_global_download_by_id", fake_get)


def _patch_updates(monkeypatch):
    updates = []

    async def fake_update(tid, values, expected_gid=None):
        updates.append((tid, values))
        return True

    monkeypatch.setattr(pol, "update_global_download", fake_update)
    monkeypatch.setattr(pol, "guarded_update_global_download", fake_update)
    return updates


class TestPolicySizeKnown:
    def test_size_known(self):
        assert pol._is_size_known({"size_known": 1}) is True
        assert pol._is_size_known({"size_known": 0}) is False
        assert pol._is_size_known({"total_bytes": 10}) is True
        assert pol._is_size_known({"total_bytes": 0}) is False


class TestObserveBackendStatus:
    @pytest.mark.asyncio
    async def test_no_row(self, monkeypatch):
        _patch_downloads(monkeypatch, {})
        assert await pol._observe_backend_status(FakeBackend(), 1) is None

    @pytest.mark.asyncio
    async def test_no_gid(self, monkeypatch):
        _patch_downloads(monkeypatch, {1: {"aria2_gid": None}})
        assert await pol._observe_backend_status(FakeBackend(), 1) is None

    @pytest.mark.asyncio
    async def test_rpc_error(self, monkeypatch):
        _patch_downloads(monkeypatch, {1: {"aria2_gid": "g"}})

        class Boom:
            async def tell_status(self, gid):
                raise RuntimeError("rpc down")

        assert await pol._observe_backend_status(Boom(), 1) is None

    @pytest.mark.asyncio
    async def test_non_mapping(self, monkeypatch):
        _patch_downloads(monkeypatch, {1: {"aria2_gid": "g"}})

        class Weird:
            async def tell_status(self, gid):
                return "not-a-dict"

        assert await pol._observe_backend_status(Weird(), 1) is None

    @pytest.mark.asyncio
    async def test_status_none(self, monkeypatch):
        _patch_downloads(monkeypatch, {1: {"aria2_gid": "g"}})

        class NoneStatus:
            async def tell_status(self, gid):
                return {"status": None}

        assert await pol._observe_backend_status(NoneStatus(), 1) is None


class TestApplyDecision:
    @pytest.mark.asyncio
    async def test_resume_unpause_error_then_running(self, monkeypatch):
        _patch_downloads(monkeypatch, {1: {"aria2_gid": "g", "error_code": None}})
        updates = _patch_updates(monkeypatch)
        backend = FakeBackend(unpause_error=RuntimeError("rpc down"))
        await pol.apply_decision(backend, 1, Decision("resume", clear_error_code=True))
        assert updates[0] == (1, {"error_code": None, "error_message": None})

    @pytest.mark.asyncio
    async def test_resume_unpause_not_running(self, monkeypatch):
        _patch_downloads(monkeypatch, {1: {"aria2_gid": "g", "error_code": None}})
        updates = _patch_updates(monkeypatch)
        backend = FakeBackend(unpause_status="paused")
        await pol.apply_decision(backend, 1, Decision("resume", clear_error_code=True))
        assert updates[0][1]["error_code"] == pol.ERROR_UNPAUSE_FAILED

    @pytest.mark.asyncio
    async def test_resume_keeps_system_code(self, monkeypatch):
        _patch_downloads(
            monkeypatch,
            {1: {"aria2_gid": "g", "error_code": pol.ERROR_QUOTA_QUEUED}},
        )
        updates = _patch_updates(monkeypatch)
        backend = FakeBackend(unpause_status="paused")
        await pol.apply_decision(backend, 1, Decision("resume", clear_error_code=True))
        assert updates == []

    @pytest.mark.asyncio
    async def test_mark_resource_queued(self, monkeypatch):
        _patch_downloads(monkeypatch, {})
        updates = _patch_updates(monkeypatch)
        backend = FakeBackend()
        await pol.apply_decision(
            backend, 1, Decision("mark_resource_queued", error_code="x")
        )
        assert backend.paused == [1]
        assert updates == [(1, {"error_code": "x"})]

    @pytest.mark.asyncio
    async def test_terminal_quota_exceeded(self, monkeypatch):
        _patch_downloads(monkeypatch, {})
        updates = _patch_updates(monkeypatch)
        backend = FakeBackend()
        await pol.apply_decision(
            backend,
            1,
            Decision(
                "terminal_quota_exceeded",
                error_code=pol.ERROR_QUOTA_EXCEEDED,
                terminal=True,
                total_bytes=10,
                quota_bytes=5,
            ),
        )
        assert backend.paused == [1]
        assert updates[0][1]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_terminal_quota_exceeded_unknown_values(self, monkeypatch):
        _patch_downloads(monkeypatch, {})
        updates = _patch_updates(monkeypatch)
        await pol.apply_decision(
            backend=FakeBackend(),
            tid=1,
            decision=Decision("terminal_quota_exceeded", terminal=True),
        )
        assert "数值未知" in updates[0][1]["error_message"]


class TestSubmit:
    @pytest.mark.asyncio
    async def test_missing_download(self, monkeypatch):
        async def none_get(tid):
            return None

        monkeypatch.setattr(sub, "get_global_download_by_id", none_get)
        assert await sub.submit_tid(backend=FakeBackend(), tid=1) is None

    @pytest.mark.asyncio
    async def test_already_submitted(self, monkeypatch):
        async def fake_get(tid):
            return {"aria2_gid": "g1", "status": "active"}

        monkeypatch.setattr(sub, "get_global_download_by_id", fake_get)
        assert await sub.submit_tid(backend=FakeBackend(), tid=1) == "g1"

    @pytest.mark.asyncio
    async def test_not_queued(self, monkeypatch):
        async def fake_get(tid):
            return {"aria2_gid": None, "status": "active"}

        monkeypatch.setattr(sub, "get_global_download_by_id", fake_get)
        assert await sub.submit_tid(backend=FakeBackend(), tid=1) is None

    @pytest.mark.asyncio
    async def test_no_uri(self, monkeypatch):
        async def fake_get(tid):
            return {"aria2_gid": None, "status": "queued"}

        monkeypatch.setattr(sub, "get_global_download_by_id", fake_get)
        assert await sub.submit_tid(backend=FakeBackend(), tid=1) is None


class TestSyncQuotaContext:
    @pytest.mark.asyncio
    async def test_no_owner(self, monkeypatch):
        async def none_owner(tid):
            return None

        monkeypatch.setattr(syn, "get_representative_active_owner_id", none_owner)
        assert await syn._build_quota_context(1) == QuotaContext()

    @pytest.mark.asyncio
    async def test_owner_user_missing(self, monkeypatch):
        async def owner(tid):
            return 5

        async def none_user(uid):
            return None

        monkeypatch.setattr(syn, "get_representative_active_owner_id", owner)
        monkeypatch.setattr(syn, "get_user_by_id", none_user)
        assert await syn._build_quota_context(1) == QuotaContext()


class TestRecordObservedSnapshot:
    @pytest.mark.asyncio
    async def test_empty_status_skipped(self):
        await syn.record_observed_snapshot(tid=1, observed_status={"status": ""})

    @pytest.mark.asyncio
    async def test_status_recorded(self):
        await syn.record_observed_snapshot(
            tid=1, observed_status={"status": "active", "gid": "g"}
        )


class TestSyncOnce:
    @pytest.mark.asyncio
    async def test_no_live_tids(self, monkeypatch):
        async def empty_list(statuses):
            return []

        monkeypatch.setattr(syn, "list_tracked_global_downloads", empty_list)
        report = await syn.sync_once(FakeBackend())
        assert report.updated == 0


class TestApplyQueuePolicy:
    @pytest.mark.asyncio
    async def test_no_snapshots(self, monkeypatch):
        async def empty_tell(tids):
            return []

        class Backend(FakeBackend):
            async def tell_many(self, tids):
                return []

        monkeypatch.setattr(syn, "list_tracked_global_downloads", _empty_rows())
        report = await syn.apply_queue_policy(Backend())
        assert report.skipped == 0


def _empty_rows():
    async def inner(statuses):
        return []

    return inner


class TestUnrefDiagnose:
    @pytest.mark.asyncio
    async def test_defensive_active_row(self, temp_db, test_user, failed_task, monkeypatch):
        # 行仍为 active 且属于本人时走防御分支
        from app.db.engine import transaction
        from app.db.schema import user_tasks
        from sqlalchemy import update

        async with transaction() as conn:
            await conn.execute(
                update(user_tasks)
                .where(user_tasks.c.id == failed_task["id"])
                .values(status="active")
            )
        err = await unf._diagnose_unref_failure(
            user_id=test_user["id"], pid=failed_task["id"]
        )
        assert err.code == unf.ERROR_NOT_FOUND


class TestUserRefProjection:
    def test_unknown_label(self):
        from app.modules.user_ref.projection import user_visible_label

        assert user_visible_label("mystery", "any") == "未知"
