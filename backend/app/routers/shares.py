"""文件分享接口

已认证端点：分享管理（CRUD + 批量失效）
公开端点：分享访问（信息/密码验证/下载/浏览）
"""
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from sqlmodel import select, func

from app.auth import require_user
from app.core.config import settings
from app.core.rate_limit import RateLimiter
from app.core.security import hash_password, verify_password
from app.database import get_session
from app.models import ShareLink, StoredFile, User, UserFile, utc_now, utc_now_str
from app.schemas import (
    CreateShareRequest,
    ShareAccessRequest,
    ShareAccessResponse,
    ShareInfoOut,
    ShareLinkOut,
)
from app.routers.files import _validate_subpath, _range_file_response

router = APIRouter(tags=["shares"])
logger = logging.getLogger(__name__)

# 限流器
_share_access_limiter = RateLimiter(max_requests=60, window_seconds=60)  # 每IP 60次/分
_share_password_limiter = RateLimiter(max_requests=5, window_seconds=60)  # 每分享码 5次/分
_share_download_limiter = RateLimiter(max_requests=30, window_seconds=60)  # 每IP 30次/分

# 每文件最大活跃分享数
MAX_ACTIVE_SHARES_PER_FILE = 10

# JWT 配置
SHARE_TOKEN_EXPIRE_MINUTES = 30

def _is_share_active(share: ShareLink) -> bool:
    """判断分享是否有效（状态 + 过期 + 次数）"""
    if share.status != "active":
        return False
    if share.expires_at:
        try:
            exp = datetime.fromisoformat(share.expires_at)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= utc_now():
                return False
        except ValueError:
            return False
    if share.max_downloads is not None and share.download_count >= share.max_downloads:
        return False
    return True


def _share_to_out(share: ShareLink, file_name: str, file_size: int) -> ShareLinkOut:
    return ShareLinkOut(
        id=share.id,  # type: ignore[arg-type]
        share_code=share.share_code,
        file_name=file_name,
        file_size=file_size,
        has_password=share.password_hash is not None,
        expires_at=share.expires_at,
        max_downloads=share.max_downloads,
        download_count=share.download_count,
        status=share.status,
        created_at=share.created_at,
        last_accessed_at=share.last_accessed_at,
    )


def _generate_share_code() -> str:
    """生成 8 字符 URL-safe 短码"""
    return secrets.token_urlsafe(6)[:8]


def _create_access_token(share_code: str) -> str:
    """为有密码的分享签发短期 JWT"""
    payload = {
        "sub": share_code,
        "exp": utc_now() + timedelta(minutes=SHARE_TOKEN_EXPIRE_MINUTES),
        "type": "share_access",
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def _verify_access_token(share_code: str, token: str) -> bool:
    """验证分享访问 token"""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        return (
            payload.get("sub") == share_code
            and payload.get("type") == "share_access"
        )
    except jwt.PyJWTError:
        return False
# ========== 已认证端点：分享管理 ==========
@router.post("/api/shares", status_code=status.HTTP_201_CREATED)
async def create_share(
    req: CreateShareRequest,
    user: User = Depends(require_user),
) -> ShareLinkOut:
    """创建文件分享链接"""
    async with get_session() as db:
        # 查找用户文件
        result = await db.exec(
            select(UserFile).where(
                UserFile.id == req.user_file_id,
                UserFile.owner_id == user.id,
            )
        )
        user_file = result.first()
        if not user_file:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
        # 检查活跃分享数上限
        now_str = utc_now_str()
        active_count_result = await db.exec(
            select(func.count()).select_from(ShareLink).where(
                ShareLink.user_file_id == req.user_file_id,
                ShareLink.status == "active",
            )
        )
        active_count = active_count_result.one()
        if active_count >= MAX_ACTIVE_SHARES_PER_FILE:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"每个文件最多 {MAX_ACTIVE_SHARES_PER_FILE} 个活跃分享"
            )
        # 生成分享码（重试以避免冲突）
        for _ in range(5):
            code = _generate_share_code()
            existing = await db.exec(
                select(ShareLink).where(ShareLink.share_code == code)
            )
            if not existing.first():
                break
        else:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "分享码生成失败，请重试")
        # 计算过期时间
        expires_at = None
        if req.expires_in:
            expires_at = (utc_now() + timedelta(seconds=req.expires_in)).isoformat()
        # 密码哈希
        pwd_hash = hash_password(req.password) if req.password else None
        share = ShareLink(
            share_code=code,
            owner_id=user.id,  # type: ignore[arg-type]
            user_file_id=req.user_file_id,
            password_hash=pwd_hash,
            expires_at=expires_at,
            max_downloads=req.max_downloads,
            created_at=now_str,
        )
        db.add(share)

        # 获取文件信息用于响应
        file_name = user_file.display_name or "未命名"
        # 获取文件大小需要查 StoredFile
        stored = await db.exec(
            select(StoredFile).where(StoredFile.id == user_file.stored_file_id)
        )
        sf = stored.first()
        file_size = sf.size if sf else 0
    logger.info(
        "创建分享 user_id=%s file_id=%s code=%s",
        user.id, req.user_file_id, code,
    )
    return _share_to_out(share, file_name, file_size)
