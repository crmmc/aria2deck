"""M10: size truth single-writer contract.

Invariants:
- Projection paths must not overwrite admitted total_bytes from live noise.
- size_known=1 total is monotonic except via admission / completion / repair.
- Display prefers DB truth when size is known.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import global_downloads
from app.domain.lifecycle import ReconcileResult
from app.modules.task_core.states import ERROR_ADMISSION_PAUSED
from app.services.download_ops import map_progress_values
from app.services.lifecycle.coordinator import reconcile_attempt_signal
from app.services.lifecycle.handoff import coordinate_reported_size
from app.services.task_projection import build_aria2_status, build_rest_task_response
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


async def _set_usage_reserved(user_id: int, reserved: int) -> None:
    from app.db.schema import user_storage_usage

    async with transaction() as conn:
        await conn.execute(
            user_storage_usage.update()
            .where(user_storage_usage.c.user_id == user_id)
            .values(reserved_bytes=reserved)
        )


async def _fetch_global(download_id: int) -> dict[str, Any]:
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(global_downloads).where(
                        global_downloads.c.id == download_id
                    )
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


# ---------------------------------------------------------------------------
# T1 — bug reproduction: admitted size must not be wiped by totalLength=0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t1_admitted_size_not_wiped_by_zero_live_total(temp_db: str) -> None:
    """BT paused + admission_paused + size_known + 50MB; live totalLength=0.

    Projection must keep total_bytes; no fake growth dance (no pause_gid).
    """
    user = await create_user_v0(username="m10_t1", quota_bytes=100_000_000)
    download = await create_global_download_v0(
        resource_key="torrent:m10-t1:files:abc",
        source_uri="base64:dGVzdA==",
        resource_kind="torrent",
        status="paused",
        aria2_gid="gid_m10_t1",
        total_bytes=52_428_800,
        size_known=True,
        completed_bytes=0,
        disk_reserved_bytes=52_428_800,
        error_code=ERROR_ADMISSION_PAUSED,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="paused",
        reserved_bytes=52_428_800,
    )
    await _set_usage_reserved(user["id"], 52_428_800)

    live_status: dict[str, Any] = {
        "gid": "gid_m10_t1",
        "status": "paused",
        "totalLength": "0",
        "completedLength": "0",
        "files": [
            {
                "path": "/tmp/payload.bin",
                "length": "52428800",
                "selected": "true",
            }
        ],
        "bittorrent": {"info": {"name": "payload.bin"}},
    }
    client = make_aria2_client(tell_status=live_status)
    result = await reconcile_attempt_signal(
        backend=client,
        observed_gid="gid_m10_t1",
        event=None,
        observed_status=live_status,
        log_prefix="[M10-T1]",
    )

    assert result in {
        ReconcileResult.CHANGED,
        ReconcileResult.WAITING,
        ReconcileResult.IGNORED,
    }
    assert result != ReconcileResult.TERMINALIZED

    stored = await _fetch_global(download["id"])
    assert int(stored["total_bytes"]) == 52_428_800
    assert bool(stored["size_known"]) is True
    # No fake growth dance: pause not re-issued.
    client.pause.assert_not_called()
    # error_code must not be re-stamped as a fresh growth admission dance.
    # Keeping admission_paused is fine (still paused); re-branding via wipe is not.
    assert stored["error_code"] in {ERROR_ADMISSION_PAUSED, None}
    # Critical: must not have been cleared then re-applied via growth path.
    # If total stayed, coordinate sees candidate==old (or shrink skip) → no dance.


# ---------------------------------------------------------------------------
# T2 — unknown size still admits via gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_unknown_size_admits_trusted_live(temp_db: str) -> None:
    user = await create_user_v0(username="m10_t2", quota_bytes=10_000_000)
    download = await create_global_download_v0(
        resource_key="http:m10-t2",
        source_uri="https://example.com/m10-t2.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid_m10_t2",
        total_bytes=0,
        size_known=False,
        completed_bytes=0,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=0,
    )

    client = make_aria2_client()
    result = await coordinate_reported_size(
        backend=client,
        download=download,
        expected_gid="gid_m10_t2",
        control_gid="gid_m10_t2",
        status={
            "status": "active",
            "totalLength": "2048",
            "completedLength": "0",
        },
        require_trusted_total=True,
        acquire_lifecycle_lock=False,
    )
    assert result["outcome"] == "admitted"
    stored = await _fetch_global(download["id"])
    assert int(stored["total_bytes"]) == 2048
    assert bool(stored["size_known"]) is True


# ---------------------------------------------------------------------------
# T3 — real growth still triggers dance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t3_real_growth_still_dances(temp_db: str) -> None:
    user = await create_user_v0(username="m10_t3", quota_bytes=50_000_000)
    download = await create_global_download_v0(
        resource_key="http:m10-t3",
        source_uri="https://example.com/m10-t3.bin",
        resource_kind="http",
        status="active",
        aria2_gid="gid_m10_t3",
        total_bytes=1024,
        size_known=True,
        completed_bytes=0,
        disk_reserved_bytes=1024,
    )
    await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="active",
        reserved_bytes=1024,
    )
    await _set_usage_reserved(user["id"], 1024)

    client = make_aria2_client()
    result = await coordinate_reported_size(
        backend=client,
        download=download,
        expected_gid="gid_m10_t3",
        control_gid="gid_m10_t3",
        status={
            "status": "active",
            "totalLength": "4096",
            "completedLength": "0",
        },
        acquire_lifecycle_lock=False,
    )
    assert result["outcome"] == "admitted"
    assert result["paused_by_us"] is True
    client.pause.assert_called_once_with("gid_m10_t3")
    client.unpause.assert_called_once_with("gid_m10_t3")
    stored = await _fetch_global(download["id"])
    assert int(stored["total_bytes"]) == 4096


# ---------------------------------------------------------------------------
# T4 — contract scan: size-truth write sites must stay on the whitelist
# ---------------------------------------------------------------------------

import ast

# Files (relative to backend/app/) allowed to write total_bytes / disk_reserved_bytes.
# Values are the permitted keys; anything outside or with a non-permitted key fails.
# Discovered by full-repo AST write-site enumeration (M10 fixplan §2.4).
_SIZE_TRUTH_WRITE_WHITELIST: dict[str, frozenset[str]] = {
    "repositories/task/downloads.py": frozenset(
        {"total_bytes", "disk_reserved_bytes"}
    ),
    "repositories/task/user_tasks.py": frozenset(
        {"total_bytes", "disk_reserved_bytes"}
    ),
    "modules/task_core/register.py": frozenset(
        {"total_bytes", "disk_reserved_bytes"}
    ),
    "services/lifecycle/handoff.py": frozenset(
        {"total_bytes", "disk_reserved_bytes"}
    ),
    # coordinator may brand external_paused with disk_reserved_bytes only;
    # total_bytes remains admission-owned and must not appear as a write site.
    "services/lifecycle/coordinator.py": frozenset({"disk_reserved_bytes"}),
}

_SIZE_TRUTH_KEYS = frozenset({"total_bytes", "disk_reserved_bytes"})
_WRITE_CALL_NAMES = frozenset(
    {
        "values",
        "insert",
        "update",
        "create_global_download",
        "update_global_download",
        "create_global_download_attempt",
        "create_user_task",
        "update_user_task",
    }
)


def _call_func_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _enum_size_truth_write_sites(app_root: Path) -> list[tuple[str, int, str]]:
    """AST-enumerate DB-ish write sites for total_bytes / disk_reserved_bytes.

    Heuristics (write-path oriented, not every mention):
    - keyword args on .values() / insert() / update() / create_* / update_*
    - dict literal keys when the dict is an arg to those calls
    - dict literal keys assigned to a *values* name (global_values / row_values)
    - subscript store: foo["total_bytes"] = ...
    """
    hits: list[tuple[str, int, str]] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self, rel: str) -> None:
            self.rel = rel
            self.stack: list[ast.AST] = []

        def visit(self, node: ast.AST) -> None:
            self.stack.append(node)
            super().visit(node)
            self.stack.pop()

        def _enclosing_call(self) -> ast.Call | None:
            for parent in reversed(self.stack[:-1]):
                if isinstance(parent, ast.Call):
                    return parent
                if isinstance(
                    parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    return None
            return None

        def visit_keyword(self, node: ast.keyword) -> None:
            if node.arg in _SIZE_TRUTH_KEYS:
                call = self._enclosing_call()
                if call is not None:
                    name = _call_func_name(call)
                    if name in _WRITE_CALL_NAMES or (
                        name is not None
                        and (name.startswith("create_") or name.startswith("update_"))
                    ):
                        hits.append((self.rel, node.lineno, node.arg))
            self.generic_visit(node)

        def visit_Assign(self, node: ast.Assign) -> None:
            for target in node.targets:
                if isinstance(target, ast.Subscript):
                    key = _const_str(target.slice)
                    if key in _SIZE_TRUTH_KEYS:
                        hits.append((self.rel, target.lineno, key))
                if isinstance(target, ast.Name) and target.id.endswith("values"):
                    if isinstance(node.value, ast.Dict):
                        for key_node in node.value.keys:
                            key = _const_str(key_node)
                            if key in _SIZE_TRUTH_KEYS:
                                hits.append((self.rel, node.lineno, key))
            self.generic_visit(node)

        def visit_Dict(self, node: ast.Dict) -> None:
            keys = [
                k
                for k in (_const_str(key_node) for key_node in node.keys)
                if k in _SIZE_TRUTH_KEYS
            ]
            if keys:
                call = self._enclosing_call()
                if call is not None:
                    name = _call_func_name(call)
                    if name in _WRITE_CALL_NAMES or (
                        name is not None
                        and (name.startswith("create_") or name.startswith("update_"))
                    ):
                        for key in keys:
                            hits.append((self.rel, node.lineno, key))
            self.generic_visit(node)

    for path in sorted(app_root.rglob("*.py")):
        rel = path.relative_to(app_root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        _Visitor(rel).visit(tree)

    # Deduplicate while preserving order.
    seen: set[tuple[str, int, str]] = set()
    unique: list[tuple[str, int, str]] = []
    for item in hits:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def test_t4_size_truth_write_sites_stay_on_whitelist() -> None:
    """Full-repo write-site scan: only whitelisted modules may write size truth.

    Also keeps a fast substring ban on projection modules writing total_bytes
    into update dicts (legacy T4 guard, still useful as a cheap tripwire).
    """
    app_root = Path(__file__).resolve().parents[1] / "app"
    sites = _enum_size_truth_write_sites(app_root)

    offenders: list[str] = []
    for rel, lineno, key in sites:
        allowed = _SIZE_TRUTH_WRITE_WHITELIST.get(rel)
        if allowed is None or key not in allowed:
            offenders.append(f"{rel}:{lineno}:{key}")

    assert offenders == [], (
        "size-truth write sites outside whitelist (do not silently expand; "
        f"report real new writers): {offenders}"
    )

    # Additional fast assertion: projection modules must not assign total_bytes
    # into update / progress dicts (admission-owned size truth).
    projection_targets = [
        app_root / "services" / "lifecycle" / "coordinator.py",
        app_root / "services" / "download_ops.py",
    ]
    projection_offenders: list[str] = []
    for path in projection_targets:
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if (
                stripped.startswith("#")
                or stripped.startswith('"""')
                or stripped.startswith("'''")
            ):
                continue
            if (
                'values["total_bytes"]' in stripped
                or "values['total_bytes']" in stripped
                or 'global_values["total_bytes"]' in stripped
                or "global_values['total_bytes']" in stripped
                or '"total_bytes":' in stripped
                or "'total_bytes':" in stripped
            ):
                projection_offenders.append(f"{path.name}:{i}:{stripped}")
    assert projection_offenders == []


