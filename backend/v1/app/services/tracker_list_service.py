"""热门 tracker 列表服务：解析/合并纯函数 + 内存缓存 + 持久化（经 repositories）。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from app.core.security import TRACKER_URI_SCHEMES
from app.repositories import tracker_list as tracker_list_repo
from app.repositories.settings import get_settings_row, now_ms

logger = logging.getLogger(__name__)

MAX_MERGED_TRACKER_COUNT = 5000
MAX_TRACKER_ENTRY_LENGTH = 2048
MAX_REMOTE_BODY_BYTES = 1024 * 1024
REMOTE_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=15)
REFRESHER_IDLE_SLEEP_SECONDS = 60

# 内存缓存：_merged 未加载时为 None（不注入）；_remote 为上次成功的远程分量
_merged: list[str] | None = None
_remote: list[str] = []
_merged_at: float = 0.0
_lock = asyncio.Lock()


def reset_tracker_cache() -> None:
    global _merged, _remote, _merged_at
    _merged = None
    _remote = []
    _merged_at = 0.0


def _split_entries(raw: str) -> list[str]:
    return [entry.strip() for entry in raw.replace(",", "\n").splitlines()]


def parse_tracker_lines(raw: str) -> tuple[list[str], int]:
    """按换行/逗号切分，剔除非法 scheme 与超长条目，返回 (合法列表, 非法计数)。"""
    valid: list[str] = []
    invalid_count = 0
    for entry in _split_entries(raw or ""):
        if not entry:
            continue
        scheme = entry.split("://", 1)[0].lower() if "://" in entry else ""
        if scheme not in TRACKER_URI_SCHEMES or len(entry) > MAX_TRACKER_ENTRY_LENGTH:
            invalid_count += 1
            continue
        valid.append(entry)
    return valid, invalid_count


def merge_trackers(fixed: list[str], remote_batches: list[list[str]]) -> list[str]:
    """固定在前、远程按源顺序追加，去重保序，截断至 MAX_MERGED_TRACKER_COUNT。"""
    merged: list[str] = []
    seen: set[str] = set()
    for entry in list(fixed) + [e for batch in remote_batches for e in batch]:
        if entry in seen:
            continue
        if len(merged) >= MAX_MERGED_TRACKER_COUNT:
            break
        seen.add(entry)
        merged.append(entry)
    return merged


def get_bt_tracker_option() -> str | None:
    """同步读取合并结果；未加载或为空返回 None（不注入）。"""
    if not _merged:
        return None
    return ",".join(_merged)


def _decode_json_list(raw: Any) -> list[str]:
    if not raw:
        return []
    try:
        decoded = json.loads(str(raw))
    except json.JSONDecodeError:
        logger.error("tracker 缓存 JSON 解析失败")
        return []
    return decoded if isinstance(decoded, list) else []


async def _persist(trackers: list[str], remote: list[str]) -> None:
    await tracker_list_repo.save_tracker_cache(
        {
            "trackers_json": json.dumps(trackers),
            "remote_trackers_json": json.dumps(remote),
            "entry_count": len(trackers),
            "updated_at_ms": now_ms(),
        }
    )


def _warn_if_truncated(fixed: list[str], remote: list[str], merged: list[str]) -> None:
    total = len(set(fixed) | set(remote))
    if total > len(merged):
        logger.warning(
            "tracker 合并截断 total=%s kept=%s", total, len(merged)
        )


async def apply_fixed_list(fixed_raw: str) -> None:
    """用新 fixed 与缓存的远程分量重合并，刷新内存与 DB（保存配置时同步调用）。"""
    global _merged, _merged_at
    fixed, _invalid = parse_tracker_lines(fixed_raw or "")
    async with _lock:
        remote = list(_remote)
        merged = merge_trackers(fixed, [remote])
        _warn_if_truncated(fixed, remote, merged)
    await _persist(merged, remote)
    async with _lock:
        _merged = merged
        _merged_at = time.time()


async def load_from_db() -> None:
    """重启加载：从 tracker_list_cache 恢复内存缓存；无行则空列表（不注入）。"""
    global _merged, _remote, _merged_at
    row = await tracker_list_repo.get_tracker_cache_row()
    async with _lock:
        if row is None:
            _merged, _remote, _merged_at = [], [], 0.0
            return
        _merged = _decode_json_list(row["trackers_json"])
        _remote = _decode_json_list(row["remote_trackers_json"])
        _merged_at = float(row.get("updated_at_ms") or 0) / 1000.0


async def get_tracker_status() -> dict[str, Any]:
    row = await tracker_list_repo.get_tracker_cache_row()
    if row is None:
        return {
            "entry_count": 0,
            "updated_at_ms": None,
            "last_refresh_at_ms": None,
            "last_refresh_status": "never",
            "last_refresh_failed_urls": [],
        }
    return {
        "entry_count": int(row["entry_count"]),
        "updated_at_ms": row["updated_at_ms"],
        "last_refresh_at_ms": row["last_refresh_at_ms"],
        "last_refresh_status": row["last_refresh_status"],
        "last_refresh_failed_urls": _decode_json_list(row["last_refresh_failed_urls"]),
    }


async def _fetch_url(url: str) -> str:
    """拉取单个远程源：15s 超时、1MiB 响应体上限，超限/非 2xx 视为该源失败。"""
    async with (
        aiohttp.ClientSession(timeout=REMOTE_FETCH_TIMEOUT) as session,
        session.get(url) as response,
    ):
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > MAX_REMOTE_BODY_BYTES:
                    raise ValueError("tracker 列表响应超过 1MiB 上限")
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", errors="replace")


async def _fetch_source(url: str) -> list[str]:
    try:
        valid, invalid_count = parse_tracker_lines(await _fetch_url(url))
        if invalid_count:
            logger.warning("远程 tracker 源剔除非法条目 url=%s invalid_count=%s", url, invalid_count)
        return valid
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("远程 tracker 源拉取失败 url=%s err=%s", url, exc)
        raise


async def refresh_remote_trackers() -> dict[str, Any]:
    """并发拉取全部远程源并重合并缓存；单源失败跳过，全失败保留上次结果。"""
    global _merged, _remote, _merged_at
    async with _lock:
        settings_row = await get_settings_row() or {}
        raw_urls = settings_row.get("tracker_remote_urls") or ""
        urls = [u.strip() for u in raw_urls.splitlines() if u.strip()]
        if not urls:
            logger.info("未配置远程 tracker URL，跳过刷新")
            return await get_tracker_status()

        results = await asyncio.gather(
            *(_fetch_source(url) for url in urls), return_exceptions=True
        )
        batches: list[list[str]] = []
        failed_urls: list[str] = []
        for url, result in zip(urls, results):
            if isinstance(result, BaseException):
                failed_urls.append(url)
            else:
                batches.append(result)

        refresh_ms = now_ms()
        if batches:
            status = "partial" if failed_urls else "ok"
            fixed, _invalid = parse_tracker_lines(
                settings_row.get("tracker_fixed_list") or ""
            )
            remote = [entry for batch in batches for entry in batch]
            merged = merge_trackers(fixed, [remote])
            _warn_if_truncated(fixed, remote, merged)
            await tracker_list_repo.save_tracker_cache(
                {
                    "trackers_json": json.dumps(merged),
                    "remote_trackers_json": json.dumps(remote),
                    "entry_count": len(merged),
                    "updated_at_ms": refresh_ms,
                    "last_refresh_at_ms": refresh_ms,
                    "last_refresh_status": status,
                    "last_refresh_failed_urls": json.dumps(failed_urls),
                }
            )
            _merged = merged
            _remote = remote
            _merged_at = time.time()
        else:
            status = "failed"
            previous = await tracker_list_repo.get_tracker_cache_row() or {}
            await tracker_list_repo.save_tracker_cache(
                {
                    "trackers_json": previous.get("trackers_json") or "[]",
                    "remote_trackers_json": previous.get("remote_trackers_json") or "[]",
                    "entry_count": int(previous.get("entry_count") or 0),
                    "updated_at_ms": previous.get("updated_at_ms") or refresh_ms,
                    "last_refresh_at_ms": refresh_ms,
                    "last_refresh_status": status,
                    "last_refresh_failed_urls": json.dumps(failed_urls),
                }
            )
        if status == "failed":
            logger.warning(
                "tracker 远程刷新完成 status=%s entry_count=%s failed=%s",
                status,
                len(_merged or []),
                len(failed_urls),
            )
        else:
            logger.info(
                "tracker 远程刷新完成 status=%s entry_count=%s failed=%s",
                status,
                len(_merged or []),
                len(failed_urls),
            )
        return await get_tracker_status()


async def _refresher_iteration() -> float:
    """单次循环体：返回下一次休眠秒数（测试直调，避免真实等待）。"""
    from app.services import settings_service

    interval = int(
        settings_service.get_config_value_sync("tracker_refresh_interval_minutes") or 0
    )
    if interval >= 5:
        try:
            await refresh_remote_trackers()
        except Exception:
            logger.exception("tracker 定时刷新失败")
        return interval * 60
    return REFRESHER_IDLE_SLEEP_SECONDS


async def run_tracker_list_refresher() -> None:
    while True:
        delay = await _refresher_iteration()
        await asyncio.sleep(delay)
