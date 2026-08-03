"""孤儿文件清理模块

在应用启动时清理 store 目录中没有对应数据库记录的物理文件。
这些孤儿文件可能由于进程在数据库事务提交后、物理文件删除前崩溃而产生。
"""

import logging
from pathlib import Path

from app.repositories.files import list_stored_file_real_paths
from app.services.storage import get_store_dir, safe_delete_path

logger = logging.getLogger(__name__)


def _resolve_real_paths(paths: set[str]) -> set[str]:
    return {str(Path(path).resolve(strict=False)) for path in paths}


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

    db_paths = _resolve_real_paths(await list_stored_file_real_paths())

    candidates: list[Path] = []
    for top_level in store_dir.iterdir():
        if not top_level.is_dir():
            continue
        if top_level.name != "v2":
            candidates.extend(top_level.iterdir())
            continue
        for object_kind in top_level.iterdir():
            if not object_kind.is_dir() or object_kind.name not in {"file", "directory"}:
                continue
            for prefix_dir in object_kind.iterdir():
                if prefix_dir.is_dir():
                    candidates.extend(prefix_dir.iterdir())

    orphan_count = 0
    for object_path in candidates:
        item_path = str(object_path.resolve(strict=False))
        if item_path in db_paths:
            continue
        current_db_paths = _resolve_real_paths(await list_stored_file_real_paths())
        if item_path in current_db_paths:
            logger.debug("Skipping newly registered stored file: %s", object_path)
            continue
        try:
            deleted = safe_delete_path(
                base_dir=store_dir,
                target=object_path,
                recursive=object_path.is_dir(),
                allow_missing=True,
            )
            if deleted:
                orphan_count += 1
                logger.info("Deleted orphan file: %s", object_path)
        except Exception as exc:
            logger.error("Failed to delete orphan file %s: %s", object_path, exc)

    if orphan_count > 0:
        logger.info("Orphan cleanup completed: deleted %d files", orphan_count)
    else:
        logger.debug("Orphan cleanup completed: no orphan files found")

    return orphan_count