# ---------------------------------------------------------------------------
# T5 — map_progress_values no longer projects total_bytes
# ---------------------------------------------------------------------------


def test_t5_map_progress_values_omits_total_bytes() -> None:
    status = {
        "bittorrent": {"info": {"name": "Movie"}},
        "totalLength": "1000",
        "completedLength": "500",
    }
    result = map_progress_values(status, None)
    assert "total_bytes" not in result
    assert result["completed_bytes"] == 500
    assert result["display_name"] == "Movie"

    # Explicit skip_total disabled must still not reintroduce projection write
    # of total into the progress map (size truth is admission-only).
    status_meta = {
        "files": [{"path": "[METADATA]hash"}],
        "totalLength": "99",
        "completedLength": "10",
    }
    result_meta = map_progress_values(
        status_meta, "fallback", skip_total_on_metadata=False
    )
    assert "total_bytes" not in result_meta
    assert result_meta["completed_bytes"] == 10


# ---------------------------------------------------------------------------
# T6 — display prefers admitted DB truth over live noise
# ---------------------------------------------------------------------------


def test_t6_display_prefers_db_truth_when_size_known() -> None:
    row = {
        "id": 10,
        "user_id": 1,
        "global_download_id": 20,
        "status": "paused",
        "reserved_bytes": 52_428_800,
        "display_name": None,
        "error_message": None,
        "created_at_ms": 1,
        "updated_at_ms": 2,
        "finished_at_ms": None,
        "resource_key": "torrent:m10-t6",
        "resource_kind": "torrent",
        "source_uri": "magnet:?xt=urn:btih:m10t6",
        "bt_info_hash": "m10t6",
        "global_display_name": "payload.bin",
        "aria2_gid": "gid-t6",
        "global_status": "paused",
        "total_bytes": 52_428_800,
        "completed_bytes": 0,
        "error_code": ERROR_ADMISSION_PAUSED,
        "global_error_message": None,
        "completed_at_ms": None,
        "size_known": 1,
    }
    live = {
        "status": "paused",
        "totalLength": "0",
        "completedLength": "0",
    }

    aria2 = build_aria2_status(row, live)
    assert aria2["totalLength"] == str(52_428_800)

    rest = build_rest_task_response(row, live)
    assert rest["total_length"] == 52_428_800


