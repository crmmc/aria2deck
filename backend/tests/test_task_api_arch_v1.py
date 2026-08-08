"""Task 6 — API surface switches to the Task Core entry points.

Covers (AC-7 / AC-9, API 面):
- ``task_service.cancel_task`` now routes through ``task_core.unref`` with a
  ``BackendPort`` adapter, mapping ``UnrefError`` codes onto the existing
  REST error surface (404 任务不存在 / 409 已结束).
- ``build_rest_task_response`` projects a ``status_label`` via
  ``user_ref.projection.user_visible_label``.
- ``task_service.register_and_submit`` composes register() + submit_tid()
  for new callers; submit failure rolls the pid back via unref.
- HTTP cancel of a v0-created task keeps legacy semantics: status flipped,
  reserved budget released, aria2 writer stopped once.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.engine import transaction
from app.db.schema import user_tasks
from app.domain.errors import BadGatewayError, ConflictError, NotFoundError
from app.modules.task_core.register import ResourceSpec
from app.modules.task_core.unref import (
    ERROR_ALREADY_TERMINAL,
    ERROR_FORBIDDEN,
    ERROR_NOT_FOUND,
    UnrefError,
)
from app.services import task_service
from app.services.task_projection import build_rest_task_response
from tests.fakes import make_aria2_client
from tests.helpers_v0 import (
    create_global_download_v0,
    create_user_task_v0,
    create_user_v0,
)


# --------------------------------------------------------------------------- #
# Cancel path: task_service.cancel_task → task_core.unref                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cancel_task_routes_through_unref(temp_db: str) -> None:
    """cancel_task 调用 task_core.unref.unref，并注入 BackendPort。"""
    calls: list[dict] = []

    async def fake_unref(*, user_id, pid, backend=None, error_message="用户取消", expected_gid=None):
        calls.append({"user_id": user_id, "pid": pid, "backend": backend})

    with (
        patch("app.services.task_service.unref", side_effect=fake_unref),
        patch("app.services.task_service._get_backend") as get_backend,
    ):
        get_backend.return_value = AsyncMock(name="backend")
        result = await task_service.cancel_task(
            user_id=1, user_task_id=42, quota_bytes=0
        )

    assert result == {"ok": True}
    assert calls == [
        {"user_id": 1, "pid": 42, "backend": get_backend.return_value}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_exc", "expected_message"),
    [
        (ERROR_NOT_FOUND, NotFoundError, "任务不存在"),
        # foreign pids are folded into 404 to avoid leaking existence
        (ERROR_FORBIDDEN, NotFoundError, "任务不存在"),
        (ERROR_ALREADY_TERMINAL, ConflictError, "任务已结束"),
    ],
)
async def test_cancel_task_maps_unref_errors(
    temp_db: str, code: str, expected_exc: type, expected_message: str
) -> None:
    async def fake_unref(*, user_id, pid, backend=None, error_message="用户取消", expected_gid=None):
        raise UnrefError(code, expected_message)

    with (
        patch("app.services.task_service.unref", side_effect=fake_unref),
        patch("app.services.task_service._get_backend", return_value=AsyncMock()),
    ):
        with pytest.raises(expected_exc) as excinfo:
            await task_service.cancel_task(user_id=1, user_task_id=7, quota_bytes=0)
    assert expected_message in str(excinfo.value)


@pytest.mark.asyncio
async def test_cancel_task_unexpected_error_becomes_bad_gateway(temp_db: str) -> None:
    async def fake_unref(*, user_id, pid, backend=None, error_message="用户取消", expected_gid=None):
        raise RuntimeError("db exploded")

    with (
        patch("app.services.task_service.unref", side_effect=fake_unref),
        patch("app.services.task_service._get_backend", return_value=AsyncMock()),
    ):
        with pytest.raises(BadGatewayError):
            await task_service.cancel_task(user_id=1, user_task_id=7, quota_bytes=0)


@pytest.mark.asyncio
async def test_cancel_v0_task_through_http_path_reclaims_writer(
    temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0 task cancelled via service → pid cancelled and writer stopped via remove."""
    user = await create_user_v0(username="arch-cancel")
    gd = await create_global_download_v0(
        resource_key="magnet:?xt=urn:btih:arch6",
        source_uri="magnet:?xt=urn:btih:arch6",
        resource_kind="magnet",
        status="active",
        total_bytes=1024,
        size_known=True,
        aria2_gid="gid-arch6",
    )
    task = await create_user_task_v0(
        user_id=user["id"],
        global_download_id=gd["id"],
        status="active",
    )
    client = make_aria2_client(remove="gid-arch6")

    with patch("app.services.task_service._get_client", return_value=client):
        result = await task_service.cancel_task(
            user_id=user["id"],
            user_task_id=int(task["id"]),
            quota_bytes=user["quota_bytes"],
        )

    assert result == {"ok": True}
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(user_tasks).where(user_tasks.c.id == task["id"])
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    assert row["status"] == "cancelled"
    assert int(row["reserved_bytes"] or 0) == 0
    client.remove.assert_awaited_once_with("gid-arch6")


# --------------------------------------------------------------------------- #
# Projection: status_label                                                    #
# --------------------------------------------------------------------------- #


