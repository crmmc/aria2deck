from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.db.bootstrap import bootstrap_database
from app.db.engine import (
    clear_credential_scrub_marker,
    credential_scrub_marker_path,
    get_engine,
    mark_credential_scrub_pending,
)


@pytest.mark.asyncio
async def test_secure_delete_is_enabled_for_connections(temp_db: str) -> None:
    async with get_engine().connect() as conn:
        enabled = (await conn.execute(text("PRAGMA secure_delete"))).scalar_one()
    assert enabled == 1


@pytest.mark.asyncio
async def test_scrub_marker_is_retained_when_vacuum_space_is_insufficient(
    monkeypatch: pytest.MonkeyPatch, temp_db: str
) -> None:
    import app.db.engine as engine_module

    marker = credential_scrub_marker_path()
    mark_credential_scrub_pending()
    monkeypatch.setattr(
        engine_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )
    try:
        await bootstrap_database()
        assert marker.exists()
    finally:
        clear_credential_scrub_marker()