def test_t6_display_uses_live_when_size_unknown() -> None:
    row = {
        "id": 11,
        "user_id": 1,
        "global_download_id": 21,
        "status": "active",
        "reserved_bytes": 0,
        "display_name": None,
        "error_message": None,
        "created_at_ms": 1,
        "updated_at_ms": 2,
        "finished_at_ms": None,
        "resource_key": "http:m10-t6u",
        "resource_kind": "http",
        "source_uri": "https://example.com/u.bin",
        "bt_info_hash": None,
        "global_display_name": "u.bin",
        "aria2_gid": "gid-t6u",
        "global_status": "active",
        "total_bytes": 0,
        "completed_bytes": 0,
        "error_code": None,
        "global_error_message": None,
        "completed_at_ms": None,
        "size_known": 0,
    }
    live = {
        "status": "active",
        "totalLength": "2048",
        "completedLength": "100",
        "downloadSpeed": "10",
        "uploadSpeed": "0",
    }

    aria2 = build_aria2_status(row, live)
    assert aria2["totalLength"] == "2048"

    rest = build_rest_task_response(row, live)
    assert rest["total_length"] == 2048


# ---------------------------------------------------------------------------
# T6b — repo select must surface size_known so list/WS display uses DB truth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t6b_list_user_tasks_row_carries_size_known_for_display(
    temp_db: str,
) -> None:
    """Shared select must include size_known; list path then prefers DB total."""
    from app.repositories.task.user_tasks import list_user_tasks

    user = await create_user_v0(username="m10_t6b", quota_bytes=100_000_000)
    download = await create_global_download_v0(
        resource_key="torrent:m10-t6b:files:abc",
        source_uri="base64:dGVzdA==",
        resource_kind="torrent",
        status="paused",
        aria2_gid="gid_m10_t6b",
        total_bytes=52_428_800,
        size_known=True,
        completed_bytes=0,
        disk_reserved_bytes=52_428_800,
        error_code=ERROR_ADMISSION_PAUSED,
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=download["id"],
        status="paused",
        reserved_bytes=52_428_800,
    )

    rows = await list_user_tasks(user["id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == task["id"]
    assert "size_known" in row
    assert bool(row["size_known"]) is True
    assert int(row["total_bytes"]) == 52_428_800

    live = {
        "status": "paused",
        "totalLength": "0",
        "completedLength": "0",
    }
    rest = build_rest_task_response(row, live)
    assert rest["total_length"] == 52_428_800
