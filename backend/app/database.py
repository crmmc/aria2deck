"""Compatibility wrapper for code not yet migrated to app.db.engine."""

from app.db.bootstrap import bootstrap_database as init_db
from app.db.engine import (
    check_database_integrity,
    check_wal_integrity,
    dispose_engine,
    get_engine as _get_engine,
    get_session_maker as _get_session_maker,
    reset_engine,
    session_scope as get_session,
)


__all__ = [
    "_get_engine",
    "_get_session_maker",
    "check_database_integrity",
    "check_wal_integrity",
    "dispose_engine",
    "get_session",
    "init_db",
    "init_default_config",
    "reset_engine",
]


async def init_default_config(_session) -> None:
    return None
