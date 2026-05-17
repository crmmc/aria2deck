from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from sqlalchemy import insert, select

from app.core.security import hash_password
from app.db.engine import transaction
from app.db.schema import (
    global_downloads,
    pack_tasks,
    sessions,
    stored_files,
    user_files,
    user_storage_usage,
    user_tasks,
    users,
)


def now_ms() -> int:
    return int(time.time() * 1000)


async def create_user_v0(
    *,
    username: str,
    password: str = "testpass",
    is_admin: bool = False,
    quota_bytes: int = 100 * 1024 * 1024 * 1024,
) -> dict[str, Any]:
    timestamp = now_ms()
    async with transaction() as conn:
        result = await conn.execute(
            insert(users)
            .values(
                username=username,
                password_hash=hash_password(password),
                is_admin=1 if is_admin else 0,
                quota_bytes=quota_bytes,
                is_initial_password=0,
                created_at_ms=timestamp,
                updated_at_ms=timestamp,
            )
            .returning(users)
        )
        row = result.mappings().one()
        await conn.execute(
            insert(user_storage_usage).values(
                user_id=row["id"],
                used_bytes=0,
                reserved_bytes=0,
                updated_at_ms=timestamp,
            )
        )
    return dict(row)


async def get_user_v0(user_id: int) -> dict[str, Any] | None:
    async with transaction() as conn:
        result = await conn.execute(select(users).where(users.c.id == user_id))
        row = result.mappings().first()
    return dict(row) if row else None


async def create_session_v0(user_id: int, session_id: str, expires_at_ms: int | None = None) -> str:
    if expires_at_ms is None:
        expires_at_ms = now_ms() + 12 * 60 * 60 * 1000
    async with transaction() as conn:
        await conn.execute(
            insert(sessions).values(
                id=session_id,
                user_id=user_id,
                expires_at_ms=expires_at_ms,
                created_at_ms=now_ms(),
            )
        )
    return session_id


async def create_user_file_v0(
    *,
    user_id: int,
    real_path: Path,
    content_hash: str,
    display_name: str,
    size_bytes: int,
    is_directory: bool = False,
) -> dict[str, Any]:
    timestamp = now_ms()
    async with transaction() as conn:
        stored = (
            await conn.execute(
                insert(stored_files)
                .values(
                    content_hash=content_hash,
                    real_path=str(real_path),
                    size_bytes=size_bytes,
                    is_directory=1 if is_directory else 0,
                    original_name=display_name,
                    created_at_ms=timestamp,
                )
                .returning(stored_files)
            )
        ).mappings().one()
        user_file = (
            await conn.execute(
                insert(user_files)
                .values(
                    user_id=user_id,
                    stored_file_id=stored["id"],
                    display_name=display_name,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                )
                .returning(user_files)
            )
        ).mappings().one()
    return {
        "id": user_file["id"],
        "hash": stored["content_hash"],
        "content_hash": stored["content_hash"],
        "stored_file_id": stored["id"],
        "display_name": user_file["display_name"],
        "size": stored["size_bytes"],
        "is_directory": bool(stored["is_directory"]),
        "real_path": stored["real_path"],
    }


async def create_global_download_v0(
    *,
    resource_key: str,
    source_uri: str = "magnet:?xt=urn:btih:test",
    resource_kind: str = "magnet",
    status: str = "queued",
    aria2_gid: str | None = None,
    display_name: str | None = None,
    total_bytes: int = 0,
    completed_bytes: int = 0,
    completed_file_id: int | None = None,
) -> dict[str, Any]:
    timestamp = now_ms()
    async with transaction() as conn:
        row = (
            await conn.execute(
                insert(global_downloads)
                .values(
                    resource_key=resource_key,
                    resource_kind=resource_kind,
                    source_uri=source_uri,
                    display_name=display_name,
                    aria2_gid=aria2_gid,
                    status=status,
                    total_bytes=total_bytes,
                    completed_bytes=completed_bytes,
                    completed_file_id=completed_file_id,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                    completed_at_ms=timestamp if status == "completed" else None,
                )
                .returning(global_downloads)
            )
        ).mappings().one()
    return dict(row)


async def create_user_task_v0(
    *,
    user_id: int,
    global_download_id: int,
    status: str = "queued",
    reserved_bytes: int = 0,
    display_name: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    timestamp = now_ms()
    async with transaction() as conn:
        row = (
            await conn.execute(
                insert(user_tasks)
                .values(
                    user_id=user_id,
                    global_download_id=global_download_id,
                    status=status,
                    reserved_bytes=reserved_bytes,
                    display_name=display_name,
                    error_message=error_message,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                    finished_at_ms=timestamp if status in {"completed", "failed", "cancelled"} else None,
                )
                .returning(user_tasks)
            )
        ).mappings().one()
    return dict(row)


async def create_pack_task_v0(
    *,
    user_id: int,
    source_user_file_ids: list[int],
    source_size_bytes: int = 0,
    reserved_bytes: int = 0,
    status: str = "pending",
    output_name: str | None = None,
) -> dict[str, Any]:
    import json

    timestamp = now_ms()
    async with transaction() as conn:
        row = (
            await conn.execute(
                insert(pack_tasks)
                .values(
                    user_id=user_id,
                    source_user_file_ids_json=json.dumps(source_user_file_ids),
                    source_size_bytes=source_size_bytes,
                    reserved_bytes=reserved_bytes,
                    output_name=output_name,
                    delete_source=0,
                    status=status,
                    progress=0,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                    finished_at_ms=timestamp if status in {"completed", "failed", "cancelled"} else None,
                )
                .returning(pack_tasks)
            )
        ).mappings().one()
    return dict(row)
