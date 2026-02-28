"""孤儿文件清理模块

在应用启动时清理 store 目录中没有对应数据库记录的物理文件。
这些孤儿文件可能由于进程在数据库事务提交后、物理文件删除前崩溃而产生。
"""
import logging

from sqlmodel import select

from app.database import get_session
from app.models import StoredFile
from app.services.storage import get_store_dir, safe_delete_path

logger = logging.getLogger(__name__)


async def cleanup_orphan_files() -> int:
    """清理 store 目录中的孤儿文件。

    扫描 store 目录，删除没有对应 StoredFile 记录的物理文件。

    Returns:
        删除的孤儿文件数量
    """
    store_dir = get_store_dir()
    if not store_dir.exists():
        logger.debug("Store directory does not exist, skipping orphan cleanup")
        return 0

    # 获取数据库中所有 StoredFile 的 real_path
    async with get_session() as db:
        result = await db.exec(select(StoredFile.real_path))
        db_paths = set(result.all())

    # 扫描 store 目录中的所有文件和目录（只扫描顶层）
    orphan_count = 0
    for item in store_dir.iterdir():
        item_path = str(item.resolve())
        if item_path not in db_paths:
            try:
                deleted = safe_delete_path(
                    base_dir=store_dir,
                    target=item,
                    recursive=item.is_dir(),
                    allow_missing=True,
                )
                if deleted:
                    orphan_count += 1
                    logger.info("Deleted orphan file: %s", item)
            except Exception as e:
                logger.error("Failed to delete orphan file %s: %s", item, e)

    if orphan_count > 0:
        logger.info("Orphan cleanup completed: deleted %d files", orphan_count)
    else:
        logger.debug("Orphan cleanup completed: no orphan files found")

    return orphan_count
