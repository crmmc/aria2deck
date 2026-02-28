"""后台任务异常处理模板。

使用示例:
    async def my_background_task():
        async with background_task_guard("my_task"):
            # 你的业务逻辑
            await do_something()
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

logger = logging.getLogger(__name__)


@asynccontextmanager
async def background_task_guard(
    task_name: str,
    *,
    reraise: bool = False,
) -> AsyncGenerator[None, None]:
    """后台任务异常保护上下文管理器。

    Args:
        task_name: 任务名称，用于日志标识
        reraise: 是否重新抛出异常（默认 False，吞掉异常继续运行）
    """
    try:
        yield
    except Exception as exc:
        logger.warning(f"[{task_name}] 执行失败，跳过: {exc}")
        if reraise:
            raise