@router.get("/api/shares")
async def list_shares(user: User = Depends(require_user)) -> list[ShareLinkOut]:
    """列出当前用户的所有分享"""
    async with get_session() as db:
        result = await db.exec(
            select(ShareLink, UserFile, StoredFile)
            .join(UserFile, ShareLink.user_file_id == UserFile.id)  # type: ignore[arg-type]
            .join(StoredFile, UserFile.stored_file_id == StoredFile.id)  # type: ignore[arg-type]
            .where(ShareLink.owner_id == user.id)
            .order_by(ShareLink.id.desc())
        )
        rows = result.all()
    return [
        _share_to_out(share, uf.display_name or "未命名", sf.size)
        for share, uf, sf in rows
    ]
@router.put("/api/shares/{share_id}/revoke")
async def revoke_share(
    share_id: int,
    user: User = Depends(require_user),
) -> dict:
    """失效单个分享"""
    async with get_session() as db:
        result = await db.exec(
            select(ShareLink).where(
                ShareLink.id == share_id,
                ShareLink.owner_id == user.id,
            )
        )
        share = result.first()
        if not share:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "分享不存在")
        if share.status == "revoked":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "分享已失效")
        share.status = "revoked"
    logger.info("失效分享 user_id=%s share_id=%s", user.id, share_id)
    return {"ok": True}
@router.delete("/api/shares/{share_id}")
async def delete_share(
    share_id: int,
    user: User = Depends(require_user),
) -> dict:
    """删除分享记录"""
    async with get_session() as db:
        result = await db.exec(
            select(ShareLink).where(
                ShareLink.id == share_id,
                ShareLink.owner_id == user.id,
            )
        )
        share = result.first()
        if not share:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "分享不存在")
        await db.delete(share)
    logger.info("删除分享 user_id=%s share_id=%s", user.id, share_id)
    return {"ok": True}
@router.put("/api/shares/revoke-all")
async def revoke_all_shares(user: User = Depends(require_user)) -> dict:
    """一键失效当前用户的所有活跃分享"""
    async with get_session() as db:
        result = await db.exec(
            select(ShareLink).where(
                ShareLink.owner_id == user.id,
                ShareLink.status == "active",
            )
        )
        shares = result.all()
        count = 0
        for share in shares:
            share.status = "revoked"
            count += 1
    logger.info("批量失效分享 user_id=%s count=%s", user.id, count)
    return {"ok": True, "count": count}

# ========== 公开端点：分享访问 ==========
async def _get_share_with_file(code: str) -> tuple[ShareLink, UserFile, StoredFile]:
    """通过分享码获取分享 + 用户文件 + 存储文件"""
    async with get_session() as db:
        result = await db.exec(
            select(ShareLink, UserFile, StoredFile)
            .join(UserFile, ShareLink.user_file_id == UserFile.id)  # type: ignore[arg-type]
            .join(StoredFile, UserFile.stored_file_id == StoredFile.id)  # type: ignore[arg-type]
            .where(ShareLink.share_code == code)
        )
        row = result.first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "分享不存在")
    return row[0], row[1], row[2]
@router.get("/api/s/{code}")
async def get_share_info(code: str, request: Request) -> ShareInfoOut:
    """获取分享元信息（无需登录）"""
    client_ip = request.client.host if request.client else "unknown"
    if not await _share_access_limiter.is_allowed(client_ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "请求过于频繁")
    share, user_file, stored_file = await _get_share_with_file(code)
    is_expired = False
    if share.expires_at:
        try:
            exp = datetime.fromisoformat(share.expires_at)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            is_expired = exp <= utc_now()
        except ValueError:
            is_expired = True
    is_exhausted = (
        share.max_downloads is not None
        and share.download_count >= share.max_downloads
    )
    return ShareInfoOut(
        file_name=user_file.display_name or "未命名",
        file_size=stored_file.size,
        is_directory=stored_file.is_directory,
        has_password=share.password_hash is not None,
        is_expired=is_expired or share.status != "active",
        is_exhausted=is_exhausted,
    )

