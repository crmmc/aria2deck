"""Async folder packing service using 7-zip CLI"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import shlex
from pathlib import Path
from typing import Callable

from app.core.config import settings
from app.db import execute, fetch_one, fetch_all, utc_now


# 全局打包队列锁
_pack_queue_lock = asyncio.Lock()


# 允许的 7za 参数前缀白名单（防止命令注入）
_ALLOWED_7ZA_ARG_PREFIXES = (
    "-mmt",   # 多线程
    "-mx",    # 压缩级别
    "-m0=",   # 压缩方法
    "-ms",    # 固实压缩
    "-mf",    # 过滤器
    "-mhc",   # 头压缩
    "-mhe",   # 头加密
    "-p",     # 密码（允许用户加密）
)


class PackTaskManager:
    """Manages async pack task execution"""

    _running_tasks: dict[int, asyncio.subprocess.Process] = {}

    @classmethod
    def get_pack_format(cls) -> str:
        """Get pack format from config (zip or 7z)"""
        from app.routers.config import get_config_value
        val = get_config_value("pack_format")
        return val if val in ("zip", "7z") else "zip"

    @classmethod
    def get_compression_level(cls) -> int:
        """Get compression level (1-9)"""
        from app.routers.config import get_config_value
        val = get_config_value("pack_compression_level")
        try:
            level = int(val) if val else 5
            return max(1, min(9, level))
        except ValueError:
            return 5

    @classmethod
    def get_extra_args(cls) -> list[str]:
        """Get extra 7za arguments from config (with whitelist validation)"""
        from app.routers.config import get_config_value
        val = get_config_value("pack_extra_args")
        if not val or not val.strip():
            return []
        try:
            args = shlex.split(val)
            # 只允许白名单中的参数前缀（防止命令注入）
            safe_args = []
            for arg in args:
                if any(arg.startswith(prefix) for prefix in _ALLOWED_7ZA_ARG_PREFIXES):
                    safe_args.append(arg)
            return safe_args
        except ValueError:
            return []

    @classmethod
    def is_any_task_running(cls) -> bool:
        """Check if any pack task is currently running"""
        return len(cls._running_tasks) > 0

    @classmethod
    async def start_pack(
        cls,
        task_id: int,
        user_id: int,
        folder_path: str,
        output_name: str | None = None,
        on_progress: Callable[[int, int], None] | None = None
    ) -> None:
        """Start async packing process with global queue control

        Only one pack task runs at a time globally.
        """
        # 等待获取全局锁（确保同一时间只有一个任务在打包）
        async with _pack_queue_lock:
            await cls._do_pack(task_id, user_id, folder_path, output_name, on_progress)

    @classmethod
    async def _do_pack(
        cls,
        task_id: int,
        user_id: int,
        folder_path: str,
        output_name: str | None = None,
        on_progress: Callable[[int, int], None] | None = None
    ) -> None:
        """Actually perform the packing (called within lock)"""
        user_dir = Path(settings.download_dir) / str(user_id)

        # 判断是多文件还是单文件夹
        is_multi = folder_path.startswith("[")
        if is_multi:
            try:
                paths = json.loads(folder_path)
            except json.JSONDecodeError:
                cls._update_task_error(task_id, "Invalid paths format")
                return
            sources = [user_dir / p for p in paths]
            # 验证所有路径存在
            for source in sources:
                if not source.exists():
                    cls._update_task_error(task_id, f"Path does not exist: {source.name}")
                    return
        else:
            source = user_dir / folder_path
            if not source.exists():
                cls._update_task_error(task_id, "Source folder does not exist")
                return
            sources = [source]

        # Determine output format and path
        pack_format = cls.get_pack_format()
        compression = cls.get_compression_level()
        extra_args = cls.get_extra_args()

        # 确定输出文件名
        if output_name:
            base_name = output_name
        elif is_multi:
            base_name = "archive"
        else:
            base_name = sources[0].name

        output_filename = f"{base_name}.{pack_format}"
        output_path = user_dir / output_filename

        # Ensure unique filename
        counter = 1
        while output_path.exists():
            output_filename = f"{base_name}_{counter}.{pack_format}"
            output_path = user_dir / output_filename
            counter += 1

        # Update status to packing
        execute(
            "UPDATE pack_tasks SET status = ?, output_path = ?, updated_at = ? WHERE id = ?",
            ["packing", str(output_path), utc_now(), task_id]
        )

        # Build 7za command
        # -tzip or -t7z for format
        # -mx=N for compression level
        # -bsp1 for progress output
        format_flag = f"-t{pack_format}"

        # 基础命令
        cmd = ["7za", "a", format_flag, f"-mx={compression}", "-bsp1"]

        # 添加额外参数
        if extra_args:
            cmd.extend(extra_args)

        # 添加输出路径
        cmd.append(str(output_path))

        # 添加源文件/文件夹
        for source in sources:
            if source.is_dir():
                cmd.append(str(source) + "/*")
            else:
                cmd.append(str(source))

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            cls._running_tasks[task_id] = process

            # Parse progress from 7za output
            progress = 0
            async for line in process.stdout:
                line_text = line.decode("utf-8", errors="ignore").strip()
                # 7za progress format: " 45%" or similar
                match = re.search(r"(\d+)%", line_text)
                if match:
                    new_progress = int(match.group(1))
                    if new_progress != progress:
                        progress = new_progress
                        execute(
                            "UPDATE pack_tasks SET progress = ?, updated_at = ? WHERE id = ?",
                            [progress, utc_now(), task_id]
                        )
                        if on_progress:
                            on_progress(task_id, progress)

            await process.wait()

            if process.returncode == 0:
                # Success: get output size, delete sources, update status
                output_size = output_path.stat().st_size if output_path.exists() else 0

                # Delete source files/folders and their .aria2 control files
                for source in sources:
                    if source.is_dir():
                        shutil.rmtree(source)
                        # 删除目录对应的 .aria2 控制文件（如果存在）
                        aria2_file = source.parent / f"{source.name}.aria2"
                        if aria2_file.exists():
                            aria2_file.unlink()
                    elif source.is_file():
                        source.unlink()
                        # 删除文件对应的 .aria2 控制文件（如果存在）
                        aria2_file = source.parent / f"{source.name}.aria2"
                        if aria2_file.exists():
                            aria2_file.unlink()

                execute(
                    """UPDATE pack_tasks SET
                       status = ?, progress = 100, output_size = ?,
                       reserved_space = 0, updated_at = ?
                       WHERE id = ?""",
                    ["done", output_size, utc_now(), task_id]
                )
            else:
                # Failed: cleanup partial output
                if output_path.exists():
                    output_path.unlink()
                cls._update_task_error(task_id, f"7za exited with code {process.returncode}")

        except FileNotFoundError:
            cls._update_task_error(task_id, "7za command not found. Please install p7zip.")
        except asyncio.CancelledError:
            # Task was cancelled
            if output_path.exists():
                output_path.unlink()
            execute(
                "UPDATE pack_tasks SET status = ?, reserved_space = 0, updated_at = ? WHERE id = ?",
                ["cancelled", utc_now(), task_id]
            )
        except Exception as exc:
            if output_path.exists():
                output_path.unlink()
            cls._update_task_error(task_id, str(exc))
        finally:
            cls._running_tasks.pop(task_id, None)

    @classmethod
    async def cancel_pack(cls, task_id: int) -> bool:
        """Cancel a running pack task

        Returns True if cancelled, False if not running
        """
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
    def _update_task_error(cls, task_id: int, error: str) -> None:
        execute(
            """UPDATE pack_tasks SET
               status = ?, error_message = ?, reserved_space = 0, updated_at = ?
               WHERE id = ?""",
            ["failed", error, utc_now(), task_id]
        )


def calculate_folder_size(path: Path) -> int:
    """Calculate total size of folder in bytes"""
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except Exception:
        pass
    return total


def get_reserved_space() -> int:
    """Get total reserved space from pending/packing tasks"""
    result = fetch_one(
        """SELECT COALESCE(SUM(reserved_space), 0) as total
           FROM pack_tasks
           WHERE status IN ('pending', 'packing')"""
    )
    return result["total"] if result else 0


def get_server_available_space() -> int:
    """Get server available space minus reserved space"""
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    reserved = get_reserved_space()
    return max(0, disk.free - reserved)


def get_user_available_space_for_pack(user_id: int) -> int:
    """Get user available space for pack (considers quota, disk, and reserved)

    Returns minimum of:
    - User remaining quota
    - Server available space (minus reserved)
    """
    # Get user quota
    user = fetch_one("SELECT quota FROM users WHERE id = ?", [user_id])
    user_quota = user["quota"] if user and user.get("quota") else 100 * 1024 * 1024 * 1024

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
    server_available = get_server_available_space()

    return min(user_remaining, server_available)
