from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt

from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.core.time_utils import ms_to_iso, now_ms
from app.domain.errors import (
    BadRequestError,
    ForbiddenError,
    GoneError,
    InternalDomainError,
    NotFoundError,
)
from app.domain.shares import (
    MAX_ACTIVE_SHARES_PER_FILE,
    SHARE_ACTIVE_STATUS,
    SHARE_REVOKED_STATUS,
    is_share_active,
    is_share_exhausted,
    is_share_expired,
)
from app.repositories import shares as shares_repo
from app.repositories.errors import RepositoryConflictError
from app.services.file_service import directory_entries, normalize_entry_parent, validate_subpath

SHARE_TOKEN_EXPIRE_MINUTES = 30


def share_to_out(share: dict[str, Any], file_name: str, file_size: int) -> dict:
    return {
        "id": int(share["id"]),
        "share_code": share["share_code"],
        "file_name": file_name,
        "file_size": file_size,
        "has_password": share["password_hash"] is not None,
        "expires_at": ms_to_iso(share["expires_at_ms"]),
        "max_downloads": share["max_downloads"],
        "download_count": int(share["download_count"]),
        "status": share["status"],
        "created_at": ms_to_iso(share["created_at_ms"]) or "",
        "last_accessed_at": ms_to_iso(share["last_accessed_at_ms"]),
    }


def generate_share_code() -> str:
    return secrets.token_urlsafe(6)[:8]


def create_access_token(share_code: str) -> str:
    payload = {
        "sub": share_code,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=SHARE_TOKEN_EXPIRE_MINUTES),
        "type": "share_access",
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def verify_access_token(share_code: str, token: str) -> bool:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        return payload.get("sub") == share_code and payload.get("type") == "share_access"
    except jwt.PyJWTError:
        return False


async def create_share(
    *,
    user_id: int,
    user_file_id: int,
    password: str | None,
    expires_in: int | None,
    max_downloads: int | None,
) -> dict:
    user_file = await shares_repo.get_owned_file(user_id, user_file_id)
    if not user_file:
        raise NotFoundError("文件不存在")

    timestamp = now_ms()
    password_hash = hash_password(password) if password else None

    def values_factory() -> dict[str, Any]:
        return {
            "share_code": generate_share_code(),
            "owner_id": user_id,
            "user_file_id": user_file_id,
            "password_hash": password_hash,
            "expires_at_ms": timestamp + expires_in * 1000 if expires_in else None,
            "max_downloads": max_downloads,
            "download_count": 0,
            "status": SHARE_ACTIVE_STATUS,
            "created_at_ms": timestamp,
        }

    try:
        share = await shares_repo.create_share_with_retry(
            user_file_id=user_file_id,
            timestamp_ms=timestamp,
            max_active_shares=MAX_ACTIVE_SHARES_PER_FILE,
            values_factory=values_factory,
            max_attempts=5,
        )
    except shares_repo.ShareTargetInactiveError:
        raise NotFoundError("文件不存在") from None
    except RepositoryConflictError:
        raise InternalDomainError("分享码生成失败，请重试") from None
    if share is None:
        raise BadRequestError(f"每个文件最多 {MAX_ACTIVE_SHARES_PER_FILE} 个活跃分享")
    return share_to_out(
        share,
        user_file["display_name"] or "未命名",
        int(user_file["size_bytes"] or 0),
    )


async def list_shares(user_id: int) -> list[dict]:
    rows = await shares_repo.list_shares(user_id)
    return [
        share_to_out(row, row["file_name"] or "未命名", int(row["size_bytes"] or 0))
        for row in rows
    ]


async def revoke_share(share_id: int, user_id: int) -> dict:
    current = await shares_repo.get_share_status_for_owner(share_id, user_id)
    if current is None:
        raise NotFoundError("分享不存在")
    if current == SHARE_REVOKED_STATUS:
        raise BadRequestError("分享已失效")
    await shares_repo.revoke_share(share_id, user_id)
    return {"ok": True}


async def delete_share(share_id: int, user_id: int) -> dict:
    if not await shares_repo.delete_share(share_id, user_id):
        raise NotFoundError("分享不存在")
    return {"ok": True}


async def revoke_all_shares(user_id: int) -> dict:
    return {"ok": True, "count": await shares_repo.revoke_all_shares(user_id)}


async def get_share_with_file(code: str) -> dict[str, Any]:
    share, existed_without_file = await shares_repo.get_share_with_file(code)
    if share:
        return share
    if existed_without_file:
        raise GoneError("文件已删除")
    raise NotFoundError("分享不存在")


async def get_share_info(code: str) -> dict:
    row = await get_share_with_file(code)
    timestamp = now_ms()
    return {
        "file_name": row["file_name"] or "未命名",
        "file_size": int(row["size_bytes"] or 0),
        "is_directory": bool(row["is_directory"]),
        "has_password": row["password_hash"] is not None,
        "is_expired": row["status"] != SHARE_ACTIVE_STATUS
        or is_share_expired(row["expires_at_ms"], now_ms=timestamp),
        "is_exhausted": is_share_exhausted(
            row["max_downloads"],
            download_count=int(row["download_count"]),
        ),
    }


async def access_share(code: str, password: str) -> dict:
    share = await get_share_with_file(code)
    if not is_share_active(
        status=str(share["status"]),
        expires_at_ms=share["expires_at_ms"],
        max_downloads=share["max_downloads"],
        download_count=int(share["download_count"]),
        now_ms=now_ms(),
    ):
        raise GoneError("分享已失效")
    if not share["password_hash"]:
        raise BadRequestError("该分享无需密码")
    if not verify_password(password, share["password_hash"]):
        raise ForbiddenError("密码错误")
    return {"access_token": create_access_token(code)}


async def check_share_access(code: str, token: str | None) -> dict[str, Any]:
    share = await get_share_with_file(code)
    if not is_share_active(
        status=str(share["status"]),
        expires_at_ms=share["expires_at_ms"],
        max_downloads=share["max_downloads"],
        download_count=int(share["download_count"]),
        now_ms=now_ms(),
    ):
        raise GoneError("分享已失效")
    if share["password_hash"] and (not token or not verify_access_token(code, token)):
        raise ForbiddenError("需要密码验证")
    return share


async def resolve_shared_download_target(
    share: dict[str, Any],
    *,
    subpath: str | None,
) -> tuple[Path, str]:
    base_path = Path(share["real_path"])
    if not base_path.exists():
        raise NotFoundError("文件不存在")
    if subpath:
        if not share["is_directory"]:
            raise BadRequestError("该文件不是目录")
        target = validate_subpath(base_path, subpath)
        if not target.exists():
            raise NotFoundError("子文件不存在")
        if target.is_dir():
            raise BadRequestError("不能下载目录")
        filename = target.name
    else:
        if share["is_directory"]:
            raise BadRequestError("请指定子文件路径")
        target = base_path
        filename = share["file_name"] or "download"

    return target, filename


async def consume_share_download(share_id: int) -> None:
    updated = await shares_repo.consume_share_download(
        share_id,
        timestamp_ms=now_ms(),
    )
    if not updated:
        raise GoneError("分享已失效或下载次数已用完")


async def browse_shared_directory(code: str, token: str | None, subpath: str) -> list[dict]:
    share = await check_share_access(code, token)
    if not share["is_directory"]:
        raise BadRequestError("该文件不是目录")
    entries = await directory_entries(
        int(share["stored_file_id"]),
        normalize_entry_parent(subpath),
    )
    await shares_repo.touch_share(int(share["id"]), now_ms())
    return entries
