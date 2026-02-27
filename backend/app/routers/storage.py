"""存储文件管理路由（管理员专用）

提供对 store 目录中存储文件的管理功能：
- 列出所有存储文件及引用用户数
- 查看引用某文件的用户列表
- 批量删除存储文件
- 扫描 store 目录补建缺失的 StoredFile 记录
- 修复 Task 与 StoredFile 的关联
"""

import asyncio
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlmodel import col, select

from app.auth import require_admin
from app.database import get_session
from app.models import StoredFile, User, UserFile
from app.services.storage import delete_user_file_reference, get_store_path_for_hash

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/storage", tags=["admin-storage"])


# ============ Schemas ============


class StoredFileInfo(BaseModel):
    """存储文件信息"""

    id: int
    content_hash: str
    original_name: str
    size: int
    is_directory: bool
    ref_count: int
    created_at: str
    real_path: str
    exists_on_disk: bool  # 文件是否存在于磁盘


class StoredFileListResponse(BaseModel):
    """存储文件列表响应"""

    files: list[StoredFileInfo]
    total: int


class FileUserInfo(BaseModel):
    """引用文件的用户信息"""

    user_id: int
    username: str
    display_name: str  # 用户给文件的显示名称


class FileUsersResponse(BaseModel):
    """文件引用用户列表响应"""

    file_id: int
    users: list[FileUserInfo]


class BulkDeleteRequest(BaseModel):
    """批量删除请求"""

    file_ids: list[int] = Field(..., min_length=1, max_length=1000)


class BulkDeleteResponse(BaseModel):
    deleted_count: int
    failed_ids: list[int]
    errors: list[str]


class ScanResult(BaseModel):
    scanned_dirs: int
    new_records: int
    already_exists: int
    errors: list[str]


class RepairResult(BaseModel):
    tasks_checked: int
    tasks_repaired: int
    errors: list[str]


# ============ Endpoints ============


@router.get("/files", response_model=StoredFileListResponse)
async def list_stored_files(
    admin: User = Depends(require_admin),
    search: str = Query(default="", description="搜索文件名"),
    orphan_only: bool = Query(default=False, description="仅显示无引用的孤立文件"),
) -> StoredFileListResponse:
    """列出所有存储文件

    管理员可以查看 store 目录中的所有文件，包括：
    - 文件基本信息（名称、大小、哈希等）
    - 引用计数（有多少用户引用此文件）
    - 磁盘存在状态
    """
    async with get_session() as db:
        query = select(StoredFile)

        # 搜索过滤
        if search:
            query = query.where(col(StoredFile.original_name).contains(search))

        # 孤立文件过滤
        if orphan_only:
            query = query.where(StoredFile.ref_count <= 0)

        query = query.order_by(col(StoredFile.created_at).desc())

        result = await db.exec(query)
        stored_files = result.all()

        files = []
        for sf in stored_files:
            if sf.id is None:
                logger.warning("Skip stored_file with null id while listing admin storage files")
                continue
            exists_on_disk = Path(sf.real_path).exists()
            files.append(
                StoredFileInfo(
                    id=sf.id,
                    content_hash=sf.content_hash,
                    original_name=sf.original_name,
                    size=sf.size,
                    is_directory=sf.is_directory,
                    ref_count=sf.ref_count,
                    created_at=sf.created_at,
                    real_path=sf.real_path,
                    exists_on_disk=exists_on_disk,
                )
            )

        return StoredFileListResponse(files=files, total=len(files))


@router.get("/files/{file_id}/users", response_model=FileUsersResponse)
async def get_file_users(
    file_id: int,
    admin: User = Depends(require_admin),
) -> FileUsersResponse:
    """获取引用某文件的用户列表

    返回所有引用指定存储文件的用户信息，包括用户名和显示名称。
    """
    async with get_session() as db:
        # 验证文件存在
        result = await db.exec(select(StoredFile).where(StoredFile.id == file_id))
        stored_file = result.first()
        if not stored_file:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"存储文件不存在: {file_id}",
            )

        # 查询引用用户
        query = (
            select(UserFile, User)
            .join(User, UserFile.owner_id == User.id)  # type: ignore[arg-type]
            .where(UserFile.stored_file_id == file_id)
        )
        result = await db.exec(query)
        rows = result.all()

        users = [
            FileUserInfo(
                user_id=user.id,
                username=user.username,
                display_name=user_file.display_name,
            )
            for user_file, user in rows
            if user.id is not None
        ]

        return FileUsersResponse(file_id=file_id, users=users)


@router.delete("/files", response_model=BulkDeleteResponse)
async def bulk_delete_files(
    request: BulkDeleteRequest,
    admin: User = Depends(require_admin),
) -> BulkDeleteResponse:
    """批量删除存储文件

    删除指定的存储文件，同时：
    - 删除所有用户对该文件的引用（UserFile）
    - 删除物理文件
    - 删除数据库记录
    """
    deleted_count = 0
    failed_ids: list[int] = []
    errors: list[str] = []

    async with get_session() as db:
        for file_id in request.file_ids:
            try:
                result = await db.exec(
                    select(StoredFile).where(StoredFile.id == file_id)
                )
                stored_file = result.first()

                if not stored_file:
                    failed_ids.append(file_id)
                    errors.append(f"文件不存在: {file_id}")
                    continue

                user_files = (
                    await db.exec(
                        select(UserFile).where(UserFile.stored_file_id == file_id)
                    )
                ).all()

                # 保存 content_hash 用于后续删除物理文件
                content_hash = stored_file.content_hash

                for uf in user_files:
                    if uf.id is not None:
                        try:
                            await delete_user_file_reference(uf.id)
                        except Exception as e:
                            logger.warning(f"删除用户文件引用失败 {uf.id}: {e!s}")

                # delete_user_file_reference 会在 ref_count 归零时自动删除
                # StoredFile 记录和物理文件。但如果有引用删除失败，
                # StoredFile 可能仍然存在，需要重新查询确认。
                db.expire_all()
                remaining = await db.exec(
                    select(StoredFile).where(StoredFile.id == file_id)
                )
                leftover = remaining.first()
                if leftover:
                    # 仍有残留记录，强制删除
                    store_path = get_store_path_for_hash(leftover.content_hash)
                    await db.delete(leftover)
                    await db.commit()
                    if store_path.exists():
                        if store_path.is_dir():
                            await asyncio.to_thread(shutil.rmtree, store_path)
                        else:
                            await asyncio.to_thread(store_path.unlink)

                deleted_count += 1
                logger.info(f"管理员删除存储文件: {content_hash}")

            except Exception as e:
                failed_ids.append(file_id)
                errors.append(f"删除失败 {file_id}: {e!s}")
                logger.exception(f"删除存储文件失败: {file_id}")

    return BulkDeleteResponse(
        deleted_count=deleted_count,
        failed_ids=failed_ids,
        errors=errors,
    )