def test_rest_response_includes_projection_label() -> None:
    row = {
        "id": 1,
        "global_download_id": 9,
        "status": "queued",
        "global_status": "queued",
        "display_name": "a.bin",
    }
    payload = build_rest_task_response(row)
    assert payload["status"] == "queued"
    assert payload["status_label"] == "排队中"

    row_paused = {**row, "status": "paused", "global_status": "paused"}
    payload_paused = build_rest_task_response(row_paused)
    assert payload_paused["status"] == "paused"
    assert payload_paused["status_label"] == "已暂停"

    row_completed = {**row, "status": "completed", "global_status": "completed"}
    payload_completed = build_rest_task_response(row_completed)
    assert payload_completed["status"] == "complete"
    assert payload_completed["status_label"] == "已完成"

    row_failed = {
        **row,
        "status": "failed",
        "global_status": "failed",
        "error_message": "boom",
    }
    payload_failed = build_rest_task_response(row_failed)
    assert payload_failed["status"] == "error"
    assert payload_failed["status_label"] == "已失败"


# --------------------------------------------------------------------------- #
# register_and_submit                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_register_and_submit_created_path(temp_db: str) -> None:
    """New entry: register() creates pid+tid, submit_tid binds the gid, and
    the response keeps the standard REST task shape."""
    user = await create_user_v0(username="arch-create")
    client = make_aria2_client(add_uri="gid-arch-create")
    spec = ResourceSpec(
        resource_key="magnet:?xt=urn:btih:arch-create",
        source_uri="magnet:?xt=urn:btih:arch-create",
        resource_kind="magnet",
        display_name="arch-create.bin",
        size_bytes=0,
        size_known=False,
    )

    with patch("app.services.task_service._get_client", return_value=client):
        payload = await task_service.register_and_submit(
            user_id=user["id"],
            quota_bytes=user["quota_bytes"],
            resource=spec,
        )

    assert payload["name"] == "arch-create.bin"
    assert payload["status"] in {"waiting", "active"}
    assert payload["status_label"] in {"排队中", "下载中"}
    assert payload["uri"] == "magnet:?xt=urn:btih:arch-create"
    assert payload["id"]
    assert payload["task_id"]
    client.add_uri.assert_awaited_once()


@pytest.mark.asyncio
async def test_register_and_submit_duplicate_maps_to_conflict(temp_db: str) -> None:
    user = await create_user_v0(username="arch-dup")
    gd = await create_global_download_v0(
        resource_key="magnet:?xt=urn:btih:arch-dup",
        source_uri="magnet:?xt=urn:btih:arch-dup",
        resource_kind="magnet",
        status="active",
        total_bytes=128,
        size_known=True,
    )
    await create_user_task_v0(
        user_id=user["id"], global_download_id=gd["id"], status="active"
    )
    spec = ResourceSpec(
        resource_key="magnet:?xt=urn:btih:arch-dup",
        source_uri="magnet:?xt=urn:btih:arch-dup",
        resource_kind="magnet",
        size_bytes=128,
        size_known=True,
    )

    client = make_aria2_client()
    with patch("app.services.task_service._get_client", return_value=client):
        with pytest.raises(ConflictError) as excinfo:
            await task_service.register_and_submit(
                user_id=user["id"],
                quota_bytes=user["quota_bytes"],
                resource=spec,
            )
    assert "任务已存在" in str(excinfo.value)
    client.add_uri.assert_not_called()


@pytest.mark.asyncio
async def test_register_and_submit_submit_failure_rolls_back(temp_db: str) -> None:
    """Submit failure cancels the fresh pid via unref and surfaces BadGateway."""
    user = await create_user_v0(username="arch-rollback")
    client = make_aria2_client(add_uri=OSError("aria2 down"))
    spec = ResourceSpec(
        resource_key="magnet:?xt=urn:btih:arch-rollback",
        source_uri="magnet:?xt=urn:btih:arch-rollback",
        resource_kind="magnet",
        display_name="arch-rollback.bin",
        size_bytes=0,
        size_known=False,
    )

    with patch("app.services.task_service._get_client", return_value=client):
        with pytest.raises(BadGatewayError):
            await task_service.register_and_submit(
                user_id=user["id"],
                quota_bytes=user["quota_bytes"],
                resource=spec,
            )

    async with transaction() as conn:
        rows = (
            (
                await conn.execute(
                    select(user_tasks).where(user_tasks.c.user_id == user["id"])
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    assert rows[0]["status"] == "cancelled"


def test_create_task_response_keeps_legacy_fields() -> None:
    """Response compatibility: the create payload keeps legacy keys and now
    carries status_label alongside."""
    task_row = {
        "id": 5,
        "global_download_id": 6,
        "status": "queued",
        "display_name": "file.bin",
    }
    payload = task_service.create_task_response(
        task_row=task_row,
        global_download=None,
        fallback_uri="magnet:?xt=urn:btih:x",
        fallback_name="file.bin",
        fallback_total_length=0,
    )
    for key in ("id", "task_id", "status", "name", "uri", "total_length",
                "completed_length", "download_speed", "upload_speed",
                "frozen_space", "created_at", "updated_at", "status_label"):
        assert key in payload
    assert payload["status_label"] == "排队中"


def test_unregister_task_creation_via_service_uses_module_entry() -> None:
    """Guard: cancel path no longer calls the legacy lifecycle service."""
    import inspect

    source = inspect.getsource(task_service.cancel_task)
    assert "cancel_user_task(" not in source
    assert "unref(" in source
