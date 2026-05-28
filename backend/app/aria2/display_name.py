"""Display-name helpers shared by aria2 sync and listener paths."""

from __future__ import annotations

from sqlalchemy import exists, func, literal, or_
from sqlalchemy.sql.elements import ColumnElement

from app.db.schema import global_downloads, user_tasks


def refreshable_user_task_display_name_condition() -> ColumnElement[bool]:
    """Return rows whose user task name is still a system placeholder."""
    synthetic_torrent_name = literal("torrent-") + func.substr(
        global_downloads.c.resource_key,
        1,
        12,
    )
    return or_(
        user_tasks.c.display_name.is_(None),
        user_tasks.c.display_name == "",
        user_tasks.c.display_name.startswith("magnet:"),
        user_tasks.c.display_name.startswith("torrent:"),
        exists().where(
            global_downloads.c.id == user_tasks.c.global_download_id,
            global_downloads.c.resource_kind == "torrent",
            user_tasks.c.display_name == synthetic_torrent_name,
        ),
    )
