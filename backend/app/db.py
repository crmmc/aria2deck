"""Removed legacy sync schema initializer.

The backend now uses app.db.bootstrap and app.db.engine.
"""


def __getattr__(name: str):
    raise RuntimeError(
        f"app.db.{name} was removed; use app.db.bootstrap or app.db.engine"
    )
