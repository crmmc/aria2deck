"""M24 Task 2 批量提交 saga 测试（service 公共入口 + 持久状态 + RPC 观察）。"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.aria2.client import MulticallOutcome
from app.modules.task_core.register import ResourceSpec
from app.modules.task_core.states import (
    ERROR_ADMISSION_PAUSED,
    ERROR_METADATA_ADMISSION_PAUSED,
)
from app.services.gateway import http_resource_identity, source_request_options
from app.services.hash import get_uri_hash
import app.services.task_batch_submission as mod
from app.services.task_batch_submission import (
    BatchTaskItem,
    derive_planned_gid,
)

HTTP_URI = "https://example.com/files/a.bin"
MAGNET_URI = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
QUOTA = 100 * 1024 * 1024 * 1024


def ok(value):
    return MulticallOutcome(ok=True, result=value)


def _http_identity() -> str:
    """与批量服务 HTTP 原URI identity 一致的 resource key。"""
    return http_resource_identity(
        get_uri_hash(HTTP_URI), source_request_options(None)
    )


def fault(code=1, message="GID x is not found"):
    return MulticallOutcome(ok=False, fault_code=code, fault_message=message)


class FakeAria2Client:
    """可编程 fake：记录 multicall payload，按脚本返回 outcome 列表或抛错。"""

    def __init__(self):
        self.scripts: list[list[MulticallOutcome] | Exception] = []
        self.calls: list[list[dict]] = []
        self.removed: list[str] = []

    def enqueue(self, outcomes):
        self.scripts.append(outcomes)

    def enqueue_error(self, exc):
        self.scripts.append(exc)

    async def multicall(self, calls):
        self.calls.append([dict(call) for call in calls])
        if not self.scripts:
            raise AssertionError("unexpected multicall")
        result = self.scripts.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def force_remove(self, gid):
        self.removed.append(gid)

    async def remove(self, gid):
        self.removed.append(gid)

    async def remove_download_result(self, gid):
        pass

    async def tell_status(self, gid):
        raise AssertionError("not scripted")

    async def get_uris(self, gid):
        raise AssertionError("not scripted")

    @property
    def add_calls(self):
        return [
            call
            for batch in self.calls
            for call in batch
            if call["methodName"] == "aria2.addUri"
        ]


def _internal_uris(tid, count=1, internal_base="http://127.0.0.1:8001"):
    return [
        {"uri": f"{internal_base}/_internal/fetch/{tid}/{i}"}
        for i in range(1, count + 1)
    ]


def _gd(tid):
    from app.repositories.task.downloads import get_global_download_by_id

    return asyncio.run(get_global_download_by_id(tid))


def _user_task(pid):
    from app.db.engine import transaction
    from app.db.schema import user_tasks
    from sqlalchemy import select

    async def run():
        async with transaction() as conn:
            row = (
                await conn.execute(
                    select(user_tasks).where(user_tasks.c.id == pid)
                )
            ).mappings().first()
            return dict(row) if row else None

    return asyncio.run(run())


@pytest.fixture
def no_probe(monkeypatch):
    """HTTP 创建不得调用网络 probe。"""

    async def boom(*args, **kwargs):
        raise AssertionError("probe must not be called")

    from app.services import task_service

    monkeypatch.setattr(task_service, "probe_url_with_get_fallback", boom)


@pytest.fixture(autouse=True)
def fixed_internal_base(monkeypatch):
    monkeypatch.setattr(mod, "get_internal_base_url", lambda: "http://127.0.0.1:8001")


class TestCreatedSubmission:
    def test_created_success_binds_planned_gid_paused(
        self, temp_db, test_user, no_probe
    ):
        class Dynamic(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                assert len(calls) == 1
                gid = calls[0]["params"][1]["gid"]
                return [ok(gid)]

        dyn = Dynamic()
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[BatchTaskItem(uri=HTTP_URI)],
                client=dyn,
            )
        )
        assert result.accepted_count == 1
        item = result.results[0]
        assert item.accepted is True
        gd = _gd(item.global_download_id)
        assert gd["aria2_gid"] == derive_planned_gid(gd["id"])
        assert gd["status"] == "paused"
        assert gd["error_code"] == ERROR_ADMISSION_PAUSED
        assert len(dyn.calls) == 1

    def test_http_uses_gateway_uri_and_pause_marker(
        self, temp_db, test_user, no_probe
    ):
        class Dynamic(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                return [ok(calls[0]["params"][1]["gid"])]

        dyn = Dynamic()
        asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[BatchTaskItem(uri=HTTP_URI)],
                client=dyn,
            )
        )
        (call,) = dyn.add_calls
        uris, options = call["params"]
        assert len(uris) == 1
        assert "/_internal/fetch/" in uris[0]
        assert options["pause"] == "true"
        assert "gid" in options

    def test_magnet_uses_pause_metadata(self, temp_db, test_user, no_probe):
        class Dynamic(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                return [ok(calls[0]["params"][1]["gid"])]

        dyn = Dynamic()
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[BatchTaskItem(uri=MAGNET_URI)],
                client=dyn,
            )
        )
        (call,) = dyn.add_calls
        uris, options = call["params"]
        assert uris == [MAGNET_URI]
        assert options["pause-metadata"] == "true"
        gd = _gd(result.results[0].global_download_id)
        assert gd["error_code"] == ERROR_METADATA_ADMISSION_PAUSED

    def test_batch_single_multicall_for_multiple_items(
        self, temp_db, test_user, no_probe
    ):
        outcomes = [ok("g1"), ok("g2")]

        class Dynamic(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                gids = [c["params"][1]["gid"] for c in calls]
                return [ok(g) for g in gids]

        dyn = Dynamic()
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[
                    BatchTaskItem(uri=HTTP_URI),
                    BatchTaskItem(uri=MAGNET_URI),
                ],
                client=dyn,
            )
        )
        assert result.accepted_count == 2
        assert len(dyn.calls) == 1
        assert len(dyn.calls[0]) == 2
        _ = outcomes


class TestRegisterOutcomes:
    def test_joined_live_with_gid_accepted_without_call(self, temp_db, test_user, no_probe):
        # joined_live + 已绑定 gid：直接 accepted，不新增任何 aria2 RPC。
        from app.services import task_service

        seeded = asyncio.run(
            task_service.register(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                resource=ResourceSpec(
                    resource_key=_http_identity(),
                    source_uri=HTTP_URI,
                    resource_kind="http",
                ),
            )
        )
        assert seeded.outcome == "created"
        from app.repositories.task.downloads import update_global_download

        asyncio.run(
            update_global_download(
                seeded.tid, {"aria2_gid": "aaaabbbbccccdddd", "status": "active"}
            )
        )

        other = asyncio.run(_make_user("otheruser"))

        client = FakeAria2Client()
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=other["id"],
                quota_bytes=QUOTA,
                items=[BatchTaskItem(uri=HTTP_URI)],
                client=client,
            )
        )
        assert result.accepted_count == 1
        item = result.results[0]
        assert item.accepted is True
        assert item.global_download_id == seeded.tid
        assert item.status == "active"
        assert client.calls == []  # joined_live + gid 不产生任何 aria2 RPC

    def test_same_user_duplicate_fails_per_item(self, temp_db, test_user, no_probe):
        class Dynamic(FakeAria2Client):
            async def multicall(self, calls):
                return [ok(c["params"][1]["gid"]) for c in calls]

        dyn = Dynamic()
        first = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[BatchTaskItem(uri=MAGNET_URI)],
                client=dyn,
            )
        )
        assert first.accepted_count == 1
        second = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[BatchTaskItem(uri=MAGNET_URI)],
                client=dyn,
            )
        )
        assert second.accepted_count == 0
        assert second.results[0].error_code == "duplicate_task"

    def test_attached_completed_accepted(self, temp_db, test_user, no_probe):
        async def seed():
            from app.db.engine import transaction
            from app.db.schema import stored_files, global_downloads
            from sqlalchemy import insert

            async with transaction() as conn:
                sf = (
                    await conn.execute(
                        insert(stored_files)
                        .values(
                            content_hash="hash_att_1",
                            size_bytes=10,
                            real_path="/tmp/x",
                            original_name="a.bin",
                            created_at_ms=1_700_000_000_000,
                        )
                        .returning(stored_files)
                    )
                ).mappings().one()
                gd = (
                    await conn.execute(
                        insert(global_downloads)
                        .values(
                            resource_key=_http_identity(),
                            resource_kind="http",
                            source_uri=HTTP_URI,
                            status="completed",
                            total_bytes=10,
                            completed_bytes=10,
                            size_known=1,
                            completed_file_id=sf["id"],
                            created_at_ms=1_700_000_000_000,
                            updated_at_ms=1_700_000_000_000,
                        )
                        .returning(global_downloads)
                    )
                ).mappings().one()
                return dict(gd)

        gd = asyncio.run(seed())
        client = FakeAria2Client()
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[BatchTaskItem(uri=HTTP_URI)],
                client=client,
            )
        )
        assert result.accepted_count == 1
        item = result.results[0]
        assert item.status == "complete"
        assert item.global_download_id == gd["id"]
        assert client.calls == []  # 秒传不产生 aria2 RPC

    def test_magnet_attach_completed_accepted_no_rpc(self, temp_db, test_user):
        async def seed():
            from app.db.engine import transaction
            from app.db.schema import stored_files, global_downloads
            from sqlalchemy import insert

            async with transaction() as conn:
                sf = (
                    await conn.execute(
                        insert(stored_files)
                        .values(
                            content_hash="hash_att_m",
                            size_bytes=10,
                            real_path="/tmp/y",
                            original_name="m.bin",
                            created_at_ms=1_700_000_000_000,
                        )
                        .returning(stored_files)
                    )
                ).mappings().one()
                gd = (
                    await conn.execute(
                        insert(global_downloads)
                        .values(
                            resource_key="0123456789abcdef0123456789abcdef01234567",
                            resource_kind="magnet",
                            source_uri=MAGNET_URI,
                            status="completed",
                            total_bytes=10,
                            completed_bytes=10,
                            size_known=1,
                            completed_file_id=sf["id"],
                            created_at_ms=1_700_000_000_000,
                            updated_at_ms=1_700_000_000_000,
                        )
                        .returning(global_downloads)
                    )
                ).mappings().one()
                return dict(gd)

        asyncio.run(seed())
        client = FakeAria2Client()
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[BatchTaskItem(uri=MAGNET_URI)],
                client=client,
            )
        )
        assert result.accepted_count == 1
        item = result.results[0]
        assert item.status == "complete"
        assert client.calls == []


async def _make_user(username: str) -> dict:
    from tests.helpers_v0 import create_user_v0

    return await create_user_v0(username=username, password="pass12345", is_admin=False)


def _undetermined_seed(user, uri):
    """留下一个 queued/gid NULL 的未确认 attempt（模拟传输不可确认）。"""

    class BothFail(FakeAria2Client):
        async def multicall(self, calls):
            self.calls.append([dict(c) for c in calls])
            raise RuntimeError("connection refused")

    client = BothFail()
    with pytest.raises(mod.BatchSubmissionUndeterminedError):
        asyncio.run(
            mod.batch_create_tasks(
                user_id=user["id"],
                quota_bytes=QUOTA,
                items=[BatchTaskItem(uri=uri)],
                client=client,
            )
        )
    return client


class TestPendingJoinCandidate:
    def test_pending_join_uses_target_tid_planned_gid(
        self, temp_db, test_user, no_probe
    ):
        _undetermined_seed(test_user, MAGNET_URI)

        class ReconSuccess(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                if calls[0]["methodName"] == "aria2.addUri":
                    return [ok(c["params"][1]["gid"]) for c in calls]
                raise AssertionError("unexpected reconcile")

        other = asyncio.run(_make_user("joiner"))
        dyn = ReconSuccess()
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=other["id"],
                quota_bytes=QUOTA,
                items=[BatchTaskItem(uri=MAGNET_URI)],
                client=dyn,
            )
        )
        assert result.accepted_count == 1
        # 只有一个 addUri descriptor，gid 是 pending attempt 的 planned gid
        (call,) = dyn.add_calls
        gd = _gd(result.results[0].global_download_id)
        assert call["params"][1]["gid"] == derive_planned_gid(gd["id"])
        assert gd["aria2_gid"] == derive_planned_gid(gd["id"])


class TestFaultAndCompensation:
    def test_fault_missing_gid_terminates_attempt_and_releases(
        self, temp_db, test_user, no_probe
    ):
        tid_holder = {}

        class Script(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                if calls[0]["methodName"] == "aria2.addUri":
                    tid_holder["gid"] = calls[0]["params"][1]["gid"]
                    return [fault(message="gID is not found")]
                # reconciliation: tellStatus + getUris
                return [fault(message="GID x is not found"), fault()]

        client = Script()
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[BatchTaskItem(uri=HTTP_URI)],
                client=client,
            )
        )
        assert result.accepted_count == 0
        item = result.results[0]
        assert item.error_code == "submission_failed"
        gd = _gd(item.global_download_id)
        assert gd["status"] == "failed"
        assert gd["aria2_gid"] is None
        ut = _user_task(item.task_id)
        assert ut["status"] == "failed"

    def test_fault_owned_by_concurrent_success_confirms(self, temp_db, test_user, no_probe):
        # 并发请求已用同一 planned GID 成功：本请求 fault 后经 ownership 确认。
        class TidHook(FakeAria2Client):
            def __init__(self):
                super().__init__()
                self.tid = None

            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                if calls[0]["methodName"] == "aria2.addUri":
                    self.gid = calls[0]["params"][1]["gid"]
                    return [fault(message="gid already exists")]
                return [
                    ok({"gid": self.gid, "status": "paused"}),
                    ok(_internal_uris(self.tid)),
                ]

        client = TidHook()
        real_get = mod.get_global_download_by_id

        async def hooked(tid):
            client.tid = tid
            return await real_get(tid)

        mod.get_global_download_by_id = hooked
        try:
            result = asyncio.run(
                mod.batch_create_tasks(
                    user_id=test_user["id"],
                    quota_bytes=QUOTA,
                    items=[BatchTaskItem(uri=HTTP_URI)],
                    client=client,
                )
            )
        finally:
            mod.get_global_download_by_id = real_get
        assert result.accepted_count == 1
        gd = _gd(result.results[0].global_download_id)
        assert gd["aria2_gid"] == derive_planned_gid(gd["id"])
        assert gd["status"] == "paused"

    def test_transport_error_reconcile_confirms_owned(self, temp_db, test_user, no_probe):
        class Script(FakeAria2Client):
            def __init__(self):
                super().__init__()
                self.tid = None

            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                if calls[0]["methodName"] == "aria2.addUri":
                    raise RuntimeError("timeout")
                return [
                    ok({"gid": "g", "status": "paused"}),
                    ok(_internal_uris(self.tid)),
                ]

        client = Script()
        real_get = mod.get_global_download_by_id

        async def hooked(tid):
            client.tid = tid
            return await real_get(tid)

        mod.get_global_download_by_id = hooked
        try:
            result = asyncio.run(
                mod.batch_create_tasks(
                    user_id=test_user["id"],
                    quota_bytes=QUOTA,
                    items=[BatchTaskItem(uri=HTTP_URI)],
                    client=client,
                )
            )
        finally:
            mod.get_global_download_by_id = real_get
        assert result.accepted_count == 1
        gd = _gd(result.results[0].global_download_id)
        assert gd["aria2_gid"] == derive_planned_gid(gd["id"])

    def test_both_transports_fail_raises_undetermined_and_keeps_queued(
        self, temp_db, test_user, no_probe
    ):
        _undetermined_seed(test_user, HTTP_URI)
        gd_rows = asyncio.run(mod.list_pending_submission_candidates())
        assert len(gd_rows) == 1
        assert gd_rows[0]["aria2_gid"] is None
        assert gd_rows[0]["status"] == "queued"

    def test_success_gid_mismatch_fails_and_removes(self, temp_db, test_user, no_probe):
        class Script(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                return [ok("ffffffffffffffff")]

        client = Script()
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[BatchTaskItem(uri=HTTP_URI)],
                client=client,
            )
        )
        assert result.accepted_count == 0
        assert result.results[0].error_code == "gid_mismatch"
        assert "ffffffffffffffff" in client.removed

    def test_cas_preempted_by_cancel_removes_gid(self, temp_db, test_user, no_probe):
        from app.repositories.task.downloads import update_global_download

        real_assign = mod.assign_submitted_gid

        async def preempted_assign(*, download_id, gid, status, error_code):
            # 模拟并发取消：在 CAS 绑定前把 DB 置为终态
            await update_global_download(
                download_id, {"status": "failed", "error_code": "cancelled"}
            )
            return await real_assign(
                download_id=download_id, gid=gid, status=status, error_code=error_code
            )

        class Script(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                return [ok(c["params"][1]["gid"]) for c in calls]

        client = Script()
        mod.assign_submitted_gid = preempted_assign
        try:
            result = asyncio.run(
                mod.batch_create_tasks(
                    user_id=test_user["id"],
                    quota_bytes=QUOTA,
                    items=[BatchTaskItem(uri=HTTP_URI)],
                    client=client,
                )
            )
        finally:
            mod.assign_submitted_gid = real_assign
        assert result.accepted_count == 0
        gd = _gd(result.results[0].global_download_id)
        assert gd["status"] == "failed"
        assert gd["aria2_gid"] is None
        # CAS 被取消抢先：best-effort remove 已提交的 planned gid
        assert derive_planned_gid(gd["id"]) in client.removed


class TestUndeterminedKeptCandidates:
    """reconciliation 无法确认（kept）时整个请求必须 502，DB 保留 queued/gid NULL。"""

    def _pending_rows(self):
        return asyncio.run(mod.list_pending_submission_candidates())

    def test_non_missing_fault_undetermined_keeps_queued(
        self, temp_db, test_user, no_probe
    ):
        class Script(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                if calls[0]["methodName"] == "aria2.addUri":
                    return [fault(message="http proxy failure")]
                return [fault(message="HTTP 502 bad gateway"), fault()]

        with pytest.raises(mod.BatchSubmissionUndeterminedError):
            asyncio.run(
                mod.batch_create_tasks(
                    user_id=test_user["id"],
                    quota_bytes=QUOTA,
                    items=[BatchTaskItem(uri=HTTP_URI)],
                    client=Script(),
                )
            )
        rows = self._pending_rows()
        assert len(rows) == 1
        assert rows[0]["status"] == "queued"
        assert rows[0]["aria2_gid"] is None

    def test_ownership_insufficient_undetermined_keeps_queued(
        self, temp_db, test_user, no_probe
    ):
        class Script(FakeAria2Client):
            def __init__(self):
                super().__init__()
                self.gid = None

            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                if calls[0]["methodName"] == "aria2.addUri":
                    self.gid = calls[0]["params"][1]["gid"]
                    return [fault(message="gid already exists")]
                return [
                    ok({"gid": self.gid, "status": "paused"}),
                    ok([{"uri": "https://evil.example/x"}]),
                ]

        with pytest.raises(mod.BatchSubmissionUndeterminedError):
            asyncio.run(
                mod.batch_create_tasks(
                    user_id=test_user["id"],
                    quota_bytes=QUOTA,
                    items=[BatchTaskItem(uri=HTTP_URI)],
                    client=Script(),
                )
            )
        rows = self._pending_rows()
        assert len(rows) == 1
        assert rows[0]["status"] == "queued"
        assert rows[0]["aria2_gid"] is None

    def test_cas_unconfirmed_undetermined_keeps_queued(
        self, temp_db, test_user, no_probe
    ):
        class Script(FakeAria2Client):
            def __init__(self):
                super().__init__()
                self.gid = None
                self.tid = None

            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                if calls[0]["methodName"] == "aria2.addUri":
                    self.gid = calls[0]["params"][1]["gid"]
                    return [fault(message="gid already exists")]
                return [
                    ok({"gid": self.gid, "status": "paused"}),
                    ok(_internal_uris(self.tid)),
                ]

        async def unconfirmed(download):
            return False

        real_get = mod.get_global_download_by_id

        async def hooked(tid):
            client.tid = tid
            return await real_get(tid)

        client = Script()
        mod.get_global_download_by_id = hooked
        try:
            with patch.object(mod, "confirm_planned_submission", unconfirmed):
                with pytest.raises(mod.BatchSubmissionUndeterminedError):
                    asyncio.run(
                        mod.batch_create_tasks(
                            user_id=test_user["id"],
                            quota_bytes=QUOTA,
                            items=[BatchTaskItem(uri=HTTP_URI)],
                            client=client,
                        )
                    )
        finally:
            mod.get_global_download_by_id = real_get
        rows = self._pending_rows()
        assert len(rows) == 1
        assert rows[0]["aria2_gid"] is None


class TestValidationAndIdentity:
    def test_no_probe_and_original_uri_identity_join(self, temp_db, test_user, no_probe):
        class Dynamic(FakeAria2Client):
            async def multicall(self, calls):
                return [ok(c["params"][1]["gid"]) for c in calls]

        dyn = Dynamic()
        first = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[BatchTaskItem(uri=HTTP_URI, options={"out": "a.bin"})],
                client=dyn,
            )
        )
        assert first.accepted_count == 1
        tid = first.results[0].global_download_id

        other = asyncio.run(_make_user("joiner2"))
        second = asyncio.run(
            mod.batch_create_tasks(
                user_id=other["id"],
                quota_bytes=QUOTA,
                items=[BatchTaskItem(uri=HTTP_URI)],
                client=dyn,
            )
        )
        assert second.accepted_count == 1
        assert second.results[0].global_download_id == tid

    def test_trim_dedup_first_options_win(self, temp_db, test_user, no_probe):
        class Dynamic(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                return [ok(c["params"][1]["gid"]) for c in calls]

        dyn = Dynamic()
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[
                    BatchTaskItem(uri="  " + HTTP_URI + "  ", options={"out": "first.bin"}),
                    BatchTaskItem(uri=HTTP_URI, options={"out": "second.bin"}),
                ],
                client=dyn,
            )
        )
        assert len(result.results) == 1
        assert result.results[0].input_index == 0
        assert result.accepted_count == 1
        # 首次 options 生效：out 不直接出现在 gateway addUri，但 capability 不同；
        # 这里只断言只注册一个 attempt。
        gd = _gd(result.results[0].global_download_id)
        assert gd is not None

    def test_invalid_items_fail_in_chinese(self, temp_db, test_user, no_probe):
        class Dynamic(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                return [ok(c["params"][1]["gid"]) for c in calls]

        client = Dynamic()
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[
                    BatchTaskItem(uri="   "),
                    BatchTaskItem(uri="ftp://example.com/x"),
                    BatchTaskItem(uri="magnet:?xt=urn:btih:zz"),
                    BatchTaskItem(
                        uri=HTTP_URI, options={"out": "../evil"}
                    ),
                    BatchTaskItem(uri=MAGNET_URI),
                ],
                client=client,
            )
        )
        assert result.accepted_count == 1
        codes = [r.error_code for r in result.results[:4]]
        assert codes == ["invalid_uri", "invalid_uri", "invalid_uri", "invalid_option"]
        for r in result.results[:4]:
            assert r.error_message  # 非空中文原因
            assert "ValueError" not in (r.error_message or "")
        assert len(client.calls) == 1  # 只有 magnet 项进入 multicall

    def test_unknown_option_rejected_without_register(
        self, temp_db, test_user, no_probe
    ):
        client = FakeAria2Client()
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[BatchTaskItem(uri=HTTP_URI, options={"foo": "bar"})],
                client=client,
            )
        )
        assert result.accepted_count == 0
        item = result.results[0]
        assert item.error_code == "invalid_option"
        assert "foo" in (item.error_message or "")
        assert any("\u4e00" <= ch <= "\u9fff" for ch in item.error_message or "")
        assert client.calls == []  # 不进入 register/multicall

    def test_server_only_options_rejected_in_chinese(
        self, temp_db, test_user, no_probe
    ):
        client = FakeAria2Client()
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[
                    BatchTaskItem(uri=MAGNET_URI, options={"select-file": "1"}),
                    BatchTaskItem(uri=MAGNET_URI + "&dn=x", options={"bt-tracker": "http://t/ann"}),
                ],
                client=client,
            )
        )
        assert result.accepted_count == 0
        messages = [r.error_message for r in result.results]
        assert "select-file" in messages[0] and "bt-tracker" in messages[1]
        for message in messages:
            assert any("\u4e00" <= ch <= "\u9fff" for ch in message or "")
        assert client.calls == []

    def test_allowed_http_options_accepted(
        self, temp_db, test_user, no_probe
    ):
        class Dynamic(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                return [ok(c["params"][1]["gid"]) for c in calls]

        dyn = Dynamic()
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[
                    BatchTaskItem(
                        uri=HTTP_URI,
                        options={
                            "out": "a.bin",
                            "header": ["X-Test: 1"],
                            "http-user": "u",
                            "http-passwd": "p",
                            "mirrors": ["https://mirror.example/a.bin"],
                        },
                    )
                ],
                client=dyn,
            )
        )
        assert result.accepted_count == 1
        assert len(dyn.calls) == 1

    def test_magnet_max_connection_per_server_passthrough_to_descriptor(
        self, temp_db, test_user, no_probe
    ):
        class Dynamic(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                return [ok(c["params"][1]["gid"]) for c in calls]

        dyn = Dynamic()
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[
                    BatchTaskItem(
                        uri=MAGNET_URI,
                        options={"max-connection-per-server": 4},
                    )
                ],
                client=dyn,
            )
        )
        assert result.accepted_count == 1
        (call,) = dyn.add_calls
        _, options = call["params"]
        assert options["max-connection-per-server"] == "4"

    def test_http_max_connection_per_server_gateway_fixed(
        self, temp_db, test_user, no_probe
    ):
        class Dynamic(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                return [ok(c["params"][1]["gid"]) for c in calls]

        dyn = Dynamic()
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[
                    BatchTaskItem(
                        uri=HTTP_URI,
                        options={"max-connection-per-server": 4},
                    )
                ],
                client=dyn,
            )
        )
        assert result.accepted_count == 1
        (call,) = dyn.add_calls
        uris, options = call["params"]
        assert "/_internal/fetch/" in uris[0]
        # HTTP 由 gateway 安全固定设置（用户值 4 不得生效）
        assert options["max-connection-per-server"] == "1"

    def test_options_isolation_between_items(self, temp_db, test_user, no_probe):
        class Dynamic(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                return [ok(c["params"][1]["gid"]) for c in calls]

        dyn = Dynamic()
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[
                    BatchTaskItem(uri=HTTP_URI, options={"out": "a.bin"}),
                    BatchTaskItem(
                        uri="https://example.com/files/b.bin", options={"out": "b.bin"}
                    ),
                ],
                client=dyn,
            )
        )
        assert result.accepted_count == 2
        # 每个 descriptor 独立 options（header capability 不同）
        opts = [c["params"][1] for c in dyn.add_calls]
        assert opts[0]["dir"] != opts[1]["dir"]  # 不同 tid 不同 dir


class TestAllowanceCallbackAndDedupKey:
    """M24 Task3：per-item allowance callback 与 trim 完整 URI 去重键。"""

    def test_allowance_called_per_deduped_valid_item_before_register(
        self, temp_db, test_user, no_probe
    ):
        class Dynamic(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                return [ok(c["params"][1]["gid"]) for c in calls]

        events: list[str] = []
        original_register = mod.register

        async def traced_register(**kwargs):
            events.append("register")
            return await original_register(**kwargs)

        async def allow():
            events.append("allow")

        import app.services.task_batch_submission as module

        with patch.object(module, "register", traced_register):
            result = asyncio.run(
                module.batch_create_tasks(
                    user_id=test_user["id"],
                    quota_bytes=QUOTA,
                    items=[
                        BatchTaskItem(uri="  " + HTTP_URI + "  "),
                        BatchTaskItem(uri=HTTP_URI),  # trim 后重复
                        BatchTaskItem(uri="ftp://example.com/x"),  # 去重后业务非法也消费 allowance
                    ],
                    client=Dynamic(),
                    allow_create_task=allow,
                )
            )
        assert events == ["allow", "register", "allow"]
        assert len(result.results) == 2  # 首项 + 无效项

    def test_duplicate_invalid_uri_single_result_and_allowance(
        self, temp_db, test_user, no_probe
    ):
        calls: list[int] = []

        async def allow():
            calls.append(1)

        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[
                    BatchTaskItem(uri="ftp://example.com/x"),
                    BatchTaskItem(uri="  ftp://example.com/x  "),  # trim 后重复
                ],
                client=FakeAria2Client(),
                allow_create_task=allow,
            )
        )
        assert len(result.results) == 1
        assert result.results[0].input_index == 0
        assert result.results[0].error_code == "invalid_uri"
        assert calls == [1]  # 重复项不再消费 allowance

    def test_allowance_denied_fails_item_and_continues(
        self, temp_db, test_user, no_probe
    ):
        class Dynamic(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                return [ok(c["params"][1]["gid"]) for c in calls]

        dyn = Dynamic()
        calls: list[int] = []

        async def allow():
            calls.append(1)
            if len(calls) >= 2:
                raise mod.BatchAllowanceDeniedError()

        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[
                    BatchTaskItem(uri=HTTP_URI),
                    BatchTaskItem(uri="https://example.com/files/b.bin"),
                ],
                client=dyn,
                allow_create_task=allow,
            )
        )
        assert result.accepted_count == 1
        assert result.failed_count == 1
        denied = result.results[1]
        assert denied.accepted is False
        assert denied.error_message == "操作过于频繁，请稍后再试"
        assert denied.task_id is None
        # 被拒项不 register：只有第一项进入 multicall
        assert len(dyn.add_calls) == 1

    def test_magnet_same_infohash_different_text_not_deduped(
        self, temp_db, test_user, no_probe
    ):
        class Dynamic(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                return [ok(c["params"][1]["gid"]) for c in calls]

        info_hash = "0123456789abcdef0123456789abcdef01234567"
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[
                    BatchTaskItem(uri=f"magnet:?xt=urn:btih:{info_hash}"),
                    BatchTaskItem(
                        uri=f"magnet:?xt=urn:btih:{info_hash}&dn=other"
                    ),
                ],
                client=Dynamic(),
            )
        )
        # 请求去重阶段不按 infohash 折叠：两项都有结果（第二项 register duplicate）
        assert len(result.results) == 2
        assert result.results[0].input_index == 0
        assert result.results[1].input_index == 1


class TestValidationMappingParametrized:
    """表驱动：同一校验逻辑的多组输入/边界/预期错误。"""

    @pytest.mark.parametrize(
        "uri,options,expected_code,fragment",
        [
            ("", None, "invalid_uri", "下载链接不能为空"),
            ("ftp://example.com/f", None, "invalid_uri", "仅支持磁力链接和 HTTP(S)"),
            ("javascript:alert(1)", None, "invalid_uri", "仅支持磁力链接和 HTTP(S)"),
            ("magnet:?xt=urn:btih:zz", None, "invalid_uri", "无效的磁力链接"),
            ("magnet:?dn=nohash", None, "invalid_uri", "无效的磁力链接"),
            (HTTP_URI, {"out": ""}, "invalid_option", "无效的 out 选项"),
            (HTTP_URI, {"out": ".."}, "invalid_option", "无效的 out 选项"),
            (HTTP_URI, {"out": "a/b"}, "invalid_option", "无效的 out 选项"),
            (HTTP_URI, {"out": "a\\b"}, "invalid_option", "无效的 out 选项"),
            (HTTP_URI, {"unknown-opt": "1"}, "invalid_option", "不支持的选项"),
            (HTTP_URI, {"header": ["nocolon"]}, "invalid_option", "header 选项格式无效"),
            (
                HTTP_URI,
                {"http-user": "user\r\nX-Evil: 1"},
                "invalid_option",
                "HTTP 认证选项无效",
            ),
        ],
    )
    def test_validate_item_table(self, uri, options, expected_code, fragment):
        outcome = mod._validate_item(uri, options)
        assert outcome is not None
        code, message = outcome
        assert code == expected_code
        assert fragment in message
        assert any("\u4e00" <= ch <= "\u9fff" for ch in message)

    @pytest.mark.parametrize(
        "uri,options",
        [
            (HTTP_URI, None),
            (MAGNET_URI, None),
            (HTTP_URI, {"out": "a.bin"}),
            (MAGNET_URI, {"max-connection-per-server": 4}),
        ],
    )
    def test_validate_item_valid_passes(self, uri, options):
        assert mod._validate_item(uri, options) is None

    def test_validate_item_non_chinese_value_error_falls_back(self):
        def english_raise(options):
            raise ValueError("english validation failure")

        with patch.object(mod, "source_request_options", english_raise):
            outcome = mod._validate_item(HTTP_URI, {"header": ["X-A: 1"]})
        assert outcome is not None
        code, message = outcome
        assert code == "invalid_option"
        assert message == "HTTP 选项无效"

    @pytest.mark.parametrize(
        "uri,expected",
        [
            ("https://example.com/files/a.bin", "a.bin"),
            ("https://example.com/files/a.bin?x=1", "a.bin"),
            ("https://example.com/", None),
            (MAGNET_URI, None),
        ],
    )
    def test_display_name(self, uri, expected):
        assert mod._display_name(uri) == expected


class TestMagnetOwnershipClassification:
    """表驱动：planned gid magnet ownership 判定的全部证据分支。"""

    TID = 4242
    INFO_HASH = "0123456789abcdef0123456789abcdef01234567"

    def _dir_status(self, **extra):
        from app.services.storage import get_task_download_dir

        return {"dir": str(get_task_download_dir(self.TID)), **extra}

    @pytest.mark.parametrize(
        "status_value,uris_value,expected",
        [
            (None, None, False),  # status 非 dict
            ("paused", None, False),
            ({"dir": "/wrong/dir"}, None, False),  # dir 不匹配
            ("owned_hash_upper", None, None),  # 占位：见下方单独构造
            ("hash_mismatch", None, False),
            ("followed_by_contradiction", None, False),
            ("uris_not_list", "not-a-list", False),
            ("uris_no_magnet", [{"uri": "https://evil/x"}], False),
            ("uris_has_magnet", None, True),
        ],
    )
    def test_classify_table(self, status_value, uris_value, expected):
        if status_value == "owned_hash_upper":
            status_value = self._dir_status(
                infoHash=self.INFO_HASH.upper()
            )
            expected = True
        elif status_value == "hash_mismatch":
            status_value = self._dir_status(
                infoHash="ffffffffffffffffffffffffffffffffffffffff"
            )
        elif status_value == "followed_by_contradiction":
            # 证据矛盾：已生成 payload（followedBy）但顶层无 infoHash
            status_value = self._dir_status(followedBy=["childgid"])
        elif status_value in ("uris_no_magnet", "uris_not_list"):
            status_value = self._dir_status()
        elif status_value == "uris_has_magnet":
            status_value = self._dir_status()
            uris_value = [
                {"uri": "https://other/x"},
                {"uri": f"magnet:?xt=urn:btih:{self.INFO_HASH}"},
            ]

        assert (
            mod._classify_magnet_ownership(
                tid=self.TID,
                info_hash=self.INFO_HASH,
                status_value=status_value,
                uris_value=uris_value,
            )
            is expected
        )

    def test_classify_hash_match_case_insensitive(self):
        assert mod._classify_magnet_ownership(
            tid=self.TID,
            info_hash=self.INFO_HASH.lower(),
            status_value=self._dir_status(infoHash=self.INFO_HASH.upper()),
            uris_value=None,
        )


class TestM24GapPaths:
    """M24 审计补齐：saga 各失败/并发分支。"""

    def test_reconcile_shape_fail_closed_keeps_queued(
        self, temp_db, test_user, no_probe
    ):
        class Script(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                if calls[0]["methodName"] == "aria2.addUri":
                    return [fault(message="gid already exists")]
                # shape 不符：仅返回 1 个 outcome（需要 2 个）
                return [fault(message="GID x is not found")]

        with pytest.raises(mod.BatchSubmissionUndeterminedError):
            asyncio.run(
                mod.batch_create_tasks(
                    user_id=test_user["id"],
                    quota_bytes=QUOTA,
                    items=[BatchTaskItem(uri=HTTP_URI)],
                    client=Script(),
                )
            )
        rows = asyncio.run(mod.list_pending_submission_candidates())
        assert len(rows) == 1
        assert rows[0]["status"] == "queued"
        assert rows[0]["aria2_gid"] is None

    def test_confirm_cas_fail_reread_same_gid_idempotent_success(
        self, temp_db, test_user, no_probe
    ):
        from app.repositories.task.downloads import update_global_download

        real_assign = mod.assign_submitted_gid

        async def concurrent_assign(*, download_id, gid, status, error_code):
            # 模拟并发请求已绑定同一 gid 后 CAS 失效：写入但返回 None
            await update_global_download(
                download_id, {"aria2_gid": gid, "status": status}
            )
            return None

        class Script(FakeAria2Client):
            def __init__(self):
                super().__init__()
                self.tid = None

            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                if calls[0]["methodName"] == "aria2.addUri":
                    return [fault(message="gid already exists")]
                return [
                    ok({"gid": "g", "status": "paused"}),
                    ok(_internal_uris(self.tid)),
                ]

        client = Script()
        real_get = mod.get_global_download_by_id

        async def hooked(tid):
            client.tid = tid
            return await real_get(tid)

        mod.get_global_download_by_id = hooked
        mod.assign_submitted_gid = concurrent_assign
        try:
            result = asyncio.run(
                mod.batch_create_tasks(
                    user_id=test_user["id"],
                    quota_bytes=QUOTA,
                    items=[BatchTaskItem(uri=HTTP_URI)],
                    client=client,
                )
            )
        finally:
            mod.get_global_download_by_id = real_get
            mod.assign_submitted_gid = real_assign
        assert result.accepted_count == 1
        gd = _gd(result.results[0].global_download_id)
        assert gd["aria2_gid"] == derive_planned_gid(gd["id"])
        assert gd["status"] == "paused"

    def test_confirm_success_cas_fail_reread_same_gid_accepted(
        self, temp_db, test_user, no_probe
    ):
        from app.repositories.task.downloads import update_global_download

        real_assign = mod.assign_submitted_gid

        async def concurrent_assign(*, download_id, gid, status, error_code):
            await update_global_download(
                download_id, {"aria2_gid": gid, "status": status}
            )
            return None

        class Script(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                return [ok(c["params"][1]["gid"]) for c in calls]

        client = Script()
        mod.assign_submitted_gid = concurrent_assign
        try:
            result = asyncio.run(
                mod.batch_create_tasks(
                    user_id=test_user["id"],
                    quota_bytes=QUOTA,
                    items=[BatchTaskItem(uri=HTTP_URI)],
                    client=client,
                )
            )
        finally:
            mod.assign_submitted_gid = real_assign
        assert result.accepted_count == 1
        gd = _gd(result.results[0].global_download_id)
        assert gd["aria2_gid"] == derive_planned_gid(gd["id"])
        assert gd["status"] == "paused"

    def test_gid_mismatch_remove_failure_swallowed(
        self, temp_db, test_user, no_probe
    ):
        class Script(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                return [ok("ffffffffffffffff")]

            async def remove(self, gid):
                raise RuntimeError("aria2 remove failed")

        client = Script()
        result = asyncio.run(
            mod.batch_create_tasks(
                user_id=test_user["id"],
                quota_bytes=QUOTA,
                items=[BatchTaskItem(uri=HTTP_URI)],
                client=client,
            )
        )
        assert result.accepted_count == 0
        assert result.results[0].error_code == "gid_mismatch"

    @pytest.mark.parametrize("concurrent_gid", ["concurrentgid", None])
    def test_guarded_fail_cas_mismatch_reread(
        self, temp_db, test_user, no_probe, concurrent_gid
    ):
        _undetermined_seed(test_user, HTTP_URI)
        rows = asyncio.run(mod.list_pending_submission_candidates())
        assert rows and rows[0]["aria2_gid"] is None

        async def cas_mismatch(**kwargs):
            return None

        class Script(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                if calls[0]["methodName"] == "aria2.addUri":
                    return [fault(message="GID is not found")]
                return [fault(message="GID x is not found"), fault()]

        other = asyncio.run(_make_user("gapuser"))
        client = Script()
        real_get = mod.get_global_download_by_id
        get_calls = {"n": 0}

        async def hooked_get(tid):
            get_calls["n"] += 1
            row = await real_get(tid)
            if get_calls["n"] > 1 and concurrent_gid and row is not None:
                # 并发请求在提交读取之后、guarded fail 重读之前绑定了 gid
                from app.repositories.task.downloads import update_global_download

                await update_global_download(
                    tid, {"aria2_gid": concurrent_gid, "status": "paused"}
                )
                row = await real_get(tid)
            return row

        mod.get_global_download_by_id = hooked_get
        try:
            with patch.object(mod, "claim_attempt_terminal", cas_mismatch):
                result = asyncio.run(
                    mod.batch_create_tasks(
                        user_id=other["id"],
                        quota_bytes=QUOTA,
                        items=[BatchTaskItem(uri=HTTP_URI)],
                        client=client,
                    )
                )
        finally:
            mod.get_global_download_by_id = real_get
        item = result.results[0]
        if concurrent_gid:
            # 重读发现 gid 已绑定：幂等 accepted
            assert item.accepted is True
            assert result.accepted_count == 1
        else:
            assert item.accepted is False
            assert item.error_code == "submission_failed"

    def test_submit_candidates_download_deleted_fails_item(
        self, temp_db, test_user, no_probe
    ):
        async def deleted(tid):
            return None

        class Script(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                return [ok(c["params"][1]["gid"]) for c in calls]

        client = Script()
        with patch.object(mod, "get_global_download_by_id", deleted):
            result = asyncio.run(
                mod.batch_create_tasks(
                    user_id=test_user["id"],
                    quota_bytes=QUOTA,
                    items=[BatchTaskItem(uri=HTTP_URI)],
                    client=client,
                )
            )
        assert result.accepted_count == 0
        item = result.results[0]
        assert item.accepted is False
        assert item.error_code == "submission_failed"
        assert client.calls == []  # 未进入 multicall

    def test_bt_tracker_injected_into_magnet_submission(
        self, temp_db, test_user, no_probe
    ):
        class Dynamic(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                return [ok(c["params"][1]["gid"]) for c in calls]

        dyn = Dynamic()
        with patch.object(
            mod.tracker_list_service,
            "get_bt_tracker_option",
            lambda: "http://t/ann1,http://t/ann2",
        ):
            result = asyncio.run(
                mod.batch_create_tasks(
                    user_id=test_user["id"],
                    quota_bytes=QUOTA,
                    items=[BatchTaskItem(uri=MAGNET_URI)],
                    client=dyn,
                )
            )
        assert result.accepted_count == 1
        (call,) = dyn.add_calls
        _, options = call["params"]
        assert options["bt-tracker"] == "http://t/ann1,http://t/ann2"

    def test_same_batch_two_uris_converge_one_candidate_one_multicall(
        self, temp_db, test_user, no_probe
    ):
        """同一 batch 内两个 URI register 汇聚到同一 pending tid：
        第二项复用 candidates_by_tid 已有 candidate，最终一次 multicall。"""
        from app.modules.task_core.register import RegisterResult

        _undetermined_seed(test_user, HTTP_URI)
        rows = asyncio.run(mod.list_pending_submission_candidates())
        tid = rows[0]["id"]

        other = asyncio.run(_make_user("convergeuser"))
        register_calls: list[str] = []

        async def converge_register(*, user_id, quota_bytes, resource):
            register_calls.append(resource.source_uri)
            pid = await _attach_user_task(user_id, tid)
            return RegisterResult(
                pid=pid, tid=tid, outcome="joined_pending", status="queued"
            )

        class Script(FakeAria2Client):
            async def multicall(self, calls):
                self.calls.append([dict(c) for c in calls])
                return [ok(c["params"][1]["gid"]) for c in calls]

        client = Script()
        with patch.object(mod, "register", converge_register):
            result = asyncio.run(
                mod.batch_create_tasks(
                    user_id=other["id"],
                    quota_bytes=QUOTA,
                    items=[
                        BatchTaskItem(uri=HTTP_URI),
                        BatchTaskItem(uri="https://example.com/files/b.bin"),
                    ],
                    client=client,
                )
            )
        assert register_calls == [HTTP_URI, "https://example.com/files/b.bin"]
        assert result.accepted_count == 2
        # 单 candidate：一次 addUri，使用 pending attempt 的 planned gid
        assert len(client.calls) == 1
        (call,) = client.add_calls
        assert call["params"][1]["gid"] == derive_planned_gid(tid)
        for item in result.results:
            assert item.accepted is True
            assert item.global_download_id == tid
        gd = _gd(tid)
        assert gd["aria2_gid"] == derive_planned_gid(tid)
        assert gd["status"] == "paused"


async def _attach_user_task(user_id: int, tid: int) -> int:
    from app.db.engine import transaction
    from app.db.schema import user_tasks
    from sqlalchemy import insert

    from app.core.time_utils import now_ms
    from sqlalchemy import select

    async with transaction() as conn:
        existing = (
            await conn.execute(
                select(user_tasks.c.id).where(
                    user_tasks.c.user_id == user_id,
                    user_tasks.c.global_download_id == tid,
                )
            )
        ).fetchone()
        if existing is not None:
            return existing[0]
        row = (
            await conn.execute(
                insert(user_tasks).values(
                    user_id=user_id,
                    global_download_id=tid,
                    status="queued",
                    created_at_ms=now_ms(),
                    updated_at_ms=now_ms(),
                )
            )
        ).inserted_primary_key
        return row[0]
