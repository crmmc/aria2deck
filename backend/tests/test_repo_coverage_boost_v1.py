"""Repository coverage boost: auth/files/shares/storage/usage/settings + task/*."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, insert, select, update

from app.core.config import settings
from app.core.security import hash_password
from app.db.engine import transaction
from app.db.schema import (
    api_tokens,
    download_sources,
    global_downloads,
    pack_task_sources,
    pack_tasks,
    sessions,
    share_links,
    stored_files,
    user_files,
    user_storage_usage,
    user_tasks,
    users,
)
from app.repositories import files as files_repo
from app.repositories import settings as settings_repo
from app.repositories import shares as shares_repo
from app.repositories import storage as storage_repo
from app.repositories import usage as usage_repo
from app.repositories.auth import (
    AdminActorInvalidError,
    AdminMutationConflictError,
    CannotMutateSelfError,
    DuplicateCredentialError,
    DuplicateUserError,
    QuotaBelowUsageError,
    UsernamePasswordRequiredError,
    create_api_token,
    create_first_user_if_none,
    claim_due_users,
    create_session,
    create_user,
    delete_api_token,
    delete_terminal_user_tasks_for_cleanup,
    delete_user,
    delete_user_as_admin,
    get_session_user,
    list_api_tokens,
    set_rpc_secret,
    update_user,
    update_user_as_admin,
    use_api_token_digest,
)
from app.repositories.errors import RepositoryConflictError
from app.repositories.task import downloads as dl
from app.repositories.task import retention as retention_repo
from app.repositories.task import sources as sources_repo
from app.repositories.task import user_tasks as ut
from app.repositories.task.sources import detached_source_uri_placeholder
from tests.helpers_v0 import (
    create_global_download_v0,
    create_session_v0,
    create_user_file_v0,
    create_user_task_v0,
    create_user_v0,
    now_ms,
)


async def _mark_user_pending(user_id: int) -> None:
    async with transaction() as conn:
        await conn.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(pending_delete=1, updated_at_ms=now_ms())
        )


async def _mark_stored_pending(stored_file_id: int) -> None:
    async with transaction() as conn:
        await conn.execute(
            update(stored_files)
            .where(stored_files.c.id == stored_file_id)
            .values(pending_delete=1)
        )


async def _insert_share(
    *,
    share_code: str,
    owner_id: int,
    user_file_id: int,
    status: str = "active",
    expires_at_ms: int | None = None,
    max_downloads: int | None = None,
) -> None:
    async with transaction() as conn:
        await conn.execute(
            insert(share_links).values(
                share_code=share_code,
                owner_id=owner_id,
                user_file_id=user_file_id,
                status=status,
                expires_at_ms=expires_at_ms,
                max_downloads=max_downloads,
                created_at_ms=now_ms(),
            )
        )


async def _make_file(
    user_id: int, name: str, *, content_hash: str | None = None
) -> dict[str, Any]:
    digest = content_hash or f"hash_{name}"
    path = Path(settings.download_dir) / "store" / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    return await create_user_file_v0(
        user_id=user_id,
        real_path=path,
        content_hash=digest,
        display_name=name,
        size_bytes=4,
    )


# --------------------------------------------------------------------------- #
# auth.py
# --------------------------------------------------------------------------- #


async def test_create_user_duplicate_username_raises(temp_db: str) -> None:
    await create_user_v0(username="dupe")
    with pytest.raises(DuplicateUserError):
        await create_user(
            username="dupe",
            password_hash=hash_password("x"),
            is_admin=False,
            quota_bytes=1000,
            is_initial_password=False,
        )


async def test_create_first_user_integrity_error_returns_none(temp_db: str) -> None:
    existing = await create_user_v0(username="first_user")
    await _mark_user_pending(existing["id"])
    row = await create_first_user_if_none(
        username="first_user",
        password_hash=hash_password("x"),
        is_admin=True,
        quota_bytes=1000,
        is_initial_password=False,
    )
    assert row is None


async def test_update_user_field_remap_and_empty_update(temp_db: str) -> None:
    user = await create_user_v0(username="remap_user")
    row = await update_user(user["id"], quota=12345, rpc_secret_created_at=111)
    assert row is not None
    assert int(row["quota_bytes"]) == 12345
    assert int(row["rpc_secret_created_at_ms"]) == 111

    empty = await update_user(user["id"])
    assert empty is not None
    assert int(empty["quota_bytes"]) == 12345


async def test_update_user_duplicate_username_raises(temp_db: str) -> None:
    await create_user_v0(username="update_conflict_a")
    other = await create_user_v0(username="update_conflict_b")
    with pytest.raises(DuplicateUserError):
        await update_user(other["id"], username="update_conflict_a")


async def test_set_rpc_secret_duplicate_digest_raises(temp_db: str) -> None:
    user_a = await create_user_v0(username="rpc_a")
    user_b = await create_user_v0(username="rpc_b")
    assert await set_rpc_secret(user_a["id"], "digestshared", "pre", 1)
    with pytest.raises(DuplicateCredentialError):
        await set_rpc_secret(user_b["id"], "digestshared", "pre", 1)


async def test_update_user_as_admin_username_requires_password(temp_db: str) -> None:
    admin = await create_user_v0(username="adm_u1", is_admin=True)
    target = await create_user_v0(username="adm_target1")
    with pytest.raises(UsernamePasswordRequiredError):
        await update_user_as_admin(
            actor_id=admin["id"],
            user_id=target["id"],
            expected_username="adm_target1",
            username="renamed_no_password",
        )


async def test_update_user_as_admin_duplicate_username(temp_db: str) -> None:
    admin = await create_user_v0(username="adm_u2", is_admin=True)
    target = await create_user_v0(username="adm_target2")
    await create_user_v0(username="adm_taken2")
    with pytest.raises(DuplicateUserError):
        await update_user_as_admin(
            actor_id=admin["id"],
            user_id=target["id"],
            expected_username="adm_target2",
            username="adm_taken2",
            password_hash=hash_password("newpass"),
        )


async def test_update_user_as_admin_password_deletes_sessions(temp_db: str) -> None:
    admin = await create_user_v0(username="adm_u3", is_admin=True)
    target = await create_user_v0(username="adm_target3")
    await create_session_v0(target["id"], "sess-adm-3")
    row = await update_user_as_admin(
        actor_id=admin["id"],
        user_id=target["id"],
        expected_username="adm_target3",
        password_hash=hash_password("newpass"),
    )
    assert row is not None
    assert await get_session_user("sess-adm-3") is None


async def test_update_user_as_admin_invalid_actor(temp_db: str) -> None:
    not_admin = await create_user_v0(username="adm_u4_notadmin")
    target = await create_user_v0(username="adm_target4")
    with pytest.raises(AdminActorInvalidError):
        await update_user_as_admin(
            actor_id=not_admin["id"],
            user_id=target["id"],
            expected_username="adm_target4",
            password_hash=hash_password("x"),
        )


async def test_update_user_as_admin_target_missing_returns_none(temp_db: str) -> None:
    admin = await create_user_v0(username="adm_u5", is_admin=True)
    row = await update_user_as_admin(
        actor_id=admin["id"],
        user_id=999999,
        expected_username="ghost",
        password_hash=hash_password("x"),
    )
    assert row is None


async def test_update_user_as_admin_quota_below_usage(temp_db: str) -> None:
    admin = await create_user_v0(username="adm_u6", is_admin=True)
    target = await create_user_v0(username="adm_target6", quota_bytes=10_000_000)
    await usage_repo.apply_usage_delta(target["id"], used_delta=5_000_000)
    with pytest.raises(QuotaBelowUsageError):
        await update_user_as_admin(
            actor_id=admin["id"],
            user_id=target["id"],
            expected_username="adm_target6",
            quota_bytes=1000,
        )


async def test_update_user_as_admin_demote_self_raises(temp_db: str) -> None:
    admin = await create_user_v0(username="adm_u7", is_admin=True)
    with pytest.raises(CannotMutateSelfError):
        await update_user_as_admin(
            actor_id=admin["id"],
            user_id=admin["id"],
            expected_username="adm_u7",
            is_admin=False,
        )


async def test_update_user_as_admin_generic_conflict(temp_db: str) -> None:
    admin = await create_user_v0(username="adm_u8", is_admin=True)
    target = await create_user_v0(username="adm_target8")
    with pytest.raises(AdminMutationConflictError):
        await update_user_as_admin(
            actor_id=admin["id"],
            user_id=target["id"],
            expected_username="wrong_expected_name",
            password_hash=hash_password("x"),
        )


async def test_delete_user_guards(temp_db: str) -> None:
    user = await create_user_v0(username="del_guard")
    assert await delete_user(user["id"]) is False  # not pending
    await _mark_user_pending(user["id"])
    assert await delete_user(user["id"]) is True

    other = await create_user_v0(username="del_guard_ref")
    await _make_file(other["id"], "ref_file.bin")
    await _mark_user_pending(other["id"])
    assert await delete_user(other["id"]) is False  # still has user_files


async def test_delete_user_as_admin_failure_paths(temp_db: str) -> None:
    admin = await create_user_v0(username="adm_del", is_admin=True)

    # target missing
    assert await delete_user_as_admin(actor_id=admin["id"], user_id=424242) is None

    # self delete
    with pytest.raises(CannotMutateSelfError):
        await delete_user_as_admin(actor_id=admin["id"], user_id=admin["id"])

    # target already pending
    target = await create_user_v0(username="adm_del_target")
    await _mark_user_pending(target["id"])
    row = await delete_user_as_admin(actor_id=admin["id"], user_id=target["id"])
    assert row is not None
    assert int(row["id"]) == target["id"]


async def test_create_api_token_duplicate_digest(temp_db: str) -> None:
    user_a = await create_user_v0(username="tok_a")
    user_b = await create_user_v0(username="tok_b")
    await create_api_token(user_a["id"], "dup_digest", "pre", "name")
    with pytest.raises(DuplicateCredentialError):
        await create_api_token(user_b["id"], "dup_digest", "pre", "name")


async def test_use_api_token_digest_unknown_returns_none(temp_db: str) -> None:
    user = await create_user_v0(username="tok_user")
    await create_api_token(user["id"], "tok_digest_real", "pre", "name")
    assert await use_api_token_digest("no_such_digest") is None


async def test_delete_api_token(temp_db: str) -> None:
    user = await create_user_v0(username="tok_del")
    token = await create_api_token(user["id"], "tok_del_digest", "pre", None)
    other = await create_user_v0(username="tok_del_other")
    assert await delete_api_token(other["id"], token["id"]) is False
    assert await delete_api_token(user["id"], token["id"]) is True
    assert await list_api_tokens(user["id"]) == []


async def test_delete_terminal_user_tasks_active_guard(temp_db: str) -> None:
    user = await create_user_v0(username="term_guard")
    gd = await create_global_download_v0(resource_key="magnet:term-guard")
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )
    assert await delete_terminal_user_tasks_for_cleanup(user["id"]) is False


async def test_claim_due_users_lease_conflict_window(temp_db: str) -> None:
    """覆盖 claim_due_users 的租约未到期分支（lease 仍有效时不再重复认领）。"""
    user = await create_user_v0(username="claim_user")
    await _mark_user_pending(user["id"])
    now = now_ms()
    claimed = await claim_due_users(
        lease_token="lease-1",
        timestamp_ms=now,
        lease_expires_at_ms=now + 60_000,
        limit=5,
    )
    assert len(claimed) == 1
    again = await claim_due_users(
        lease_token="lease-2",
        timestamp_ms=now + 1000,
        lease_expires_at_ms=now + 61_000,
        limit=5,
    )
    assert again == []


# --------------------------------------------------------------------------- #
# settings.py / usage.py / storage.py
# --------------------------------------------------------------------------- #


async def test_update_settings_row_empty_values(temp_db: str) -> None:
    before = await settings_repo.get_settings_row()
    row = await settings_repo.update_settings_row({})
    assert row is not None
    assert int(row["id"]) == 1
    assert int(row["id"]) == int(before["id"])  # type: ignore[arg-type]


async def test_reserve_usage_over_quota_returns_none(temp_db: str) -> None:
    user = await create_user_v0(username="quota_user", quota_bytes=1000)
    row = await usage_repo.reserve_usage_bytes_if_within_quota(
        user["id"], amount=500, quota_bytes=1000
    )
    assert row is not None
    assert await usage_repo.reserve_usage_bytes_if_within_quota(
        user["id"], amount=999_999, quota_bytes=1000
    ) is None


async def test_storage_repo_file_users_and_orphan_guards(temp_db: str) -> None:
    user = await create_user_v0(username="storage_user")
    other = await create_user_v0(username="storage_user2")
    uf = await _make_file(user["id"], "orphan_guard.bin")

    assert await storage_repo.stored_file_exists(uf["stored_file_id"]) is True
    assert await storage_repo.stored_file_exists(999999) is False
    assert await storage_repo.get_stored_file(999999) is None

    users_list = await storage_repo.list_file_users(uf["stored_file_id"])
    assert len(users_list) == 1
    assert users_list[0]["username"] == "storage_user"

    with pytest.raises(ValueError):
        await storage_repo.delete_orphan_stored_file(uf["stored_file_id"])

    # content hash mismatch -> None
    assert (
        await storage_repo.delete_orphan_stored_file(
            uf["stored_file_id"], expected_content_hash="wrong"
        )
        is None
    )

    total, rows = await storage_repo.list_stored_files(
        "orphan", False, offset=0, limit=10
    )
    assert total == 1 and rows[0]["ref_count"] == 1

    await _make_file(other["id"], "orphan_guard2.bin")
    total, rows = await storage_repo.list_stored_files("", True, offset=0, limit=10)
    assert total == 0


# --------------------------------------------------------------------------- #
# shares.py
# --------------------------------------------------------------------------- #


def _share_values(owner_id: int, user_file_id: int, code: str) -> dict[str, Any]:
    return {
        "share_code": code,
        "owner_id": owner_id,
        "user_file_id": user_file_id,
        "status": "active",
        "created_at_ms": now_ms(),
    }


async def test_create_share_target_inactive(temp_db: str) -> None:
    user = await create_user_v0(username="share_inactive")
    uf = await _make_file(user["id"], "share_inactive.bin")
    await _mark_stored_pending(uf["stored_file_id"])
    with pytest.raises(shares_repo.ShareTargetInactiveError):
        await shares_repo.create_share_with_retry(
            user_file_id=uf["id"],
            timestamp_ms=now_ms(),
            max_active_shares=5,
            values_factory=lambda: _share_values(user["id"], uf["id"], "code_x1"),
            max_attempts=2,
        )


async def test_create_share_collision_then_success(temp_db: str) -> None:
    user = await create_user_v0(username="share_retry")
    uf = await _make_file(user["id"], "share_retry.bin")
    await _insert_share(
        share_code="code_exists", owner_id=user["id"], user_file_id=uf["id"]
    )
    codes = iter(["code_exists", "code_fresh"])
    share = await shares_repo.create_share_with_retry(
        user_file_id=uf["id"],
        timestamp_ms=now_ms(),
        max_active_shares=5,
        values_factory=lambda: _share_values(user["id"], uf["id"], next(codes)),
        max_attempts=3,
    )
    assert share is not None
    assert share["share_code"] == "code_fresh"


async def test_create_share_collision_exhausted(temp_db: str) -> None:
    user = await create_user_v0(username="share_exhaust")
    uf = await _make_file(user["id"], "share_exhaust.bin")
    await _insert_share(
        share_code="code_stuck", owner_id=user["id"], user_file_id=uf["id"]
    )
    with pytest.raises(shares_repo.RepositoryConflictError):
        await shares_repo.create_share_with_retry(
            user_file_id=uf["id"],
            timestamp_ms=now_ms(),
            max_active_shares=5,
            values_factory=lambda: _share_values(user["id"], uf["id"], "code_stuck"),
            max_attempts=2,
        )


async def test_share_owner_ops(temp_db: str) -> None:
    user = await create_user_v0(username="share_owner")
    other = await create_user_v0(username="share_owner2")
    uf = await _make_file(user["id"], "share_owner.bin")
    await _insert_share(
        share_code="code_owner", owner_id=user["id"], user_file_id=uf["id"]
    )

    assert await shares_repo.get_share_status_for_owner(999, user["id"]) is None
    assert await shares_repo.get_share_status_for_owner(1, other["id"]) is None

    await shares_repo.revoke_share(1, other["id"])  # no-op for wrong owner
    assert await shares_repo.get_share_status_for_owner(1, user["id"]) == "active"
    await shares_repo.revoke_share(1, user["id"])
    assert await shares_repo.get_share_status_for_owner(1, user["id"]) == "revoked"

    assert await shares_repo.delete_share(1, other["id"]) is False
    assert await shares_repo.delete_share(1, user["id"]) is True

    await _insert_share(
        share_code="code_owner2", owner_id=user["id"], user_file_id=uf["id"]
    )
    await _insert_share(
        share_code="code_owner3",
        owner_id=user["id"],
        user_file_id=uf["id"],
        status="revoked",
    )
    assert await shares_repo.revoke_all_shares(user["id"]) == 1

    # touch_share updates last_accessed_at_ms
    await _insert_share(
        share_code="code_touch", owner_id=user["id"], user_file_id=uf["id"]
    )
    async with transaction() as conn:
        share_id = (
            await conn.execute(
                select(share_links.c.id).where(share_links.c.share_code == "code_touch")
            )
        ).scalar_one()
    await shares_repo.touch_share(share_id, 12345)
    share, _ = await shares_repo.get_share_with_file("code_touch")
    assert share is not None
    assert int(share["last_accessed_at_ms"]) == 12345


# --------------------------------------------------------------------------- #
# files.py
# --------------------------------------------------------------------------- #


def _entry(path: str, parent: str, name: str, is_dir: bool) -> dict[str, Any]:
    return {
        "relative_path": path,
        "parent_path": parent,
        "name": name,
        "is_dir": 1 if is_dir else 0,
        "size_bytes": 0 if is_dir else 4,
        "sort_key": name,
    }


async def test_create_stored_file_conflict(temp_db: str) -> None:
    ts = now_ms()
    base = {
        "real_path": "/tmp/boost_conflict.bin",
        "size_bytes": 4,
        "original_name": "boost_conflict.bin",
        "created_at_ms": ts,
    }
    row, _ = await files_repo.create_stored_file_with_entries(
        {"content_hash": "boost_conflict", **base}, []
    )
    assert row["content_hash"] == "boost_conflict"
    with pytest.raises(RepositoryConflictError):
        await files_repo.create_stored_file_with_entries(
            {"content_hash": "boost_conflict", **base}, []
        )


async def test_list_stored_file_paths_and_retry_claim(temp_db: str) -> None:
    user = await create_user_v0(username="files_paths")
    uf = await _make_file(user["id"], "paths.bin")
    assert await files_repo.list_stored_file_content_hashes() == {"hash_paths.bin"}
    assert await files_repo.list_stored_file_real_paths() == {
        str(uf["real_path"])
    }

    await _mark_stored_pending(uf["stored_file_id"])
    async with transaction() as conn:
        await conn.execute(
            update(stored_files)
            .where(stored_files.c.id == uf["stored_file_id"])
            .values(delete_lease_token="lease-files")
        )
    assert (
        await files_repo.retry_claimed_stored_file_delete(
            stored_file_id=uf["stored_file_id"],
            lease_token="wrong",
            next_retry_at_ms=1,
            error="boom",
        )
        is False
    )
    assert (
        await files_repo.retry_claimed_stored_file_delete(
            stored_file_id=uf["stored_file_id"],
            lease_token="lease-files",
            next_retry_at_ms=1,
            error="boom",
        )
        is True
    )


async def test_ensure_stored_file_with_user_ref_paths(temp_db: str) -> None:
    user = await create_user_v0(username="ensure_ref")
    sid, ufid = await files_repo.ensure_stored_file_with_user_ref(
        user_id=user["id"],
        content_hash="ensure_hash",
        real_path="/tmp/ensure_hash.bin",
        size_bytes=10,
        is_directory=False,
        original_name="ensure.bin",
        entry_templates=[],
    )
    assert ufid is not None
    # existing ref -> None second time
    sid2, ufid2 = await files_repo.ensure_stored_file_with_user_ref(
        user_id=user["id"],
        content_hash="ensure_hash",
        real_path="/tmp/ensure_hash.bin",
        size_bytes=10,
        is_directory=False,
        original_name="ensure.bin",
        entry_templates=[],
    )
    assert sid2 == sid and ufid2 is None

    await _mark_stored_pending(sid)
    with pytest.raises(RepositoryConflictError):
        await files_repo.ensure_stored_file_with_user_ref(
            user_id=user["id"],
            content_hash="ensure_hash",
            real_path="/tmp/ensure_hash.bin",
            size_bytes=10,
            is_directory=False,
            original_name="ensure.bin",
            entry_templates=[],
        )

    pending_user = await create_user_v0(username="ensure_pending")
    await _mark_user_pending(pending_user["id"])
    with pytest.raises(RepositoryConflictError):
        await files_repo.ensure_stored_file_with_user_ref(
            user_id=pending_user["id"],
            content_hash="other_hash",
            real_path="/tmp/other_hash.bin",
            size_bytes=10,
            is_directory=False,
            original_name="other.bin",
            entry_templates=[],
        )


async def test_user_file_listing_search_directory_resolve(temp_db: str) -> None:
    user = await create_user_v0(username="list_user")
    ts = now_ms()
    entries = [
        _entry(".", "", ".", True),
        _entry("dir", ".", "dir", True),
        _entry("dir/a.txt", "dir", "a.txt", False),
        _entry("b.txt", ".", "b.txt", False),
    ]
    row, _ = await files_repo.create_stored_file_with_entries(
        {
            "content_hash": "entries_hash",
            "real_path": "/tmp/entries_hash",
            "size_bytes": 8,
            "original_name": "arch",
            "created_at_ms": ts,
        },
        entries,
    )
    sid = int(row["id"])
    async with transaction() as conn:
        await conn.execute(
            insert(user_files).values(
                user_id=user["id"],
                stored_file_id=sid,
                display_name="arch",
                created_at_ms=ts,
                updated_at_ms=ts,
            )
        )

    total, rows = await files_repo.list_user_file_rows(
        user["id"], offset=0, limit=10
    )
    assert total == 1 and rows[0]["content_hash"] == "entries_hash"
    assert len(await files_repo.list_all_user_file_rows(user["id"])) == 1

    hits = await files_repo.search_stored_file_entries([sid], path_prefix="dir")
    assert {h["relative_path"] for h in hits} == {"dir", "dir/a.txt"}

    parent_is_dir, children = await files_repo.directory_entries(sid, "dir")
    assert parent_is_dir is True
    assert [c["name"] for c in children] == ["a.txt"]
    is_file, empty = await files_repo.directory_entries(sid, "dir/a.txt")
    assert is_file is False and empty == []
    missing, empty2 = await files_repo.directory_entries(sid, "nope")
    assert missing is None and empty2 == []

    uf_rows = await files_repo.resolve_user_file_ids(user["id"], [rows[0]["user_file_id"], 999, rows[0]["user_file_id"]])
    assert len(uf_rows) == 1
    assert uf_rows[0]["user_file_id"] == rows[0]["user_file_id"]


async def test_delete_user_file_reference_guards(temp_db: str) -> None:
    user = await create_user_v0(username="del_ref")
    uf = await _make_file(user["id"], "del_ref.bin")

    ok, _, _ = await files_repo.delete_user_file_reference(
        user["id"],
        uf["id"],
        expected_stored_file_id=uf["stored_file_id"] + 999,
    )
    assert ok is False
    ok, _, _ = await files_repo.delete_user_file_reference(
        user["id"], uf["id"], expected_created_at_ms=123
    )
    assert ok is False
    ok, _, _ = await files_repo.delete_user_file_reference(
        user["id"], 999999
    )
    assert ok is False


async def test_delete_user_file_reference_pack_protected(temp_db: str) -> None:
    user = await create_user_v0(username="pack_protect")
    uf = await _make_file(user["id"], "pack_protect.bin")
    ts = now_ms()
    async with transaction() as conn:
        created = (
            await conn.execute(
                select(user_files.c.created_at_ms).where(user_files.c.id == uf["id"])
            )
        ).scalar_one()
        task_id = (
            await conn.execute(
                insert(pack_tasks)
                .values(
                    user_id=user["id"],
                    source_user_file_ids_json=json.dumps([uf["id"]]),
                    source_size_bytes=4,
                    reserved_bytes=0,
                    status="pending",
                    created_at_ms=ts,
                    updated_at_ms=ts,
                )
                .returning(pack_tasks.c.id)
            )
        ).scalar_one()
        await conn.execute(
            insert(pack_task_sources).values(
                task_id=task_id,
                ordinal=0,
                original_user_file_id=uf["id"],
                stored_file_id=uf["stored_file_id"],
                user_file_created_at_ms=created,
                content_hash=uf["content_hash"],
                cleanup_state="pending",
            )
        )
    with pytest.raises(files_repo.PackSourceProtectedError):
        await files_repo.delete_user_file_reference(user["id"], uf["id"])


async def test_cleanup_pack_source_reference_paths(temp_db: str) -> None:
    user = await create_user_v0(username="pack_clean")
    ts = now_ms()

    # noop: unknown task
    assert await files_repo.cleanup_pack_source_reference(424242, 0) == (
        "noop",
        [],
        None,
    )

    uf = await _make_file(user["id"], "pack_clean.bin")
    async with transaction() as conn:
        task_id = (
            await conn.execute(
                insert(pack_tasks)
                .values(
                    user_id=user["id"],
                    source_user_file_ids_json=json.dumps([uf["id"]]),
                    source_size_bytes=4,
                    reserved_bytes=0,
                    status="completed",
                    source_cleanup_pending=1,
                    output_stored_file_id=uf["stored_file_id"],
                    created_at_ms=ts,
                    updated_at_ms=ts,
                )
                .returning(pack_tasks.c.id)
            )
        ).scalar_one()
        created = (
            await conn.execute(
                select(user_files.c.created_at_ms).where(user_files.c.id == uf["id"])
            )
        ).scalar_one()
        await conn.execute(
            insert(pack_task_sources).values(
                task_id=task_id,
                ordinal=0,
                original_user_file_id=uf["id"],
                stored_file_id=uf["stored_file_id"],
                user_file_created_at_ms=created,
                content_hash=uf["content_hash"],
                cleanup_state="pending",
            )
        )

    # output == source -> retained
    assert await files_repo.cleanup_pack_source_reference(task_id, 0) == (
        "retained_output",
        [],
        None,
    )

    # identity mismatch after deleting user file
    async with transaction() as conn:
        await conn.execute(
            update(pack_task_sources)
            .where(
                pack_task_sources.c.task_id == task_id,
                pack_task_sources.c.ordinal == 0,
            )
            .values(cleanup_state="pending")
        )
        await conn.execute(
            update(pack_tasks)
            .where(pack_tasks.c.id == task_id)
            .values(output_stored_file_id=None)
        )
        await conn.execute(delete(user_files).where(user_files.c.id == uf["id"]))
    assert await files_repo.cleanup_pack_source_reference(task_id, 0) == (
        "identity_mismatch",
        [],
        None,
    )
    async with transaction() as conn:
        await conn.execute(
            delete(pack_task_sources).where(
                pack_task_sources.c.task_id == task_id,
                pack_task_sources.c.ordinal == 0,
            )
        )

    # full clean path
    uf2 = await _make_file(user["id"], "pack_clean2.bin")
    async with transaction() as conn:
        created2 = (
            await conn.execute(
                select(user_files.c.created_at_ms).where(user_files.c.id == uf2["id"])
            )
        ).scalar_one()
        await conn.execute(
            insert(pack_task_sources).values(
                task_id=task_id,
                ordinal=1,
                original_user_file_id=uf2["id"],
                stored_file_id=uf2["stored_file_id"],
                user_file_created_at_ms=created2,
                content_hash=uf2["content_hash"],
                cleanup_state="pending",
            )
        )
    outcome, _, real_path = await files_repo.cleanup_pack_source_reference(task_id, 1)
    assert outcome == "cleaned"
    assert real_path is not None

    # set real path + finish
    assert (
        await files_repo.set_pack_source_cleanup_real_path(task_id, 1, "/new/path")
        is True
    )
    assert (
        await files_repo.set_pack_source_cleanup_real_path(task_id, 1, "/new/path2")
        is True
    )
    assert (
        await files_repo.set_pack_source_cleanup_real_path(424242, 1, "/new/path")
        is False
    )
    await files_repo.finish_pack_source_physical_cleanup(task_id, 1, None)
    async with transaction() as conn:
        state = (
            await conn.execute(
                select(pack_task_sources.c.cleanup_real_path, pack_task_sources.c.cleanup_error).where(
                    pack_task_sources.c.task_id == task_id,
                    pack_task_sources.c.ordinal == 1,
                )
            )
        ).first()
    assert state is not None and state[0] is None and state[1] is None
    await files_repo.finish_pack_source_physical_cleanup(424242, 0, "boom")
    await files_repo.set_pack_source_cleanup_real_path(task_id, 1, "/x")
    await files_repo.finish_pack_source_physical_cleanup(task_id, 1, "boom")


async def test_rename_user_file_by_hash(temp_db: str) -> None:
    user = await create_user_v0(username="rename_user")
    other = await create_user_v0(username="rename_other")
    uf = await _make_file(user["id"], "rename.bin")
    assert (
        await files_repo.rename_user_file_by_hash(user["id"], "hash_rename.bin", "n1")
        is True
    )
    assert (
        await files_repo.rename_user_file_by_hash(other["id"], "hash_rename.bin", "n2")
        is False
    )
    assert (
        await files_repo.rename_user_file_by_hash(user["id"], "nope", "n3") is False
    )


# --------------------------------------------------------------------------- #
# task/sources.py
# --------------------------------------------------------------------------- #


def test_detached_source_uri_placeholder_branches() -> None:
    h40 = "a" * 40
    assert (
        detached_source_uri_placeholder(
            resource_kind="magnet",
            resource_key="magnet:x",
            bt_info_hash=h40,
            source_uri="ignored",
        )
        == f"magnet:?xt=urn:btih:{h40}"
    )
    assert (
        detached_source_uri_placeholder(
            resource_kind="magnet",
            resource_key="magnet:nohash",
            bt_info_hash=None,
            source_uri="",
        )
        == "magnet:purged"
    )
    assert (
        detached_source_uri_placeholder(
            resource_kind="torrent",
            resource_key=f"torrent:{h40}",
            bt_info_hash=None,
            source_uri="",
        )
        == f"torrent:{h40}"
    )
    assert (
        detached_source_uri_placeholder(
            resource_kind="torrent", resource_key="", bt_info_hash=None, source_uri=""
        )
        == "torrent:unknown"
    )
    assert (
        detached_source_uri_placeholder(
            resource_kind="http",
            resource_key="http:x",
            bt_info_hash=None,
            source_uri="https://example.com/f.bin",
        )
        == "https://example.com/f.bin"
    )
    assert (
        detached_source_uri_placeholder(
            resource_kind="http", resource_key="", bt_info_hash=None, source_uri=""
        )
        == "http:purged"
    )
    assert (
        detached_source_uri_placeholder(
            resource_kind="", resource_key="", bt_info_hash=None, source_uri="x" * 200
        )
        == "http:purged"
    )


async def test_create_and_get_download_source(temp_db: str) -> None:
    row = await sources_repo.create_download_source(
        {"resource_kind": "http", "payload_text": "abc"}
    )
    fetched = await sources_repo.get_download_source_by_id(row["id"])
    assert fetched is not None and fetched["payload_text"] == "abc"
    assert await sources_repo.get_download_source_by_id(999999) is None


# --------------------------------------------------------------------------- #
# task/retention.py
# --------------------------------------------------------------------------- #


async def _make_source(payload: str = "payload") -> int:
    async with transaction() as conn:
        return int(
            (
                await conn.execute(
                    insert(download_sources)
                    .values(
                        resource_kind="http",
                        payload_text=payload,
                        created_at_ms=now_ms(),
                        updated_at_ms=now_ms(),
                    )
                    .returning(download_sources.c.id)
                )
            ).scalar_one()
        )


async def test_reclaim_zero_pid_tid_early_returns(temp_db: str) -> None:
    user = await create_user_v0(username="reclaim_user")

    assert (await retention_repo.reclaim_zero_pid_tid(999999))["action"] == "none"

    live = await create_global_download_v0(resource_key="magnet:reclaim-live", status="active")
    assert (await retention_repo.reclaim_zero_pid_tid(live["id"]))["action"] == "skipped_live"

    gd = await create_global_download_v0(resource_key="magnet:reclaim-pids", status="failed")
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="failed"
    )
    assert (await retention_repo.reclaim_zero_pid_tid(gd["id"]))["action"] == "skipped_has_pids"


async def test_reclaim_completed_shell_with_source_gc(temp_db: str) -> None:
    user = await create_user_v0(username="reclaim_shell")
    source_id = await _make_source()
    uf = await _make_file(user["id"], "shell.bin")
    gd = await create_global_download_v0(
        resource_key="magnet:reclaim-shell",
        status="completed",
        completed_file_id=uf["stored_file_id"],
    )
    async with transaction() as conn:
        await conn.execute(
            update(global_downloads)
            .where(global_downloads.c.id == gd["id"])
            .values(source_id=source_id)
        )
    result = await retention_repo.reclaim_zero_pid_tid(gd["id"])
    assert result["action"] == "kept_completed_shell"
    assert result["source_gc"] is True
    row = await dl.get_global_download_by_id(gd["id"])
    assert row is not None and row["source_id"] is None


async def test_soft_expire_due_history_branches(temp_db: str) -> None:
    user = await create_user_v0(username="soft_expire")
    now = now_ms()
    cutoff = now - 1000

    # pid terminal+due, tid live -> skipped_live
    live = await create_global_download_v0(resource_key="magnet:se-live", status="active")
    await create_user_task_v0(
        user_id=user["id"], global_download_id=live["id"], status="failed"
    )
    async with transaction() as conn:
        await conn.execute(
            update(user_tasks)
            .where(
                user_tasks.c.user_id == user["id"],
                user_tasks.c.global_download_id == live["id"],
            )
            .values(finished_at_ms=now - 10_000)
        )

    # tid terminal with one due pid + one fresh pid -> detach skipped (unexpired)
    user2 = await create_user_v0(username="soft_expire2")
    mixed = await create_global_download_v0(resource_key="magnet:se-mixed", status="failed")
    await create_user_task_v0(
        user_id=user["id"], global_download_id=mixed["id"], status="failed"
    )
    fresh = await create_user_task_v0(
        user_id=user2["id"], global_download_id=mixed["id"], status="completed"
    )
    assert fresh["id"]
    async with transaction() as conn:
        await conn.execute(
            update(user_tasks)
            .where(user_tasks.c.global_download_id == live["id"])
            .values(finished_at_ms=now - 10_000)
        )
        await conn.execute(
            update(user_tasks)
            .where(user_tasks.c.global_download_id == mixed["id"])
            .values(
                finished_at_ms=now - 10_000,
                updated_at_ms=now - 10_000,
            )
        )
        await conn.execute(
            update(user_tasks)
            .where(user_tasks.c.id == fresh["id"])
            .values(finished_at_ms=now, updated_at_ms=now)
        )

    result = await retention_repo.soft_expire_due_history(
        cutoff_ms=cutoff, now=now
    )
    assert result["skipped_live"] >= 1
    assert result["detached_source_tids"] == 0

    # make the fresh pid due too -> detach tid + gc source
    source_id = await _make_source()
    async with transaction() as conn:
        await conn.execute(
            update(user_tasks)
            .where(user_tasks.c.id == fresh["id"])
            .values(finished_at_ms=now - 10_000, updated_at_ms=now - 10_000)
        )
        await conn.execute(
            update(global_downloads)
            .where(global_downloads.c.id == mixed["id"])
            .values(source_id=source_id)
        )
    result = await retention_repo.soft_expire_due_history(
        cutoff_ms=cutoff, now=now
    )
    assert result["detached_source_tids"] == 1
    assert result["gcs_sources"] == 1


# --------------------------------------------------------------------------- #
# task/downloads.py
# --------------------------------------------------------------------------- #


async def test_strict_adjust_usage_reserved_guards(temp_db: str) -> None:
    user = await create_user_v0(username="strict_adj", quota_bytes=1000)
    async with transaction() as conn:
        with pytest.raises(ValueError):
            await dl._strict_adjust_usage_reserved(
                conn, user_id=user["id"], delta=10, quota_bytes=None, timestamp=now_ms()
            )
        with pytest.raises(RepositoryConflictError):
            await dl._strict_adjust_usage_reserved(
                conn, user_id=user["id"], delta=-10, timestamp=now_ms()
            )
        assert await dl._strict_adjust_usage_reserved(
            conn, user_id=user["id"], delta=0, timestamp=now_ms()
        )


async def test_resize_active_task_pending_user(temp_db: str) -> None:
    user = await create_user_v0(username="resize_pending")
    gd = await create_global_download_v0(resource_key="magnet:resize-pending")
    task = await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )
    await _mark_user_pending(user["id"])
    async with transaction() as conn:
        assert (
            await dl._resize_active_task(
                conn, task, target_bytes=100, timestamp=now_ms()
            )
            is False
        )


async def test_reconcile_download_size_no_subscribers(temp_db: str) -> None:
    user = await create_user_v0(username="reconcile_small", quota_bytes=1000)
    gd = await create_global_download_v0(
        resource_key="magnet:reconcile-small", status="active", aria2_gid="gidrc1"
    )
    task = await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )
    assert task["id"]
    result = await dl.reconcile_download_size(
        download_id=gd["id"],
        expected_gid="gidrc1",
        candidate_bytes=500_000,
        completed_bytes=0,
        size_limit_bytes=10_000_000,
        disk_available_bytes=10_000_000,
    )
    assert result["outcome"] == "no_subscribers"
    row = await dl.get_global_download_by_id(gd["id"])
    assert row is not None and row["status"] == "cancelled"
    task_row = await ut.get_user_task(user["id"], gd["id"])
    assert task_row is not None and task_row["status"] == "failed"


async def test_create_global_download_duplicate_live_resource(temp_db: str) -> None:
    await dl.create_global_download(
        {
            "resource_key": "http:dup-live",
            "resource_kind": "http",
            "source_uri": "https://example.com/dup",
        }
    )
    with pytest.raises(RepositoryConflictError):
        await dl.create_global_download(
            {
                "resource_key": "http:dup-live",
                "resource_kind": "http",
                "source_uri": "https://example.com/dup",
            }
        )


async def test_claim_submitted_gid_for_failure_paths(temp_db: str) -> None:
    user = await create_user_v0(username="claim_fail")
    gd = await create_global_download_v0(
        resource_key="magnet:claim-fail", status="queued"
    )
    task = await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="queued"
    )
    await usage_repo.apply_usage_delta(user["id"], reserved_delta=0)

    row = await dl.claim_submitted_gid_for_failure(
        download_id=gd["id"], gid="gidcf1", message="submit exploded"
    )
    assert row is not None and row["status"] == "failed"
    task_row = await ut.get_user_task(user["id"], gd["id"])
    assert task_row is not None and task_row["status"] == "failed"

    gd2 = await create_global_download_v0(
        resource_key="magnet:claim-fail-miss", status="queued"
    )
    assert (
        await dl.claim_submitted_gid_for_failure(
            download_id=gd2["id"],
            gid="gidcf2",
            message="x",
        )
        is not None
    )
    # now terminal -> miss
    assert (
        await dl.claim_submitted_gid_for_failure(
            download_id=gd2["id"], gid="gidcf3", message="x"
        )
        is None
    )


async def test_update_global_download_empty_values(temp_db: str) -> None:
    gd = await create_global_download_v0(resource_key="magnet:upd-empty")
    row = await dl.update_global_download(gd["id"], {})
    assert row is not None and int(row["id"]) == gd["id"]
    assert await dl.update_global_download(999999, {}) is None


async def test_guarded_updates_empty_values(temp_db: str) -> None:
    gd = await create_global_download_v0(resource_key="magnet:guarded-empty")
    assert (
        await dl.guarded_update_global_download(
            gd["id"], {}, expected_gid="g1", return_row=False
        )
        is False
    )
    assert (
        await dl.guarded_update_global_download(
            gd["id"], {}, expected_gid="g1", return_row=True
        )
        is None
    )
    assert (
        await dl.guarded_update_download_and_active_user_tasks(
            gd["id"], {}, expected_gid="g1"
        )
        is None
    )


async def _completed_attempt_fixture(user_id: int, size: int) -> tuple[int, int]:
    gd = await create_global_download_v0(
        resource_key="magnet:complete-attempt",
        status="active",
        aria2_gid="gidca1",
        total_bytes=size,
    )
    await create_user_task_v0(
        user_id=user_id,
        global_download_id=gd["id"],
        status="active",
        reserved_bytes=size,
    )
    await usage_repo.apply_usage_delta(user_id, reserved_delta=size)
    path = Path(settings.download_dir) / "store" / "complete_attempt_hash"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    ts = now_ms()
    async with transaction() as conn:
        stored_file_id = int(
            (
                await conn.execute(
                    insert(stored_files)
                    .values(
                        content_hash="complete_attempt_hash",
                        real_path=str(path),
                        size_bytes=size,
                        original_name="ca.bin",
                        created_at_ms=ts,
                    )
                    .returning(stored_files.c.id)
                )
            ).scalar_one()
        )
    return gd["id"], stored_file_id


async def test_complete_attempt_cas_miss(temp_db: str) -> None:
    user = await create_user_v0(username="ca_miss")
    gd_id, stored_id = await _completed_attempt_fixture(user["id"], 4)
    assert (
        await dl.complete_attempt(
            attempt_id=gd_id,
            expected_gid="wrong-gid",
            stored_file_id=stored_id,
            size_bytes=4,
            original_name="ca.bin",
            completed_at_ms=now_ms(),
        )
        is None
    )


async def test_complete_attempt_usage_drift(temp_db: str) -> None:
    user = await create_user_v0(username="ca_drift")
    gd_id, stored_id = await _completed_attempt_fixture(user["id"], 4)
    async with transaction() as conn:
        await conn.execute(
            delete(user_storage_usage).where(
                user_storage_usage.c.user_id == user["id"]
            )
        )
    with pytest.raises(RepositoryConflictError):
        await dl.complete_attempt(
            attempt_id=gd_id,
            expected_gid="gidca1",
            stored_file_id=stored_id,
            size_bytes=4,
            original_name="ca.bin",
            completed_at_ms=now_ms(),
        )


async def test_reopen_and_restore_incomplete_download(temp_db: str) -> None:
    user = await create_user_v0(username="reopen_user")
    gd = await create_global_download_v0(
        resource_key="magnet:reopen-restore", status="completed"
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="completed"
    )
    assert await dl.reopen_completed_download_for_index_repair(424242) is None
    row = await dl.reopen_completed_download_for_index_repair(
        gd["id"], recovery_gid="gidrr1"
    )
    assert row is not None and row["status"] == "active"

    assert await dl.restore_incomplete_completed_download(424242, aria2_gid=None) is None
    restored = await dl.restore_incomplete_completed_download(
        gd["id"], aria2_gid="gidrr1"
    )
    assert restored is not None and restored["status"] == "completed"


# --------------------------------------------------------------------------- #
# task/user_tasks.py
# --------------------------------------------------------------------------- #


def test_download_admission_error_messages() -> None:
    assert str(ut.DownloadAdmissionError("quota")) == "quota exceeded"
    assert str(ut.DownloadAdmissionError("disk full")) == "disk full"


async def test_list_user_tasks_page_invalid_and_filters(temp_db: str) -> None:
    user = await create_user_v0(username="page_user")
    with pytest.raises(ValueError):
        await ut.list_user_tasks_page(user["id"], page=0, page_size=10)

    gd_active = await create_global_download_v0(
        resource_key="magnet:page-active", status="active", aria2_gid="gidpg1"
    )
    gd_done = await create_global_download_v0(
        resource_key="magnet:page-done", status="completed"
    )
    gd_fail = await create_global_download_v0(
        resource_key="magnet:page-fail", status="failed"
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd_active["id"], status="active"
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd_done["id"], status="completed"
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd_fail["id"], status="failed"
    )

    rows, total = await ut.list_user_tasks_page(user["id"], page=1, page_size=10)
    assert total == 3
    rows, total = await ut.list_user_tasks_page(
        user["id"], page=1, page_size=10, status_filter="active"
    )
    assert total == 1 and rows[0]["aria2_gid"] == "gidpg1"
    rows, total = await ut.list_user_tasks_page(
        user["id"], page=1, page_size=10, status_filter="complete"
    )
    assert total == 1 and rows[0]["global_download_id"] == gd_done["id"]
    rows, total = await ut.list_user_tasks_page(
        user["id"], page=1, page_size=10, status_filter="error"
    )
    assert total == 1 and rows[0]["global_download_id"] == gd_fail["id"]
    rows, total = await ut.list_user_tasks_page(
        user["id"], page=1, page_size=10, statuses=("failed",)
    )
    assert total == 1
    rows, total = await ut.list_user_tasks_page(
        user["id"], page=1, page_size=10, status_filter="current"
    )
    assert total == 1

    filtered = await ut.list_user_tasks_for_download(
        gd_active["id"], statuses=("active",)
    )
    assert len(filtered) == 1
    assert await ut.list_user_tasks_for_download(gd_active["id"], statuses=("failed",)) == []


async def test_create_user_task_conflict(temp_db: str) -> None:
    user = await create_user_v0(username="ut_conflict")
    gd = await create_global_download_v0(resource_key="magnet:ut-conflict")
    await ut.create_user_task(
        {"user_id": user["id"], "global_download_id": gd["id"]}
    )
    with pytest.raises(RepositoryConflictError):
        await ut.create_user_task(
            {"user_id": user["id"], "global_download_id": gd["id"]}
        )


async def test_update_active_user_tasks_variants(temp_db: str) -> None:
    user = await create_user_v0(username="ut_update")
    gd = await create_global_download_v0(
        resource_key="magnet:ut-update", status="active", aria2_gid="giduu1"
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status="active",
        display_name="magnet:placeholder",
    )
    assert task["id"]

    await ut.update_active_user_tasks(
        gd["id"], expected_gid="giduu1", status="paused"
    )
    row = await ut.get_user_task(user["id"], gd["id"])
    assert row is not None and row["status"] == "paused"

    # magnet: placeholder name gets replaced
    await ut.update_active_user_tasks(
        gd["id"], expected_gid="giduu1", display_name="real.bin"
    )
    row = await ut.get_user_task(user["id"], gd["id"])
    assert row is not None and row["display_name"] == "real.bin"

    # non-refreshable name only replaced with force
    await ut.update_active_user_tasks(
        gd["id"], expected_gid="giduu1", display_name="not-applied.bin"
    )
    row = await ut.get_user_task(user["id"], gd["id"])
    assert row is not None and row["display_name"] == "real.bin"
    await ut.update_active_user_tasks(
        gd["id"], expected_gid="giduu1", display_name="forced.bin", force_display_name=True
    )
    row = await ut.get_user_task(user["id"], gd["id"])
    assert row is not None and row["display_name"] == "forced.bin"

    # wrong gid -> no change
    await ut.update_active_user_tasks(
        gd["id"], expected_gid="wrong", status="waiting"
    )
    row = await ut.get_user_task(user["id"], gd["id"])
    assert row is not None and row["status"] == "paused"


async def test_attach_completed_file_paths(temp_db: str) -> None:
    owner = await create_user_v0(username="attach_owner", quota_bytes=1_000_000)
    other = await create_user_v0(username="attach_other", quota_bytes=2)
    path = Path(settings.download_dir) / "store" / "attach_hash"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"abcd")
    ts = now_ms()
    async with transaction() as conn:
        stored_file_id = int(
            (
                await conn.execute(
                    insert(stored_files)
                    .values(
                        content_hash="attach_hash",
                        real_path=str(path),
                        size_bytes=4,
                        original_name="att.bin",
                        created_at_ms=ts,
                    )
                    .returning(stored_files.c.id)
                )
            ).scalar_one()
        )
    gd = await create_global_download_v0(
        resource_key="magnet:attach", status="active", aria2_gid="gidat1"
    )

    # no existing task -> inserts a completed task
    row = await ut.attach_completed_file_to_user(
        user_id=owner["id"],
        quota_bytes=1_000_000,
        global_download_id=gd["id"],
        stored_file_id=stored_file_id,
        size_bytes=4,
        display_name="att.bin",
        finished_at_ms=ts,
    )
    assert row["status"] == "completed"

    # quota exceeded for a fresh user file
    gd2 = await create_global_download_v0(
        resource_key="magnet:attach2", status="active", aria2_gid="gidat2"
    )
    with pytest.raises(ValueError):
        await ut.attach_completed_file_to_user(
            user_id=other["id"],
            quota_bytes=100,
            global_download_id=gd2["id"],
            stored_file_id=stored_file_id,
            size_bytes=4,
            display_name="att.bin",
            finished_at_ms=ts,
        )

    # user pending -> conflict
    await _mark_user_pending(other["id"])
    with pytest.raises(RepositoryConflictError):
        await ut.attach_completed_file_to_user(
            user_id=other["id"],
            quota_bytes=1_000_000,
            global_download_id=gd2["id"],
            stored_file_id=stored_file_id,
            size_bytes=4,
            display_name="att.bin",
            finished_at_ms=ts,
        )


async def test_attach_completed_file_releases_reservation(temp_db: str) -> None:
    user = await create_user_v0(username="attach_reserve", quota_bytes=1_000_000)
    path = Path(settings.download_dir) / "store" / "attach_res_hash"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"abcd")
    ts = now_ms()
    async with transaction() as conn:
        stored_file_id = int(
            (
                await conn.execute(
                    insert(stored_files)
                    .values(
                        content_hash="attach_res_hash",
                        real_path=str(path),
                        size_bytes=4,
                        original_name="atr.bin",
                        created_at_ms=ts,
                    )
                    .returning(stored_files.c.id)
                )
            ).scalar_one()
        )
    gd = await create_global_download_v0(
        resource_key="magnet:attach-res", status="active", aria2_gid="gidar1"
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status="active",
        reserved_bytes=8,
    )
    await usage_repo.apply_usage_delta(user["id"], reserved_delta=8)
    row = await ut.attach_completed_file_to_user(
        user_id=user["id"],
        quota_bytes=1_000_000,
        global_download_id=gd["id"],
        stored_file_id=stored_file_id,
        size_bytes=4,
        display_name="atr.bin",
        finished_at_ms=ts,
    )
    assert row["status"] == "completed"
    usage = await usage_repo.get_usage_row(user["id"])
    assert int(usage["reserved_bytes"]) == 0
    assert int(usage["used_bytes"]) == 4


async def test_complete_active_user_tasks_miss(temp_db: str) -> None:
    user = await create_user_v0(username="catf_miss")
    gd = await create_global_download_v0(
        resource_key="magnet:catf-miss", status="active", aria2_gid="gidcm1"
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )
    assert (
        await ut.complete_active_user_tasks_for_stored_file(
            global_download_id=gd["id"],
            expected_gid="wrong",
            stored_file_id=1,
            size_bytes=4,
            original_name="x",
            completed_at_ms=now_ms(),
        )
        is None
    )


async def test_repair_completed_download_with_stored_file(temp_db: str) -> None:
    user = await create_user_v0(username="repair_user", quota_bytes=1_000_000)
    size = 4
    path = Path(settings.download_dir) / "store" / "repair_hash"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"abcd")
    ts = now_ms()
    async with transaction() as conn:
        stored_file_id = int(
            (
                await conn.execute(
                    insert(stored_files)
                    .values(
                        content_hash="repair_hash",
                        real_path=str(path),
                        size_bytes=size,
                        original_name="rep.bin",
                        created_at_ms=ts,
                    )
                    .returning(stored_files.c.id)
                )
            ).scalar_one()
        )
    gd = await create_global_download_v0(resource_key="magnet:repair", status="completed")
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status="active",
        reserved_bytes=size,
    )
    await usage_repo.apply_usage_delta(user["id"], reserved_delta=size)

    assert (
        await ut.repair_completed_download_with_stored_file(
            global_download_id=gd["id"],
            expected_gid="nomatch",
            stored_file_id=stored_file_id,
            size_bytes=size,
            original_name="rep.bin",
            completed_at_ms=ts,
        )
        is False
    )
    assert (
        await ut.repair_completed_download_with_stored_file(
            global_download_id=gd["id"],
            expected_gid=None,
            stored_file_id=stored_file_id,
            size_bytes=size,
            original_name="rep.bin",
            completed_at_ms=ts,
        )
        is True
    )
    task = await ut.get_user_task(user["id"], gd["id"])
    assert task is not None and task["status"] == "completed"


async def test_mark_global_download_failed_paths(temp_db: str) -> None:
    user = await create_user_v0(username="mark_fail")
    gd = await create_global_download_v0(
        resource_key="magnet:mark-fail", status="active", aria2_gid="gidmf1"
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status="active",
        reserved_bytes=6,
    )
    assert task["id"]
    await usage_repo.apply_usage_delta(user["id"], reserved_delta=6)

    assert (
        await ut.mark_global_download_failed(
            gd["id"], expected_gid="wrong", message="boom"
        )
        is None
    )
    row = await ut.mark_global_download_failed(
        gd["id"], expected_gid="gidmf1", message="boom", error_code="rpc_error"
    )
    assert row is not None and row["status"] == "failed"
    task = await ut.get_user_task(user["id"], gd["id"])
    assert task is not None and task["status"] == "failed"
    usage = await usage_repo.get_usage_row(user["id"])
    assert int(usage["reserved_bytes"]) == 0


async def test_cancel_active_user_task_paths(temp_db: str) -> None:
    user = await create_user_v0(username="cancel_u")
    other = await create_user_v0(username="cancel_o")
    gd = await create_global_download_v0(
        resource_key="magnet:cancel-1", status="active", aria2_gid="gidcn1"
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status="active",
        reserved_bytes=5,
    )
    await usage_repo.apply_usage_delta(user["id"], reserved_delta=5)
    await create_user_task_v0(
        user_id=other["id"], global_download_id=gd["id"], status="active"
    )

    # terminal task -> None
    gd2 = await create_global_download_v0(resource_key="magnet:cancel-2", status="failed")
    term = await create_user_task_v0(
        user_id=user["id"], global_download_id=gd2["id"], status="failed"
    )
    assert (
        await ut.cancel_active_user_task(
            user["id"], term["id"], error_message="x", finished_at_ms=now_ms()
        )
        is None
    )

    # cancel with remaining subscriber -> global stays active
    row = await ut.cancel_active_user_task(
        user["id"], task["id"], error_message="cancel", finished_at_ms=now_ms()
    )
    assert row is not None and row["status"] == "cancelled"
    gd_row = await dl.get_global_download_by_id(gd["id"])
    assert gd_row is not None and gd_row["status"] == "active"

    # last subscriber -> global cancelled
    other_task = await ut.get_user_task(other["id"], gd["id"])
    assert other_task is not None
    row = await ut.cancel_active_user_task(
        other["id"], other_task["id"], error_message="cancel", finished_at_ms=now_ms()
    )
    assert row is not None
    gd_row = await dl.get_global_download_by_id(gd["id"])
    assert gd_row is not None and gd_row["status"] == "cancelled"


async def test_cancel_user_task_and_maybe_claim_attempt_paths(temp_db: str) -> None:
    user = await create_user_v0(username="claim_cancel")
    other = await create_user_v0(username="claim_cancel2")

    # terminal task -> (None, None)
    gd0 = await create_global_download_v0(resource_key="magnet:cc0", status="failed")
    term = await create_user_task_v0(
        user_id=user["id"], global_download_id=gd0["id"], status="failed"
    )
    assert await ut.cancel_user_task_and_maybe_claim_attempt(
        user_id=user["id"], user_task_id=term["id"], expected_gid=None
    ) == (None, None)

    # two subscribers: cancel one -> no claim
    gd = await create_global_download_v0(
        resource_key="magnet:cc1", status="active", aria2_gid="gidcc1"
    )
    t1 = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status="active",
        reserved_bytes=3,
    )
    await create_user_task_v0(
        user_id=other["id"], global_download_id=gd["id"], status="active"
    )
    await usage_repo.apply_usage_delta(user["id"], reserved_delta=3)
    updated, claim = await ut.cancel_user_task_and_maybe_claim_attempt(
        user_id=user["id"], user_task_id=t1["id"], expected_gid="gidcc1"
    )
    assert updated is not None and claim is None

    # last subscriber with matching gid -> claim returned
    t2 = await ut.get_user_task(other["id"], gd["id"])
    assert t2 is not None
    updated, claim = await ut.cancel_user_task_and_maybe_claim_attempt(
        user_id=other["id"],
        user_task_id=t2["id"],
        expected_gid="nomatch",
    )
    assert updated is not None and claim is None
    gd_row = await dl.get_global_download_by_id(gd["id"])
    assert gd_row is not None and gd_row["status"] == "active"

    # single-subscriber cancellation with matching gid
    gd2 = await create_global_download_v0(
        resource_key="magnet:cc2", status="active", aria2_gid="gidcc2"
    )
    t3 = await create_user_task_v0(
        user_id=user["id"], global_download_id=gd2["id"], status="active"
    )
    updated, claim = await ut.cancel_user_task_and_maybe_claim_attempt(
        user_id=user["id"], user_task_id=t3["id"], expected_gid="gidcc2"
    )
    assert updated is not None and updated["status"] == "cancelled"
    assert claim is not None and claim.attempt_id == gd2["id"]


async def test_delete_and_clear_terminal_user_tasks(temp_db: str) -> None:
    user = await create_user_v0(username="del_terminal")
    gd = await create_global_download_v0(resource_key="magnet:del-term", status="failed")
    task = await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="failed"
    )
    gd_active = await create_global_download_v0(
        resource_key="magnet:del-term-live", status="active"
    )
    live_task = await create_user_task_v0(
        user_id=user["id"], global_download_id=gd_active["id"], status="active"
    )
    assert live_task["id"]

    assert await ut.delete_terminal_user_task(user["id"], 999999) is None
    assert await ut.delete_terminal_user_task(user["id"], task["id"]) == gd["id"]
    assert await ut.delete_terminal_user_task(user["id"], live_task["id"]) is None

    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="cancelled"
    )
    cleared = await ut.clear_terminal_user_tasks(user["id"])
    assert cleared == [gd["id"]]
    assert await ut.delete_all_terminal_user_tasks(user["id"]) == []


# --------------------------------------------------------------------------- #
# Round 2: remaining branch gaps
# --------------------------------------------------------------------------- #


async def test_update_user_as_admin_demote_conflict_via_wrong_username(
    temp_db: str,
) -> None:
    admin_a = await create_user_v0(username="adm_demote_a", is_admin=True)
    admin_b = await create_user_v0(username="adm_demote_b", is_admin=True)
    with pytest.raises(AdminMutationConflictError):
        await update_user_as_admin(
            actor_id=admin_a["id"],
            user_id=admin_b["id"],
            expected_username="wrong_username",
            is_admin=False,
        )


async def test_ensure_stored_file_with_entries(temp_db: str) -> None:
    user = await create_user_v0(username="ensure_entries")
    sid, _ = await files_repo.ensure_stored_file_with_user_ref(
        user_id=user["id"],
        content_hash="ensure_entries_hash",
        real_path="/tmp/ensure_entries_hash",
        size_bytes=8,
        is_directory=True,
        original_name="dir",
        entry_templates=[
            _entry(".", "", ".", True),
            _entry("inner.txt", ".", "inner.txt", False),
        ],
    )
    _, children = await files_repo.directory_entries(sid, ".")
    assert [c["name"] for c in children] == ["inner.txt"]


async def test_reconcile_download_size_max_task_size(temp_db: str) -> None:
    user = await create_user_v0(username="reconcile_max", quota_bytes=10_000_000)
    gd = await create_global_download_v0(
        resource_key="magnet:reconcile-max",
        status="active",
        aria2_gid="gidrm1",
        size_limit_bytes=100,
    )
    task = await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )
    assert task["id"]
    result = await dl.reconcile_download_size(
        download_id=gd["id"],
        expected_gid="gidrm1",
        candidate_bytes=500,
        completed_bytes=0,
        size_limit_bytes=10_000_000,
        disk_available_bytes=10_000_000,
    )
    assert result["outcome"] == "max_task_size"
    row = await dl.get_global_download_by_id(gd["id"])
    assert row is not None and row["status"] == "failed"


async def test_complete_attempt_reservation_mismatch(temp_db: str) -> None:
    user = await create_user_v0(username="ca_mismatch")
    size = 4
    gd = await create_global_download_v0(
        resource_key="magnet:ca-mismatch",
        status="active",
        aria2_gid="gidcm2",
        total_bytes=size,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status="active",
        reserved_bytes=99,
    )
    path = Path(settings.download_dir) / "store" / "ca_mismatch_hash"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    ts = now_ms()
    async with transaction() as conn:
        stored_file_id = int(
            (
                await conn.execute(
                    insert(stored_files)
                    .values(
                        content_hash="ca_mismatch_hash",
                        real_path=str(path),
                        size_bytes=size,
                        original_name="cam.bin",
                        created_at_ms=ts,
                    )
                    .returning(stored_files.c.id)
                )
            ).scalar_one()
        )
    with pytest.raises(RepositoryConflictError):
        await dl.complete_attempt(
            attempt_id=gd["id"],
            expected_gid="gidcm2",
            stored_file_id=stored_file_id,
            size_bytes=size,
            original_name="cam.bin",
            completed_at_ms=ts,
        )


async def test_soft_expire_backfills_names(temp_db: str) -> None:
    user = await create_user_v0(username="backfill_user")
    now = now_ms()
    gd = await create_global_download_v0(
        resource_key="magnet:backfill",
        status="failed",
        display_name="global_name.bin",
        error_message="boom",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status="failed",
        display_name=None,
        error_message=None,
    )
    async with transaction() as conn:
        await conn.execute(
            update(user_tasks)
            .where(user_tasks.c.id == task["id"])
            .values(finished_at_ms=now - 10_000)
        )
    result = await retention_repo.soft_expire_due_history(
        cutoff_ms=now - 1000, now=now
    )
    assert result["expired_count"] == 1
    row = await ut.get_user_task(user["id"], gd["id"])
    assert row is not None
    assert row["display_name"] == "global_name.bin"
    assert row["error_message"] == "boom"


async def test_list_user_tasks_page_invalid_filter(temp_db: str) -> None:
    user = await create_user_v0(username="bad_filter")
    with pytest.raises(ValueError):
        await ut.list_user_tasks_page(
            user["id"], page=1, page_size=10, status_filter="bogus"
        )


async def test_attach_completed_file_reserved_drift(temp_db: str) -> None:
    user = await create_user_v0(username="attach_drift", quota_bytes=1_000_000)
    path = Path(settings.download_dir) / "store" / "attach_drift_hash"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"abcd")
    ts = now_ms()
    async with transaction() as conn:
        stored_file_id = int(
            (
                await conn.execute(
                    insert(stored_files)
                    .values(
                        content_hash="attach_drift_hash",
                        real_path=str(path),
                        size_bytes=4,
                        original_name="adr.bin",
                        created_at_ms=ts,
                    )
                    .returning(stored_files.c.id)
                )
            ).scalar_one()
        )
    gd = await create_global_download_v0(
        resource_key="magnet:attach-drift", status="active", aria2_gid="gidad1"
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status="active",
        reserved_bytes=8,
    )
    # usage reserved stays 0 -> release guard trips
    with pytest.raises(RepositoryConflictError):
        await ut.attach_completed_file_to_user(
            user_id=user["id"],
            quota_bytes=1_000_000,
            global_download_id=gd["id"],
            stored_file_id=stored_file_id,
            size_bytes=4,
            display_name="adr.bin",
            finished_at_ms=ts,
        )


async def test_mark_global_download_failed_clear_gid(temp_db: str) -> None:
    user = await create_user_v0(username="mark_clear")
    gd = await create_global_download_v0(
        resource_key="magnet:mark-clear", status="active", aria2_gid="gidmc1"
    )
    task = await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )
    assert task["id"]
    row = await ut.mark_global_download_failed(
        gd["id"], expected_gid="gidmc1", message="gone", clear_gid=True
    )
    assert row is not None
    assert row["aria2_gid"] is None


async def test_count_active_user_tasks(temp_db: str) -> None:
    user = await create_user_v0(username="count_user")
    other = await create_user_v0(username="count_other")
    gd = await create_global_download_v0(
        resource_key="magnet:count-active", status="active", aria2_gid="gidct1"
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )
    await create_user_task_v0(
        user_id=other["id"], global_download_id=gd["id"], status="paused"
    )
    gd_done = await create_global_download_v0(
        resource_key="magnet:count-done", status="failed"
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd_done["id"], status="failed"
    )
    assert await ut.count_active_user_tasks(gd["id"]) == 2
    assert await ut.count_active_user_tasks(gd_done["id"]) == 0
