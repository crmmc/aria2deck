"""M24 Task 2 启动恢复测试：planned GID ownership、fencing、orphan 保护。"""

from __future__ import annotations

import asyncio

import pytest

from app.aria2.client import MulticallOutcome
import app.services.task_batch_submission as mod
from app.services.task_batch_submission import derive_planned_gid

HTTP_URI = "https://example.com/files/a.bin"
MAGNET_URI = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
INFOHASH = "0123456789abcdef0123456789abcdef01234567"
QUOTA = 100 * 1024 * 1024 * 1024
INTERNAL_BASE = "http://127.0.0.1:8001"


def ok(value):
    return MulticallOutcome(ok=True, result=value)


def fault(code=1, message="GID x is not found"):
    return MulticallOutcome(ok=False, fault_code=code, fault_message=message)


def internal_uris(tid, count=1):
    return [
        {"uri": f"{INTERNAL_BASE}/_internal/fetch/{tid}/{i}"}
        for i in range(1, count + 1)
    ]


class FakeAria2Client:
    def __init__(self):
        self.scripts: list[list[MulticallOutcome] | Exception] = []
        self.calls: list[list[dict]] = []
        self.removed: list[str] = []

    def enqueue(self, outcomes):
        self.scripts.append(outcomes)

    async def multicall(self, calls):
        self.calls.append([dict(c) for c in calls])
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


@pytest.fixture(autouse=True)
def fixed_internal_base(monkeypatch):
    monkeypatch.setattr(mod, "get_internal_base_url", lambda: INTERNAL_BASE)


def _register_pending(user_id: int, kind: str, uri: str, resource_key=None,
                      payload=None, options=None) -> int:
    from app.modules.task_core.register import ResourceSpec
    from app.services import task_service

    async def run():
        result = await task_service.register(
            user_id=user_id,
            quota_bytes=QUOTA,
            resource=ResourceSpec(
                resource_key=resource_key or INFOHASH,
                source_uri=uri,
                resource_kind=kind,
                source_payload=payload or uri,
                source_options=options,
            ),
        )
        return result.tid

    return asyncio.run(run())


def _gd(tid):
    from app.repositories.task.downloads import get_global_download_by_id

    return asyncio.run(get_global_download_by_id(tid))


class TestRecoverPlannedSubmissions:
    def test_no_candidates_no_rpc(self, temp_db, test_user):
        client = FakeAria2Client()
        unresolved = asyncio.run(mod.recover_planned_submissions(client))
        assert unresolved == set()
        assert client.calls == []

    def test_crash_before_rpc_leaves_stale_for_cleanup(self, temp_db, test_user):
        tid = _register_pending(test_user["id"], "http", HTTP_URI,
                                resource_key="crash-http-1")
        gid = derive_planned_gid(tid)
        client = FakeAria2Client()
        client.enqueue([fault(), fault()])
        unresolved = asyncio.run(mod.recover_planned_submissions(client))
        assert unresolved == {gid}
        gd = _gd(tid)
        assert gd["status"] == "queued" and gd["aria2_gid"] is None
        # 一次 multicall 核对
        assert len(client.calls) == 1
        # 300s stale cleanup 最终终结
        from app.repositories.task.downloads import update_global_download

        asyncio.run(
            update_global_download(tid, {"status": "failed", "error_code": "submit_timeout"})
        )

    def test_http_owned_candidate_recovered(self, temp_db, test_user):
        tid = _register_pending(test_user["id"], "http", HTTP_URI,
                                resource_key="rec-http-1")
        gid = derive_planned_gid(tid)
        client = FakeAria2Client()
        client.enqueue([ok({"gid": gid, "status": "paused"}), ok(internal_uris(tid))])
        unresolved = asyncio.run(mod.recover_planned_submissions(client))
        assert unresolved == set()
        gd = _gd(tid)
        assert gd["aria2_gid"] == gid
        assert gd["status"] == "paused"

    def test_http_ownership_rejected_keeps_unbound(self, temp_db, test_user):
        for label, uris in [
            ("external", [{"uri": "https://evil.example/x"}]),
            ("wrong_tid", internal_uris(tid=999999)),
            ("empty", []),
        ]:
            tid = _register_pending(
                test_user["id"], "http", HTTP_URI, resource_key=f"rej-{label}"
            )
            gid = derive_planned_gid(tid)
            client = FakeAria2Client()
            client.enqueue([ok({"gid": gid, "status": "paused"}), ok(uris)])
            unresolved = asyncio.run(mod.recover_planned_submissions(client))
            assert gid in unresolved, label
            gd = _gd(tid)
            assert gd["aria2_gid"] is None
            assert gd["status"] == "queued"
            assert client.removed == []  # 不删除可能属于外部的 gid

    def test_magnet_ready_hash_match_recovers(self, temp_db, test_user):
        tid = _register_pending(test_user["id"], "magnet", MAGNET_URI)
        gid = derive_planned_gid(tid)
        from app.services.storage import get_task_download_dir

        client = FakeAria2Client()
        client.enqueue(
            [
                ok(
                    {
                        "gid": gid,
                        "status": "paused",
                        "dir": str(get_task_download_dir(tid)),
                        "infoHash": INFOHASH.upper(),
                        "followedBy": [],
                    }
                ),
                fault(),
            ]
        )
        unresolved = asyncio.run(mod.recover_planned_submissions(client))
        assert unresolved == set()
        assert _gd(tid)["aria2_gid"] == gid

    def test_magnet_hash_mismatch_rejects_no_fallback(self, temp_db, test_user):
        tid = _register_pending(test_user["id"], "magnet", MAGNET_URI)
        gid = derive_planned_gid(tid)
        from app.services.storage import get_task_download_dir

        client = FakeAria2Client()
        client.enqueue(
            [
                ok(
                    {
                        "gid": gid,
                        "status": "paused",
                        "dir": str(get_task_download_dir(tid)),
                        "infoHash": "f" * 40,
                    }
                ),
                ok([{"uri": MAGNET_URI}]),  # 即使 URI 匹配也禁止 fallback
            ]
        )
        unresolved = asyncio.run(mod.recover_planned_submissions(client))
        assert unresolved == {gid}
        assert _gd(tid)["aria2_gid"] is None

    def test_magnet_metadata_not_ready_fallback_recovers(self, temp_db, test_user):
        tid = _register_pending(test_user["id"], "magnet", MAGNET_URI)
        gid = derive_planned_gid(tid)
        from app.services.storage import get_task_download_dir

        client = FakeAria2Client()
        client.enqueue(
            [
                ok(
                    {
                        "gid": gid,
                        "status": "paused",
                        "dir": str(get_task_download_dir(tid)),
                        "infoHash": "",
                        "followedBy": [],
                    }
                ),
                ok([{"uri": MAGNET_URI}]),
            ]
        )
        unresolved = asyncio.run(mod.recover_planned_submissions(client))
        assert unresolved == set()
        assert _gd(tid)["aria2_gid"] == gid

    def test_magnet_dir_mismatch_rejects(self, temp_db, test_user):
        tid = _register_pending(test_user["id"], "magnet", MAGNET_URI)
        gid = derive_planned_gid(tid)
        client = FakeAria2Client()
        client.enqueue(
            [
                ok({"gid": gid, "status": "paused", "dir": "/tmp/other",
                    "infoHash": INFOHASH}),
                ok([{"uri": MAGNET_URI}]),
            ]
        )
        unresolved = asyncio.run(mod.recover_planned_submissions(client))
        assert unresolved == {gid}
        assert _gd(tid)["aria2_gid"] is None

    def test_transport_error_fail_closed_all_unresolved(self, temp_db, test_user):
        tid = _register_pending(test_user["id"], "http", HTTP_URI,
                                resource_key="undet-1")
        gid = derive_planned_gid(tid)
        client = FakeAria2Client()
        client.enqueue(RuntimeError("timeout"))
        unresolved = asyncio.run(mod.recover_planned_submissions(client))
        assert unresolved == {gid}
        assert _gd(tid)["aria2_gid"] is None

    def test_recovery_never_raises(self, temp_db, test_user):
        tid = _register_pending(test_user["id"], "http", HTTP_URI, resource_key="boom-1")
        client = FakeAria2Client()
        client.enqueue(ValueError("bad shape"))
        unresolved = asyncio.run(mod.recover_planned_submissions(client))
        assert unresolved == {derive_planned_gid(tid)}
        assert _gd(tid)["aria2_gid"] is None


