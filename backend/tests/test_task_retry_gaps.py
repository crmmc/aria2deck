"""Coverage gaps for app/services/task_retry.py."""

from __future__ import annotations

import pytest

from app.domain.errors import BadRequestError, NotFoundError
from app.services import task_retry as tr


class TestParseHelpers:
    def test_parse_selection_none(self):
        assert tr._parse_selection_indexes(None) is None
        assert tr._parse_selection_indexes("") is None

    def test_parse_selection_bad_json(self):
        assert tr._parse_selection_indexes("{bad") is None

    def test_parse_selection_not_dict(self):
        assert tr._parse_selection_indexes("[1,2]") is None

    def test_parse_selection_missing_key(self):
        assert tr._parse_selection_indexes('{"other": 1}') is None

    def test_parse_selection_not_list(self):
        assert tr._parse_selection_indexes('{"selected_file_indexes": "x"}') is None

    def test_parse_selection_ok(self):
        assert tr._parse_selection_indexes('{"selected_file_indexes": [1, 2]}') == [1, 2]

    def test_parse_options_none(self):
        assert tr._parse_options(None) is None
        assert tr._parse_options("") is None

    def test_parse_options_bad_json(self):
        assert tr._parse_options("{bad") is None

    def test_parse_options_not_dict(self):
        assert tr._parse_options("[1]") is None


def _gd(kind="http", uri="https://x/f", key="k"):
    return {"resource_kind": kind, "source_uri": uri, "resource_key": key}


class TestLazyMigrateIncomplete:
    @pytest.mark.asyncio
    async def test_http_without_uri(self):
        with pytest.raises(BadRequestError):
            await tr._lazy_migrate_source(
                tid=1, global_download=_gd(uri="")
            )

    @pytest.mark.asyncio
    async def test_torrent_partial_selection(self):
        gd = _gd(kind="torrent", uri="base64:xxx", key="infohash:files:1")
        with pytest.raises(BadRequestError):
            await tr._lazy_migrate_source(tid=1, global_download=gd)

    @pytest.mark.asyncio
    async def test_torrent_non_base64(self):
        with pytest.raises(BadRequestError):
            await tr._lazy_migrate_source(
                tid=1, global_download=_gd(kind="torrent", uri="magnet:?xt=x")
            )

    @pytest.mark.asyncio
    async def test_unknown_kind(self):
        with pytest.raises(BadRequestError):
            await tr._lazy_migrate_source(
                tid=1, global_download=_gd(kind="ftp", uri="ftp://x")
            )


@pytest.mark.asyncio
async def test_rebuild_unknown_kind():
    with pytest.raises(BadRequestError):
        await tr._rebuild_from_source(
            user_id=1,
            quota_bytes=100,
            source={"resource_kind": "other", "payload_text": "x"},
            global_download={},
        )


@pytest.mark.asyncio
async def test_retry_task_not_found(temp_db):
    with pytest.raises(NotFoundError):
        await tr.retry_task(user_id=1, user_task_id=999, quota_bytes=100)


@pytest.mark.asyncio
async def test_retry_task_unknown_terminal_status(temp_db, monkeypatch):
    async def fake_get(user_id, task_id):
        return {
            "id": task_id,
            "history_expired_at_ms": None,
            "status": "mystery",
            "global_download_id": 1,
        }

    monkeypatch.setattr(tr, "get_user_task_by_id", fake_get)
    with pytest.raises(BadRequestError):
        await tr.retry_task(user_id=1, user_task_id=1, quota_bytes=10**9)


@pytest.mark.asyncio
async def test_retry_task_active_status(temp_db, test_user, failed_task):
    from app.db.engine import transaction
    from app.db.schema import user_tasks
    from sqlalchemy import update

    async with transaction() as conn:
        await conn.execute(
            update(user_tasks)
            .where(user_tasks.c.id == failed_task["id"])
            .values(status="active")
        )
    with pytest.raises(BadRequestError):
        await tr.retry_task(
            user_id=test_user["id"],
            user_task_id=failed_task["id"],
            quota_bytes=10**9,
        )


@pytest.mark.asyncio
async def test_lazy_migrate_torrent_full_selection(monkeypatch):
    created = {}

    async def fake_create_source(values):
        created.update(values)
        return {"id": 5, **values}

    async def fake_update(tid, values):
        return {"id": tid, **values}

    monkeypatch.setattr(tr, "create_download_source", fake_create_source)
    monkeypatch.setattr(tr, "update_global_download", fake_update)
    gd = _gd(kind="torrent", uri="base64:QUJD", key="infohash123")
    source = await tr._lazy_migrate_source(tid=1, global_download=gd)
    assert source["id"] == 5
    assert created["payload_text"] == "base64:QUJD"
    assert created["selection_json"] is None


@pytest.mark.asyncio
async def test_lazy_migrate_http_success(monkeypatch):
    async def fake_create_source(values):
        return {"id": 6, **values}

    async def fake_update(tid, values):
        return {"id": tid, **values}

    monkeypatch.setattr(tr, "create_download_source", fake_create_source)
    monkeypatch.setattr(tr, "update_global_download", fake_update)
    source = await tr._lazy_migrate_source(tid=1, global_download=_gd())
    assert source["payload_text"] == "https://x/f"
