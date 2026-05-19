"""文件分享接口"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.auth import AuthUser, require_user
from app.core.config import settings
from app.core.download_limiter import download_limiter
from app.core.request_rate_guard import (
    RateLimitScope,
    client_ip_from_request,
    ensure_public_allowed,
    ensure_share_access_allowed,
)
from app.core.security import hash_password, verify_password
from app.db.engine import transaction
from app.db.schema import share_links, stored_files, user_files
from app.routers.files import (
    _directory_entries,
    _normalize_entry_parent,
    _range_file_response,
    _tracked_response,
    _validate_subpath,
    ms_to_iso,
)
from app.schemas import (
    CreateShareRequest,
    ShareAccessRequest,
    ShareAccessResponse,
    ShareInfoOut,
    ShareLinkOut,
)

router = APIRouter(tags=["shares"])
logger = logging.getLogger(__name__)

MAX_ACTIVE_SHARES_PER_FILE = 10
SHARE_TOKEN_EXPIRE_MINUTES = 30


def now_ms() -> int:
    return int(time.time() * 1000)


def _share_select():
    return select(
        share_links,
        user_files.c.display_name.label("file_name"),
        user_files.c.stored_file_id,
        stored_files.c.content_hash,
        stored_files.c.real_path,
        stored_files.c.size_bytes,
        stored_files.c.is_directory,
    ).select_from(
        share_links.join(user_files, share_links.c.user_file_id == user_files.c.id)
        .join(stored_files, user_files.c.stored_file_id == stored_files.c.id)
    )


def _is_share_active(share: dict[str, Any]) -> bool:
    if share["status"] != "active":
        return False
    expires_at_ms = share["expires_at_ms"]
    if expires_at_ms is not None and int(expires_at_ms) <= now_ms():
        return False
    max_downloads = share["max_downloads"]
    if max_downloads is not None and int(share["download_count"]) >= int(max_downloads):
        return False
    return True


def _share_to_out(share: dict[str, Any], file_name: str, file_size: int) -> ShareLinkOut:
    return ShareLinkOut(
        id=int(share["id"]),
        share_code=share["share_code"],
        file_name=file_name,
        file_size=file_size,
        has_password=share["password_hash"] is not None,
        expires_at=ms_to_iso(share["expires_at_ms"]),
        max_downloads=share["max_downloads"],
        download_count=int(share["download_count"]),
        status=share["status"],
        created_at=ms_to_iso(share["created_at_ms"]) or "",
        last_accessed_at=ms_to_iso(share["last_accessed_at_ms"]),
    )


def _generate_share_code() -> str:
    return secrets.token_urlsafe(6)[:8]


def _create_access_token(share_code: str) -> str:
    payload = {
        "sub": share_code,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=SHARE_TOKEN_EXPIRE_MINUTES),
        "type": "share_access",
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def _verify_access_token(share_code: str, token: str) -> bool:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        return payload.get("sub") == share_code and payload.get("type") == "share_access"
    except jwt.PyJWTError:
        return False


async def _get_owned_file(user_id: int, user_file_id: int) -> dict[str, Any] | None:
    stmt = (
        select(
            user_files.c.id.label("user_file_id"),
            user_files.c.display_name,
            stored_files.c.size_bytes,
        )
        .select_from(user_files.join(stored_files, user_files.c.stored_file_id == stored_files.c.id))
        .where(user_files.c.id == user_file_id, user_files.c.user_id == user_id)
    )
    async with transaction() as conn:
        row = (await conn.execute(stmt)).mappings().first()
    return dict(row) if row else None


@router.post("/api/shares", status_code=status.HTTP_201_CREATED)
async def create_share(
    req: CreateShareRequest,
    user: AuthUser = Depends(require_user),
) -> ShareLinkOut:
    user_file = await _get_owned_file(user.id, req.user_file_id)
    if not user_file:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")

    timestamp = now_ms()
    async with transaction() as conn:
        active_count = (
            await conn.execute(
                select(func.count()).select_from(share_links).where(
                    share_links.c.user_file_id == req.user_file_id,
                    share_links.c.status == "active",
                    (share_links.c.expires_at_ms.is_(None) | (share_links.c.expires_at_ms > timestamp)),
                    (
                        share_links.c.max_downloads.is_(None)
                        | (share_links.c.download_count < share_links.c.max_downloads)
                    ),
                )
            )
        ).scalar_one()
        if int(active_count or 0) >= MAX_ACTIVE_SHARES_PER_FILE:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"每个文件最多 {MAX_ACTIVE_SHARES_PER_FILE} 个活跃分享",
            )

        share: dict[str, Any] | None = None
        for attempt in range(5):
            expires_at_ms = timestamp + req.expires_in * 1000 if req.expires_in else None
            try:
                share = (
                    await conn.execute(
                        insert(share_links)
                        .values(
                            share_code=_generate_share_code(),
                            owner_id=user.id,
                            user_file_id=req.user_file_id,
                            password_hash=hash_password(req.password) if req.password else None,
                            expires_at_ms=expires_at_ms,
                            max_downloads=req.max_downloads,
                            download_count=0,
                            status="active",
                            created_at_ms=timestamp,
                        )
                        .returning(share_links)
                    )
                ).mappings().one()
                share = dict(share)
                break
            except IntegrityError:
                if attempt == 4:
                    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "分享码生成失败，请重试")

    if share is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "分享创建失败")
    logger.info("创建分享 user_id=%s file_id=%s code=%s", user.id, req.user_file_id, share["share_code"])
    return _share_to_out(share, user_file["display_name"] or "未命名", int(user_file["size_bytes"] or 0))


@router.get("/api/shares")
async def list_shares(user: AuthUser = Depends(require_user)) -> list[ShareLinkOut]:
    async with transaction() as conn:
        rows = (
            await conn.execute(
                _share_select()
                .where(share_links.c.owner_id == user.id)
                .order_by(share_links.c.id.desc())
            )
        ).mappings().all()
    return [
        _share_to_out(dict(row), row["file_name"] or "未命名", int(row["size_bytes"] or 0))
        for row in rows
    ]


@router.put("/api/shares/{share_id}/revoke")
async def revoke_share(
    share_id: int,
    user: AuthUser = Depends(require_user),
) -> dict:
    async with transaction() as conn:
        current = (
            await conn.execute(
                select(share_links.c.status).where(
                    share_links.c.id == share_id,
                    share_links.c.owner_id == user.id,
                )
            )
        ).first()
        if not current:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "分享不存在")
        if current[0] == "revoked":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "分享已失效")
        await conn.execute(
            update(share_links)
            .where(share_links.c.id == share_id, share_links.c.owner_id == user.id)
            .values(status="revoked")
        )
    logger.info("失效分享 user_id=%s share_id=%s", user.id, share_id)
    return {"ok": True}


@router.delete("/api/shares/{share_id}")
async def delete_share(
    share_id: int,
    user: AuthUser = Depends(require_user),
) -> dict:
    async with transaction() as conn:
        result = await conn.execute(
            delete(share_links).where(share_links.c.id == share_id, share_links.c.owner_id == user.id)
        )
    if not result.rowcount:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "分享不存在")
    logger.info("删除分享 user_id=%s share_id=%s", user.id, share_id)
    return {"ok": True}


@router.put("/api/shares/revoke-all")
async def revoke_all_shares(user: AuthUser = Depends(require_user)) -> dict:
    async with transaction() as conn:
        result = await conn.execute(
            update(share_links)
            .where(share_links.c.owner_id == user.id, share_links.c.status == "active")
            .values(status="revoked")
        )
    count = int(result.rowcount or 0)
    logger.info("批量失效分享 user_id=%s count=%s", user.id, count)
    return {"ok": True, "count": count}


async def _get_share_with_file(code: str) -> dict[str, Any]:
    async with transaction() as conn:
        share = (
            await conn.execute(_share_select().where(share_links.c.share_code == code))
        ).mappings().first()
        if not share:
            existing = (
                await conn.execute(select(share_links.c.id).where(share_links.c.share_code == code))
            ).first()
            if existing:
                raise HTTPException(status.HTTP_410_GONE, "文件已删除")
            raise HTTPException(status.HTTP_404_NOT_FOUND, "分享不存在")
    return dict(share)


@router.get("/api/s/{code}")
async def get_share_info(code: str, request: Request) -> ShareInfoOut:
    await ensure_public_allowed(
        client_ip_from_request(request),
        RateLimitScope.PUBLIC_API,
        detail="请求过于频繁",
    )
    row = await _get_share_with_file(code)
    return ShareInfoOut(
        file_name=row["file_name"] or "未命名",
        file_size=int(row["size_bytes"] or 0),
        is_directory=bool(row["is_directory"]),
        has_password=row["password_hash"] is not None,
        is_expired=row["status"] != "active" or (
            row["expires_at_ms"] is not None and int(row["expires_at_ms"]) <= now_ms()
        ),
        is_exhausted=(
            row["max_downloads"] is not None
            and int(row["download_count"]) >= int(row["max_downloads"])
        ),
    )


@router.post("/api/s/{code}/access")
async def access_share(
    code: str,
    req: ShareAccessRequest,
    request: Request,
) -> ShareAccessResponse:
    await ensure_share_access_allowed(
        client_ip_from_request(request),
        code,
        detail="请求过于频繁",
    )
    share = await _get_share_with_file(code)
    if not _is_share_active(share):
        raise HTTPException(status.HTTP_410_GONE, "分享已失效")
    if not share["password_hash"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该分享无需密码")
    if not verify_password(req.password, share["password_hash"]):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "密码错误")
    return ShareAccessResponse(access_token=_create_access_token(code))


async def _check_share_access(
    code: str,
    token: str | None,
    _request: Request,
) -> dict[str, Any]:
    share = await _get_share_with_file(code)
    if not _is_share_active(share):
        raise HTTPException(status.HTTP_410_GONE, "分享已失效")
    if share["password_hash"] and (not token or not _verify_access_token(code, token)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要密码验证")
    return share


@router.get("/api/s/{code}/download")
async def download_shared_file(
    code: str,
    request: Request,
    token: str | None = Query(default=None),
    subpath: str | None = Query(default=None),
):
    client_ip = client_ip_from_request(request)
    await ensure_public_allowed(
        client_ip,
        RateLimitScope.ANONYMOUS_DOWNLOAD,
        detail="下载请求过于频繁，请稍后再试",
    )
    share = await _check_share_access(code, token, request)
    acquire_result = await download_limiter.acquire_anonymous(client_ip, share["content_hash"])
    if not acquire_result.allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, acquire_result.detail())

    lease = acquire_result.lease
    try:
        base_path = Path(share["real_path"])
        if not base_path.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
        if subpath:
            if not share["is_directory"]:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "该文件不是目录")
            target = _validate_subpath(base_path, subpath)
            if not target.exists():
                raise HTTPException(status.HTTP_404_NOT_FOUND, "子文件不存在")
            if target.is_dir():
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能下载目录")
            filename = target.name
        else:
            if share["is_directory"]:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "请指定子文件路径")
            target = base_path
            filename = share["file_name"] or "download"

        should_count_download = not request.headers.get("range")
        timestamp = now_ms()
        async with transaction() as conn:
            conditions = [
                share_links.c.id == share["id"],
                share_links.c.status == "active",
                (share_links.c.expires_at_ms.is_(None) | (share_links.c.expires_at_ms > timestamp)),
            ]
            if share["max_downloads"] is not None:
                conditions.append(share_links.c.download_count < share["max_downloads"])

            values: dict[str, Any] = {"last_accessed_at_ms": timestamp}
            if should_count_download:
                values["download_count"] = share_links.c.download_count + 1

            result = await conn.execute(
                update(share_links)
                .where(*conditions)
                .values(**values)
            )
            if result.rowcount == 0:
                raise HTTPException(status.HTTP_410_GONE, "分享已失效或下载次数已用完")
        return _tracked_response(_range_file_response(request, target, filename), lease)
    except Exception:
        if lease is not None:
            await lease.release()
        raise


@router.get("/api/s/{code}/browse")
async def browse_shared_directory(
    code: str,
    request: Request,
    token: str | None = Query(default=None),
    subpath: str = Query(default=""),
) -> list[dict]:
    await ensure_public_allowed(
        client_ip_from_request(request),
        RateLimitScope.PUBLIC_API,
        detail="请求过于频繁",
    )
    share = await _check_share_access(code, token, request)
    if not share["is_directory"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该文件不是目录")
    entries = await _directory_entries(
        int(share["stored_file_id"]),
        _normalize_entry_parent(subpath),
    )
    async with transaction() as conn:
        await conn.execute(
            update(share_links)
            .where(share_links.c.id == share["id"])
            .values(last_accessed_at_ms=now_ms())
        )
    return entries