class TestFencing:
    def test_legacy_reconciliation_skips_queued_gid_null(self, temp_db, test_user):
        tid = _register_pending(test_user["id"], "http", HTTP_URI,
                                resource_key="fence-1")
        from app.services.lifecycle.repair import reconcile_legacy_http_downloads_v0

        client = FakeAria2Client()
        failed = asyncio.run(reconcile_legacy_http_downloads_v0(client))
        assert failed == 0
        gd = _gd(tid)
        assert gd["status"] == "queued"
        assert gd["aria2_gid"] is None

    def test_orphan_purge_respects_protected_gids(self, temp_db, test_user):
        tid = _register_pending(test_user["id"], "http", HTTP_URI,
                                resource_key="purge-1")
        gid = derive_planned_gid(tid)

        class Backend:
            async def tell_active(self):
                return []

            async def tell_waiting(self, start, end):
                return [
                    {
                        "gid": gid,
                        "dir": f"{_download_root()}/downloading/{tid}",
                    },
                    {"gid": "zzzzzzzzzzzzzzzz", "dir": f"{_download_root()}/downloading/x"},
                ]

            async def force_remove_gid(self, gid):
                removed.append(gid)

        removed: list[str] = []
        backend = Backend()
        from app.services.repair import purge_orphan_aria2_downloads

        result = asyncio.run(
            purge_orphan_aria2_downloads(backend, protected_gids={gid})
        )
        assert gid not in removed
        assert "zzzzzzzzzzzzzzzz" in removed
        assert result["removed"] == 1

    def test_orphan_purge_default_no_protection_still_works(self, temp_db, test_user):
        class Backend:
            async def tell_active(self):
                return []

            async def tell_waiting(self, start, end):
                return [{"gid": "aaaaaaaaaaaaaaaa",
                         "dir": f"{_download_root()}/downloading/y"}]

            async def force_remove_gid(self, gid):
                removed.append(gid)

        removed: list[str] = []
        from app.services.repair import purge_orphan_aria2_downloads

        asyncio.run(purge_orphan_aria2_downloads(Backend()))
        assert removed == ["aaaaaaaaaaaaaaaa"]


def _download_root() -> str:
    from app.core.config import settings
    from pathlib import Path

    return str(Path(settings.download_dir).resolve())
