"""Async folder packing service using 7zz/tar CLI"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

from sqlalchemy import update
from sqlmodel import select, func

from app.core.config import settings
from app.database import get_session
from app.models import PackTask, User


# 全局打包队列锁
_pack_queue_lock = asyncio.Lock()
# 保护 _running_tasks 字典的锁
_running_tasks_lock = asyncio.Lock()

SUPPORTED_FORMATS = ("zip", "7z")
SUPPORTED_7Z_METHODS = ("lzma2", "zstd")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 允许的 7zz 参数前缀白名单（防止命令注入）
_ALLOWED_7ZZ_ARG_PREFIXES = (
    "-mmt",      # 多线程
    "-mx",       # 压缩级别
    "-m0=",      # 压缩方法
    "-ms",       # 固实压缩
    "-mf",       # 过滤器
    "-mhc",      # 头压缩
    "-mhe",      # 头加密
    "-mmemuse",  # 内存限制
    "-p",        # 密码（允许用户加密）
)


class PackTaskManager:
    """Manages async pack task execution"""

    _running_tasks: dict[int, asyncio.subprocess.Process] = {}

    @classmethod
    def get_pack_format(cls) -> str:
        from app.routers.config import get_config_value
        val = get_config_value("pack_format")
        return val if val in SUPPORTED_FORMATS else "zip"

    @classmethod
    def get_compression_level(cls) -> int:
        from app.routers.config import get_config_value
        val = get_config_value("pack_compression_level")
        try:
            level = int(val) if val else 5
            return max(0, min(9, level))
        except ValueError:
            return 5

    @classmethod
    def get_7z_method(cls) -> str:
        from app.routers.config import get_config_value
        val = get_config_value("pack_7z_method")
        return val if val in SUPPORTED_7Z_METHODS else "lzma2"

    @classmethod
    def get_extra_args(cls) -> list[str]:
        from app.routers.config import get_config_value
        val = get_config_value("pack_extra_args")
        if not val or not val.strip():
            return []
        try:
            args = shlex.split(val)
            # 只允许白名单中的参数前缀（防止命令注入）
            safe_args = []
            for arg in args:
                if any(arg.startswith(prefix) for prefix in _ALLOWED_7ZZ_ARG_PREFIXES):
                    safe_args.append(arg)
            return safe_args
        except ValueError:
            return []

    @classmethod
    def is_any_task_running(cls) -> bool:
        return len(cls._running_tasks) > 0

    @classmethod
    async def start_pack(
        cls,
        task_id: int,
        user_id: int,
        abs_paths: list[str],
        file_ids: list[int],
        output_name: str | None = None,
        delete_source: bool = False,
        on_progress: Callable[[int, int], None] | None = None
    ) -> None:
        """Start async packing process with global queue control

        Only one pack task runs at a time globally.
        abs_paths: list of absolute file paths to pack.
        file_ids: list of UserFile IDs (for ref_count deletion when delete_source=True).
        """
        async with _pack_queue_lock:
            await cls._do_pack(task_id, user_id, abs_paths, file_ids, output_name, delete_source, on_progress)

    @classmethod
    async def _do_pack(
        cls,
        task_id: int,
        user_id: int,
        abs_paths: list[str],
        file_ids: list[int],
        output_name: str | None = None,
        delete_source: bool = False,
        on_progress: Callable[[int, int], None] | None = None
    ) -> None:
        """Actually perform the packing (called within lock)"""
        user_dir = Path(settings.download_dir) / str(user_id)

        sources = [Path(p) for p in abs_paths]
        
        if not sources:
            await cls._update_task_error(task_id, "No valid source files")
            return
        
        # 验证所有路径存在
        for source in sources:
            if not source.exists():
                await cls._update_task_error(task_id, f"Path does not exist: {source.name}")
                return

        # Determine output format and path
        pack_format = cls.get_pack_format()
        compression = cls.get_compression_level()
        extra_args = cls.get_extra_args()

        if output_name:
            base_name = output_name
        elif len(sources) == 1:
            base_name = sources[0].name
        else:
            base_name = "archive"

        output_filename = f"{base_name}.{pack_format}"
        output_path = user_dir / output_filename

        counter = 1
        while output_path.exists():
            output_filename = f"{base_name}_{counter}.{pack_format}"
            output_path = user_dir / output_filename
            counter += 1

        async with get_session() as db:
            result = await db.execute(
                update(PackTask)
                .where(
                    PackTask.id == task_id,
                    PackTask.status == "pending"
                )
                .values(
                    status="packing",
                    output_path=str(output_path),
                    updated_at=utc_now()
                )
            )

            if result.rowcount == 0:
                logger.info(f"Pack task {task_id} status changed, skipping")
                return

        format_flag = f"-t{pack_format}"
        cmd = ["7zz", "a", format_flag, f"-mx={compression}", "-bsp1"]
        if pack_format == "7z":
            method = cls.get_7z_method()
            # 7zz 需要正确的大小写: LZMA2, Zstd
            method_map = {"lzma2": "LZMA2", "zstd": "Zstd"}
            cmd.append(f"-m0={method_map.get(method, 'LZMA2')}")
        if extra_args:
            cmd.extend(extra_args)
        cmd.append(str(output_path))
        for source in sources:
            cmd.append(str(source))

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            async with _running_tasks_lock:
                cls._running_tasks[task_id] = process

            async with get_session() as db:
                result = await db.exec(select(PackTask).where(PackTask.id == task_id))
                task_check = result.first()
                if task_check and task_check.status != "packing":
                    logger.info(f"Pack task {task_id} was cancelled during startup, terminating")
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        process.kill()
                    if output_path.exists():
                        output_path.unlink()
                    async with _running_tasks_lock:
                        cls._running_tasks.pop(task_id, None)
                    return

            progress = 0
            buf = b""
            full_output = b""
            while True:
                chunk = await process.stdout.read(256)
                if not chunk:
                    break
                buf += chunk
                full_output += chunk
                last_match = None
                for m in re.finditer(rb"(\d+)%", buf):
                    last_match = m
                if last_match:
                    new_progress = int(last_match.group(1))
                    buf = buf[last_match.end():]
                    if new_progress != progress:
                        progress = new_progress
                        async with get_session() as db:
                            result = await db.exec(select(PackTask).where(PackTask.id == task_id))
                            task = result.first()
                            if task:
                                task.progress = progress
                                task.updated_at = utc_now()
                                db.add(task)
                        if on_progress:
                            on_progress(task_id, progress)

            await process.wait()

            if process.returncode == 0:
                output_size = output_path.stat().st_size if output_path.exists() else 0

                stored_file_id = None
                if output_path.exists():
                    try:
                        from app.services.storage import register_pack_output
                        stored_file, _user_file = await register_pack_output(
                            output_path=output_path,
                            original_name=output_filename,
                            user_id=user_id,
                        )
                        stored_file_id = stored_file.id
                        logger.info(
                            "Pack output registered: task_id=%s stored_file_id=%s",
                            task_id, stored_file_id,
                        )
                    except Exception:
                        logger.exception("Failed to register pack output: task_id=%s", task_id)
                        if output_path.exists():
                            output_path.unlink()
                        await cls._update_task_error(task_id, "打包成功但注册文件失败，请重试")
                        return

                if stored_file_id and delete_source and file_ids:
                    from app.services.storage import delete_user_file_reference
                    for uf_id in file_ids:
                        try:
                            await delete_user_file_reference(uf_id)
                        except Exception:
                            logger.warning(
                                "Failed to delete source ref: user_file_id=%s", uf_id,
                            )

                async with get_session() as db:
                    result = await db.execute(
                        update(PackTask)
                        .where(
                            PackTask.id == task_id,
                            PackTask.status == "packing"
                        )
                        .values(
                            status="done",
                            progress=100,
                            output_size=output_size,
                            stored_file_id=stored_file_id,
                            reserved_space=0,
                            updated_at=utc_now()
                        )
                    )

                    if result.rowcount == 0:
                        logger.warning(f"Pack task {task_id} was cancelled during packing")
            else:
                if output_path.exists():
                    output_path.unlink()
                output_text = full_output.decode("utf-8", errors="replace")
                logger.error(f"Pack task {task_id} failed with code {process.returncode}:\n{output_text}")
                await cls._update_task_error(task_id, f"7zz exited with code {process.returncode}")

        except FileNotFoundError:
            await cls._update_task_error(task_id, "7zz command not found. Please install 7-Zip.")
        except asyncio.CancelledError:
            # Task was cancelled
            if output_path.exists():
                output_path.unlink()
            async with get_session() as db:
                result = await db.exec(select(PackTask).where(PackTask.id == task_id))
                task = result.first()
                if task:
                    task.status = "cancelled"
                    task.reserved_space = 0
                    task.updated_at = utc_now()
                    db.add(task)
        except Exception as exc:
            if output_path.exists():
                output_path.unlink()
            await cls._update_task_error(task_id, str(exc))
        finally:
            async with _running_tasks_lock:
                cls._running_tasks.pop(task_id, None)

    @classmethod
    async def cancel_pack(cls, task_id: int) -> bool:
        async with _running_tasks_lock:
            process = cls._running_tasks.get(task_id)
        if process:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()
            return True
        return False

    @classmethod
    async def _update_task_error(cls, task_id: int, error: str) -> None:
        async with get_session() as db:
            # Use CAS pattern: only pending or packing can become failed
            await db.execute(
                update(PackTask)
                .where(
                    PackTask.id == task_id,
                    PackTask.status.in_(["pending", "packing"])
                )
                .values(
                    status="failed",
                    error_message=error,
                    reserved_space=0,
                    updated_at=utc_now()
                )
            )


def calculate_folder_size(path: Path) -> int:
    """Calculate total size of folder in bytes"""
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except OSError as e:
        logger.error(f"Failed to calculate folder size for {path}: {e}")
        pass
    return total


async def get_reserved_space() -> int:
    """Get total reserved space from pending/packing tasks"""
    async with get_session() as db:
        result = await db.exec(
            select(func.coalesce(func.sum(PackTask.reserved_space), 0))
            .where(PackTask.status.in_(["pending", "packing"]))
        )
        total = result.first()
        return total if total else 0


async def get_server_available_space() -> int:
    """Get server available space minus reserved space"""
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    reserved = await get_reserved_space()
    return max(0, disk.free - reserved)


async def get_user_available_space_for_pack(user_id: int) -> int:
    """Get user available space for pack (considers quota, disk, and reserved)

    Returns minimum of:
    - User remaining quota
    - Server available space (minus reserved)
    """
    # Get user quota
    async with get_session() as db:
        result = await db.exec(select(User).where(User.id == user_id))
        user = result.first()
        user_quota = user.quota if user and user.quota else 100 * 1024 * 1024 * 1024

    # Calculate user's current usage
    user_dir = Path(settings.download_dir) / str(user_id)
    used_space = 0
    if user_dir.exists():
        for file_path in user_dir.rglob("*"):
            if file_path.is_file():
                try:
                    used_space += file_path.stat().st_size
                except Exception:
                    pass

    user_remaining = max(0, user_quota - used_space)
    server_available = await get_server_available_space()

    return min(user_remaining, server_available)
