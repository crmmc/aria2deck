from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from aria2deck.infrastructure.sqlite.bootstrap import initialize_database
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[SQLiteDatabase]:
    db = SQLiteDatabase(tmp_path / "v2.db")
    await initialize_database(db)
    yield db
    await db.close()
