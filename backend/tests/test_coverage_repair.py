"""Coverage supplements for app/services/repair.py."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.services import repair as rp
from app.services.storage import get_downloading_dir
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


async def _fetch(download_id: int) -> dict | None:
    from app.db.engine import transaction
    from app.db.schema import global_downloads
    from sqlalchemy import select

    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(global_downloads).where(global_downloads.c.id == download_id)
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# purge_terminal_residual_gids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_residual_gids_branches(temp_db, monkeypatch):
    rows = [
        {"id": 1, "aria2_gid": "g1"},   # claim stale → skipped
        {"id": 2, "aria2_gid": "g2"},   # cleanup stops writer
        {"id": 3, "aria2_gid": "g3"},   # cleanup fails to stop writer
        {"id": 4, "aria2_gid": "g4"},   # cleanup raises
        {"id": 5, "aria2_gid": None},   # no gid → skipped
    ]
    monkeypatch.setattr(
        rp, "list_terminal_downloads_with_residual_gid", AsyncMock(return_value=rows)
    )
    claims = {
        "g1": None,
        "g2": "claim2",
        "g3": "claim3",
        "g4": "claim4",
    }
    monkeypatch.setattr(
        rp,
        "claim_terminal_reclaim",
        AsyncMock(side_effect=lambda attempt_id, expected_gid: claims.get(expected_gid)),
    )

    async def fake_cleanup(backend, claim, log_prefix):
        if claim == "claim4":
            raise RuntimeError("rpc down")
        return SimpleNamespace(writer_stopped=claim == "claim2")

    monkeypatch.setattr(rp, "cleanup_with_claim", fake_cleanup)
    result = await rp.purge_terminal_residual_gids(make_aria2_client())
    assert result == {"found": 5, "purged": 1, "failed": 2}


# ---------------------------------------------------------------------------
# purge_terminal_download_dirs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_download_dirs_missing_dir(temp_db, monkeypatch):
    monkeypatch.setattr(
        rp, "get_downloading_dir", lambda: Path(settings.download_dir) / "nope"
    )
    assert await rp.purge_terminal_download_dirs() == {
        "found": 0,
        "purged": 0,
        "failed": 0,
        "skipped": 0,
    }


@pytest.mark.asyncio
async def test_purge_download_dirs_branches(temp_db, monkeypatch):
    dl = await create_global_download_v0(
        resource_key="http:purge-ok",
        status="failed",
        aria2_gid="g-ok",
    )
    active = await create_global_download_v0(
        resource_key="http:purge-live",
        status="active",
        aria2_gid="g-live",
    )
    broken = await create_global_download_v0(
        resource_key="http:purge-broken",
        status="failed",
        aria2_gid="g-broken",
    )
    downloading = get_downloading_dir()
    (downloading / str(dl["id"])).mkdir()
    (downloading / str(active["id"])).mkdir()
    (downloading / str(broken["id"])).mkdir()
    (downloading / "pack_x").mkdir()
    (downloading / "loose").write_bytes(b"x")

    async def fake_cleanup(download_id):
        if download_id == broken["id"]:
            raise OSError("boom")

    monkeypatch.setattr(rp, "cleanup_task_download_dir", fake_cleanup)
    result = await rp.purge_terminal_download_dirs()
    assert result["found"] == 2
    assert result["purged"] == 1
    assert result["failed"] == 1
    assert result["skipped"] == 2


# ---------------------------------------------------------------------------
# recover_completed_downloads_pending_index
# ---------------------------------------------------------------------------


def _make_pending(status="completed", **kwargs):
    return create_global_download_v0(status=status, **kwargs)


@pytest.mark.asyncio
async def test_recover_pending_index_dir_missing(temp_db):
    await _make_pending(resource_key="http:rec-missing", aria2_gid="g-m")
    result = await rp.recover_completed_downloads_pending_index(make_aria2_client())
    assert result == {"found": 1, "recovered": 0, "failed": 0, "skipped": 1}


@pytest.mark.asyncio
async def test_recover_pending_index_reopen_stale(temp_db, monkeypatch):
    dl = await _make_pending(resource_key="http:rec-stale", aria2_gid="g-s")
    (get_downloading_dir() / str(dl["id"])).mkdir()
    monkeypatch.setattr(
        rp, "reopen_completed_download_for_index_repair", AsyncMock(return_value=None)
    )
    result = await rp.recover_completed_downloads_pending_index(make_aria2_client())
    assert result["skipped"] == 1


@pytest.mark.asyncio
async def test_recover_pending_index_scan_oserror(temp_db, monkeypatch):
    dl = await _make_pending(resource_key="http:rec-oserr", aria2_gid="g-o")
    (get_downloading_dir() / str(dl["id"])).mkdir()

    def boom(*a, **k):
        raise OSError("scan fail")

    monkeypatch.setattr(Path, "rglob", boom)
    result = await rp.recover_completed_downloads_pending_index(make_aria2_client())
    assert result["failed"] == 1
    # restore put the row back to completed
    assert (await _fetch(dl["id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_recover_pending_index_empty_dir(temp_db):
    dl = await _make_pending(resource_key="http:rec-empty", aria2_gid="g-e")
    (get_downloading_dir() / str(dl["id"])).mkdir()
    result = await rp.recover_completed_downloads_pending_index(make_aria2_client())
    assert result["skipped"] == 1
    assert (await _fetch(dl["id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_recover_pending_index_success(temp_db):
    user = await create_user_v0(username="rec_ok")
    dl = await _make_pending(
        resource_key="http:rec-ok",
        aria2_gid="g-ok",
        total_bytes=0,
        size_known=False,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=dl["id"], status="active"
    )
    task_dir = get_downloading_dir() / str(dl["id"])
    task_dir.mkdir()
    payload = task_dir / "payload.bin"
    payload.write_bytes(b"recover-me")

    result = await rp.recover_completed_downloads_pending_index(make_aria2_client())
    assert result["recovered"] == 1
    row = await _fetch(dl["id"])
    assert row["status"] == "completed"
    assert row["completed_file_id"] is not None


@pytest.mark.asyncio
async def test_recover_pending_index_completion_refused(temp_db, monkeypatch):
    dl = await _make_pending(resource_key="http:rec-refuse", aria2_gid="g-r")
    task_dir = get_downloading_dir() / str(dl["id"])
    task_dir.mkdir()
    (task_dir / "payload.bin").write_bytes(b"x")

    async def incomplete(backend, download, **kwargs):
        return False

    monkeypatch.setattr(rp, "handle_v0_download_complete", incomplete)
    result = await rp.recover_completed_downloads_pending_index(make_aria2_client())
    assert result["failed"] == 1
    assert (await _fetch(dl["id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_recover_pending_index_completion_raises(temp_db, monkeypatch):
    dl = await _make_pending(resource_key="http:rec-raise", aria2_gid="g-ra")
    task_dir = get_downloading_dir() / str(dl["id"])
    task_dir.mkdir()
    (task_dir / "payload.bin").write_bytes(b"x")

    async def boom(backend, download, **kwargs):
        raise RuntimeError("completion exploded")

    monkeypatch.setattr(rp, "handle_v0_download_complete", boom)
    result = await rp.recover_completed_downloads_pending_index(make_aria2_client())
    assert result["failed"] == 1


# ---------------------------------------------------------------------------
# rebuild_active_download_accounting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebuild_accounting_branches(temp_db, monkeypatch):
    from app.domain.lifecycle import ReconcileResult
    from app.services.lifecycle import coordinator as coord_mod

    await create_global_download_v0(
        resource_key="http:acc-nogid", status="queued", aria2_gid=None
    )
    await create_global_download_v0(
        resource_key="http:acc-ok", status="active", aria2_gid="g-acc-ok"
    )
    await create_global_download_v0(
        resource_key="http:acc-fail", status="active", aria2_gid="g-acc-fail"
    )
    await create_global_download_v0(
        resource_key="http:acc-term", status="active", aria2_gid="g-acc-term"
    )
    await create_global_download_v0(
        resource_key="http:acc-wait", status="active", aria2_gid="g-acc-wait"
    )

    results = {
        "g-acc-ok": ReconcileResult.STALE,
        "g-acc-fail": RuntimeError("reconcile boom"),
        "g-acc-term": ReconcileResult.TERMINALIZED,
        "g-acc-wait": ReconcileResult.WAITING,
    }

    async def fake_reconcile(**kwargs):
        result = results[kwargs["observed_gid"]]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(coord_mod, "reconcile_attempt_signal", fake_reconcile)
    # rebuild imports the symbol inside the function body
    with patch(
        "app.services.lifecycle.coordinator.reconcile_attempt_signal", fake_reconcile
    ):
        summary = await rp.rebuild_active_download_accounting(make_aria2_client())
    assert summary == {"rebuilt": 2, "failed": 2}


# ---------------------------------------------------------------------------
# scan_and_create_stored_files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_store_dir_missing(temp_db, monkeypatch):
    monkeypatch.setattr(
        rp, "get_store_dir", lambda: Path(settings.download_dir) / "no-store"
    )
    result = await rp.scan_and_create_stored_files()
    assert result == {"found": 0, "created": 0, "unresolved": 0, "errors": []}


@pytest.mark.asyncio
async def test_scan_legacy_orphan_created(temp_db):
    # Legacy layout: store/<dir>/<hash>; canonical path matches the file name.
    from app.services.storage import get_store_dir

    payload = b"legacy-orphan"
    import hashlib

    content_hash = hashlib.sha256(payload).hexdigest()
    store = get_store_dir()
    (store / "ab").mkdir(parents=True, exist_ok=True)
    target = store / "ab" / content_hash
    target.write_bytes(payload)

    # Legacy candidates under a prefix dir never match the canonical
    # store/<hash> location, so they are reported unresolved.
    result = await rp.scan_and_create_stored_files()
    assert result["created"] == 0
    assert result["found"] == 1
    assert result["unresolved"] == 1
    assert len(result["errors"]) == 1


@pytest.mark.asyncio
async def test_scan_v2_orphan_created(temp_db):
    import hashlib

    from app.domain.content_identity import CONTENT_HASH_V2
    from app.services.storage import get_store_dir
    from app.services.storage_index import _FILE_DOMAIN, _v2_file_digest

    payload = b"v2-orphan-data"
    raw = hashlib.sha256(payload).hexdigest()
    digest = _v2_file_digest(raw)
    content_hash = f"{CONTENT_HASH_V2}:file:{digest}"
    target = get_store_dir() / "v2" / "file" / digest[:2] / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    # ignored layouts inside v2
    store = get_store_dir()
    (store / "v2" / "bogus" / "xy").mkdir(parents=True, exist_ok=True)
    (store / "v2" / "file" / "loose-file").write_bytes(b"x")

    result = await rp.scan_and_create_stored_files()
    assert result["created"] == 1
    assert result["found"] == 1


@pytest.mark.asyncio
async def test_scan_hash_mismatch_unresolved(temp_db):
    # v2 layout: name matches the canonical path but content hashes to a
    # different digest → unresolved.
    import hashlib as _hl

    from app.services.storage import get_store_dir
    from app.services.storage_index import _FILE_DOMAIN, _v2_file_digest

    payload = b"mismatched-content"
    wrong_digest = _v2_file_digest(_hl.sha256(b"other").hexdigest())
    target = get_store_dir() / "v2" / "file" / wrong_digest[:2] / wrong_digest
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)

    result = await rp.scan_and_create_stored_files()
    assert result["unresolved"] == 1
    assert result["created"] == 0


# ---------------------------------------------------------------------------
# _create_stored_file_for_path direct branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_stored_file_scan_error(temp_db, monkeypatch, tmp_path):
    async def boom(path):
        raise RuntimeError("scan fail")

    monkeypatch.setattr(rp, "scan_storage_path_async", boom)
    status = await rp._create_stored_file_for_path(tmp_path / "f", "v1:" + "a" * 64)
    assert status == "unresolved"


@pytest.mark.asyncio
async def test_create_stored_file_existing_same_path(temp_db):
    from sqlalchemy import insert

    from app.core.time_utils import now_ms
    from app.db.engine import transaction
    from app.db.schema import stored_files
    from app.services.storage import get_store_dir

    payload = b"same-path"
    import hashlib

    content_hash = hashlib.sha256(payload).hexdigest()
    path = get_store_dir() / content_hash
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    async with transaction() as conn:
        await conn.execute(
            insert(stored_files).values(
                content_hash=content_hash,
                real_path=str(path),
                size_bytes=len(payload),
                is_directory=0,
                original_name=content_hash,
                created_at_ms=now_ms(),
            )
        )
    status = await rp._create_stored_file_for_path(path, content_hash)
    assert status == "resolved"


@pytest.mark.asyncio
async def test_create_stored_file_existing_other_path(temp_db):
    from sqlalchemy import insert

    from app.core.time_utils import now_ms
    from app.db.engine import transaction
    from app.db.schema import stored_files
    from app.services.storage import get_store_dir

    payload = b"other-path"
    import hashlib

    content_hash = hashlib.sha256(payload).hexdigest()
    path = get_store_dir() / content_hash
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    async with transaction() as conn:
        await conn.execute(
            insert(stored_files).values(
                content_hash=content_hash,
                real_path=str(get_store_dir() / "elsewhere"),
                size_bytes=len(payload),
                is_directory=0,
                original_name=content_hash,
                created_at_ms=now_ms(),
            )
        )
    status = await rp._create_stored_file_for_path(path, content_hash)
    assert status == "unresolved"


@pytest.mark.asyncio
async def test_create_stored_file_race_resolved(temp_db, monkeypatch, tmp_path):
    payload = b"race"
    path = tmp_path / "race"
    path.write_bytes(payload)
    import hashlib

    content_hash = hashlib.sha256(payload).hexdigest()

    call = {"n": 0}

    from app.repositories.errors import RepositoryConflictError

    async def fake_create(values, entries):
        call["n"] += 1
        raise RepositoryConflictError("conflict")

    monkeypatch.setattr(rp, "create_stored_file_with_entries", fake_create)
    status = await rp._create_stored_file_for_path(path, content_hash)
    # no concurrent registration row exists → unresolved
    assert status == "unresolved"


@pytest.mark.asyncio
async def test_create_stored_file_race_confirmed(temp_db, monkeypatch, tmp_path):
    payload = b"race2"
    path = tmp_path / "race2"
    path.write_bytes(payload)
    import hashlib

    content_hash = hashlib.sha256(payload).hexdigest()

    async def fake_create(values, entries):
        # concurrent writer registers the exact same file first
        from sqlalchemy import insert

        from app.core.time_utils import now_ms
        from app.db.engine import transaction
        from app.db.schema import stored_files

        async with transaction() as conn:
            await conn.execute(
                insert(stored_files).values(
                    content_hash=content_hash,
                    real_path=str(path),
                    size_bytes=len(payload),
                    is_directory=0,
                    original_name="race2",
                    created_at_ms=now_ms(),
                )
            )
        raise rp.RepositoryConflictError("conflict")

    monkeypatch.setattr(rp, "create_stored_file_with_entries", fake_create)
    status = await rp._create_stored_file_for_path(path, content_hash)
    assert status == "resolved"


@pytest.mark.asyncio
async def test_create_stored_file_confirmation_fails(temp_db, monkeypatch, tmp_path):
    payload = b"race3"
    path = tmp_path / "race3"
    path.write_bytes(payload)
    import hashlib

    content_hash = hashlib.sha256(payload).hexdigest()

    from app.repositories.errors import RepositoryConflictError

    async def fake_create(values, entries):
        raise RepositoryConflictError("conflict")

    lookup = AsyncMock(side_effect=[None, RuntimeError("db down")])

    monkeypatch.setattr(rp, "create_stored_file_with_entries", fake_create)
    monkeypatch.setattr(rp, "get_stored_file_by_content_hash", lookup)
    status = await rp._create_stored_file_for_path(path, content_hash)
    assert status == "unresolved"


# ---------------------------------------------------------------------------
# purge_orphan_aria2_downloads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_orphan_aria2_downloads(temp_db):
    root = str(Path(settings.download_dir).resolve() / "downloading")
    live = await create_global_download_v0(
        resource_key="http:zomb-live", status="active", aria2_gid="g-live"
    )
    client = make_aria2_client(
        tell_active=[
            {"gid": "g-live", "dir": root},
            {"gid": "g-foreign", "dir": "/elsewhere"},
            {"gid": "", "dir": root},
            {"gid": "g-zombie", "dir": root},
        ],
        tell_waiting=[{"gid": "g-broken", "dir": root}],
        force_remove=[Exception("rpc refuse"), "OK"],
    )
    result = await rp.purge_orphan_aria2_downloads(client)
    assert result == {"found": 2, "removed": 1, "failed": 1}


@pytest.mark.asyncio
async def test_recover_pending_index_changed_but_not_completed(temp_db, monkeypatch):
    dl = await _make_pending(resource_key="http:rec-lie", aria2_gid="g-lie")
    task_dir = get_downloading_dir() / str(dl["id"])
    task_dir.mkdir()
    (task_dir / "payload.bin").write_bytes(b"x")

    async def lying_complete(backend, download, **kwargs):
        return True

    monkeypatch.setattr(rp, "handle_v0_download_complete", lying_complete)
    result = await rp.recover_completed_downloads_pending_index(make_aria2_client())
    assert result["failed"] == 1
    assert (await _fetch(dl["id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_recover_pending_index_restore_miss(temp_db, monkeypatch):
    dl = await _make_pending(resource_key="http:rec-restore", aria2_gid="g-rst")
    task_dir = get_downloading_dir() / str(dl["id"])
    task_dir.mkdir()
    (task_dir / "payload.bin").write_bytes(b"x")

    async def boom(backend, download, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(rp, "handle_v0_download_complete", boom)
    monkeypatch.setattr(
        rp, "restore_incomplete_completed_download", AsyncMock(return_value=None)
    )
    result = await rp.recover_completed_downloads_pending_index(make_aria2_client())
    assert result["failed"] == 1


@pytest.mark.asyncio
async def test_scan_skips_registered_hash_and_loose_files(temp_db):
    from sqlalchemy import insert

    from app.core.time_utils import now_ms
    from app.db.engine import transaction
    from app.db.schema import stored_files
    from app.services.storage import get_store_dir

    payload = b"registered"
    import hashlib

    content_hash = hashlib.sha256(payload).hexdigest()
    store = get_store_dir()
    path = store / "ab" / content_hash
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    (store / "loose").write_bytes(b"x")
    async with transaction() as conn:
        await conn.execute(
            insert(stored_files).values(
                content_hash=content_hash,
                real_path=str(path),
                size_bytes=len(payload),
                is_directory=0,
                original_name=content_hash,
                created_at_ms=now_ms(),
            )
        )
    result = await rp.scan_and_create_stored_files()
    assert result == {"found": 0, "created": 0, "unresolved": 0, "errors": []}


@pytest.mark.parametrize(
    "bad_digest",
    ["", "00", "a" * 63, "z" * 64, "A" * 64],
    ids=["empty", "short-hex", "odd-length", "non-hex", "uppercase"],
)
def test_content_identity_rejects_invalid_raw_digest(bad_digest: str) -> None:
    """CodeRabbit PR#8 二审：非 64 位小写十六进制不得派生 v2:file 身份。"""
    from app.services.storage_index import content_identity_from_raw_file_digest

    with pytest.raises(ValueError, match="invalid raw file digest"):
        content_identity_from_raw_file_digest(bad_digest)
