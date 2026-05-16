from importlib import util
from pathlib import Path
from types import ModuleType

from app.db.bootstrap import SCHEMA_VERSION, bootstrap_database, validate_schema_version
from app.db.engine import (
    apply_sqlite_pragmas,
    check_database_integrity,
    check_wal_integrity,
    dispose_engine,
    get_engine,
    get_session_maker,
    reset_engine,
    session_scope,
    transaction,
)


def _load_legacy_db() -> ModuleType:
    legacy_path = Path(__file__).resolve().parent.parent / "db.py"
    spec = util.spec_from_file_location("app._legacy_db", legacy_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load legacy database module from {legacy_path}")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_legacy_db = _load_legacy_db()

ensure_default_admin = _legacy_db.ensure_default_admin
execute = _legacy_db.execute
fetch_all = _legacy_db.fetch_all
fetch_one = _legacy_db.fetch_one
init_db = _legacy_db.init_db
main = _legacy_db.main
reset_admin_password_for_dev = _legacy_db.reset_admin_password_for_dev
utc_now = _legacy_db.utc_now

__all__ = [
    "SCHEMA_VERSION",
    "apply_sqlite_pragmas",
    "bootstrap_database",
    "check_database_integrity",
    "check_wal_integrity",
    "dispose_engine",
    "ensure_default_admin",
    "execute",
    "fetch_all",
    "fetch_one",
    "get_engine",
    "get_session_maker",
    "init_db",
    "main",
    "reset_engine",
    "reset_admin_password_for_dev",
    "session_scope",
    "transaction",
    "utc_now",
    "validate_schema_version",
]