@router.post("/api/s/{code}/access")
async def access_share(
    code: str,
    req: ShareAccessRequest,
    request: Request,
) -> ShareAccessResponse:
    """验证分享密码，返回短期 access_token"""
    client_ip = request.client.host if request.client else "unknown"
    if not await _share_password_limiter.is_allowed(f"{code}:{client_ip}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "请求过于频繁")
    share, _, _ = await _get_share_with_file(code)
    if not _is_share_active(share):
        raise HTTPException(status.HTTP_410_GONE, "分享已失效")
    if not share.password_hash:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该分享无需密码")
    if not verify_password(req.password, share.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "密码错误")
    token = _create_access_token(code)
    return ShareAccessResponse(access_token=token)

async def _check_share_access(
    code: str, token: str | None, request: Request,
) -> tuple[ShareLink, UserFile, StoredFile]:
    """\u516c\u5f00\u7aef\u70b9\u901a\u7528\u8bbf\u95ee\u68c0\u67e5\uff1a\u9650\u6d41 + \u6709\u6548\u6027 + \u5bc6\u7801\u9a8c\u8bc1"""
    client_ip = request.client.host if request.client else "unknown"
    if not await _share_download_limiter.is_allowed(client_ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "请求过于频繁")
    share, user_file, stored_file = await _get_share_with_file(code)
    if not _is_share_active(share):
        raise HTTPException(status.HTTP_410_GONE, "\u5206\u4eab\u5df2\u5931\u6548")
    # \u5bc6\u7801\u4fdd\u62a4\u7684\u5206\u4eab\u9700\u8981 token
    if share.password_hash:
        if not token or not _verify_access_token(code, token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "\u9700\u8981\u5bc6\u7801\u9a8c\u8bc1")
    return share, user_file, stored_file


@router.get("/api/s/{code}/download")
async def download_shared_file(
    code: str,
    request: Request,
    token: str | None = Query(default=None),
    subpath: str | None = Query(default=None),
):
    """\u4e0b\u8f7d\u5206\u4eab\u6587\u4ef6"""
    share, user_file, stored_file = await _check_share_access(code, token, request)
    base_path = Path(stored_file.real_path)
    if not base_path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "\u6587\u4ef6\u4e0d\u5b58\u5728")
    # \u786e\u5b9a\u4e0b\u8f7d\u76ee\u6807
    if subpath:
        if not stored_file.is_directory:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "\u8be5\u6587\u4ef6\u4e0d\u662f\u76ee\u5f55")
        target = _validate_subpath(base_path, subpath)
        if not target.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "\u5b50\u6587\u4ef6\u4e0d\u5b58\u5728")
        if target.is_dir():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "\u4e0d\u80fd\u4e0b\u8f7d\u76ee\u5f55")
        filename = target.name
    else:
        if stored_file.is_directory:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "\u8bf7\u6307\u5b9a\u5b50\u6587\u4ef6\u8def\u5f84")
        target = base_path
        filename = user_file.display_name or "download"
    # 原子递增下载计数（含 max_downloads 竞态保护）
    async with get_session() as db:
        if share.max_downloads is not None:
            result = await db.execute(
                ShareLink.__table__.update()  # type: ignore[attr-defined]
                .where(
                    ShareLink.id == share.id,  # type: ignore[arg-type]
                    ShareLink.download_count < share.max_downloads,
                )
                .values(
                    download_count=ShareLink.download_count + 1,
                    last_accessed_at=utc_now_str(),
                )
            )
            if result.rowcount == 0:  # type: ignore[union-attr]
                raise HTTPException(status.HTTP_410_GONE, "下载次数已用完")
        else:
            await db.execute(
                ShareLink.__table__.update()  # type: ignore[attr-defined]
                .where(ShareLink.id == share.id)  # type: ignore[arg-type]
                .values(
                    download_count=ShareLink.download_count + 1,
                    last_accessed_at=utc_now_str(),
                )
            )
    return _range_file_response(request, target, filename)


@router.get("/api/s/{code}/browse")
async def browse_shared_directory(
    code: str,
    request: Request,
    token: str | None = Query(default=None),
    subpath: str = Query(default=""),
) -> list[dict]:
    """浏览分享的 BT 文件夹内容"""
    share, user_file, stored_file = await _check_share_access(code, token, request)
    if not stored_file.is_directory:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该文件不是目录")
    base_path = Path(stored_file.real_path)
    if not base_path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    target = _validate_subpath(base_path, subpath)
    if not target.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "路径不存在")
    if not target.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不是目录")
    entries = []
    for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        entries.append({
            "name": item.name,
            "is_dir": item.is_dir(),
            "size": item.stat().st_size if item.is_file() else 0,
            "path": str(item.relative_to(base_path)),
        })
    # 更新最后访问时间
    async with get_session() as db:
        result = await db.exec(
            select(ShareLink).where(ShareLink.id == share.id)  # type: ignore[arg-type]
        )
        db_share = result.first()
        if db_share:
            db_share.last_accessed_at = utc_now_str()
    return entries