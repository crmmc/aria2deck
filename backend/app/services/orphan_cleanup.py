"""孤儿文件清理模块

在应用启动时清理 store 目录中没有对应数据库记录的物理文件。
这些孤儿文件可能由于进程在数据库事务提交后、物理文件删除前崩溃而产生。
"""

import logging

from app.repositories.files import list_stored_file_real_paths
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

    db_paths = await list_stored_file_real_paths()

    # 扫描 store 目录：结构为 /store/{prefix}/{content_hash}
    # 只删除 hash 级别的目录/文件，不删除 prefix 目录
    orphan_count = 0
    for prefix_dir in store_dir.iterdir():
        if not prefix_dir.is_dir():
            # 顶层不应有文件，跳过
            continue
        # 扫描 prefix 目录下的 hash 目录
        for hash_item in prefix_dir.iterdir():
            item_path = str(hash_item.resolve())
            if item_path not in db_paths:
                try:
                    deleted = safe_delete_path(
                        base_dir=store_dir,
                        target=hash_item,
                        recursive=hash_item.is_dir(),
                        allow_missing=True,
                    )
                    if deleted:
                        orphan_count += 1
                        logger.info("Deleted orphan file: %s", hash_item)
                except Exception as e:
                    logger.error("Failed to delete orphan file %s: %s", hash_item, e)
        # 如果 prefix 目录变空，也删除它
        try:
            if prefix_dir.exists() and not any(prefix_dir.iterdir()):
                prefix_dir.rmdir()
                logger.debug("Removed empty prefix directory: %s", prefix_dir)
        except OSError:
            pass  # 目录非空或其他原因，忽略

    if orphan_count > 0:
        logger.info("Orphan cleanup completed: deleted %d files", orphan_count)
    else:
        logger.debug("Orphan cleanup completed: no orphan files found")

    return orphan_count
