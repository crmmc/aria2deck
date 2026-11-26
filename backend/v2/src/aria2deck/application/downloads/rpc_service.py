from __future__ import annotations

import logging
from typing import cast

from ...domain.downloads import LIVE_SUBSCRIPTION_STATUSES, parse_allow_rename_policy
from ...domain.errors import DomainError
from .commands import DownloadCommandService
from .history import HistoryService
from .models import TaskProjection
from .queries import DownloadQueryService
from .rpc_projection import (
    VirtualGidSpace,
    global_stat_payload,
    option_payload,
    servers_payload,
    slice_window,
    tell_status_payload,
    uris_payload,
    version_payload,
)

ARIA2_METHODS = (
    "aria2.addTorrent",
    "aria2.addUri",
    "aria2.changePosition",
    "aria2.changeUri",
    "aria2.forcePause",
    "aria2.forcePauseAll",
    "aria2.forceRemove",
    "aria2.getFiles",
    "aria2.getGlobalOption",
    "aria2.getGlobalStat",
    "aria2.getOption",
    "aria2.getPeers",
    "aria2.getServers",
    "aria2.getSessionInfo",
    "aria2.getUris",
    "aria2.getVersion",
    "aria2.pause",
    "aria2.pauseAll",
    "aria2.purgeDownloadResult",
    "aria2.remove",
    "aria2.removeDownloadResult",
    "aria2.saveSession",
    "aria2.tellActive",
    "aria2.tellStatus",
    "aria2.tellStopped",
    "aria2.tellWaiting",
    "aria2.unpause",
    "aria2.unpauseAll",
)
SYSTEM_METHODS = ("system.listMethods", "system.multicall")
RPC_METHODS = ARIA2_METHODS + SYSTEM_METHODS

_SESSION_ID = "aria2deck-v2"

logger = logging.getLogger(__name__)

# aria2 客户端（AriaNg/Motrix 等）总附带大量标准写入选项：门面只保留后端真正消费的键，
# 其余忽略，避免客户端默认选项变成创建失败。领域层白名单与 REST 契约保持严格（A9）。
# 注：规范的 header 选项在过滤前先转成内部 headers dict，不在本白名单内。
_SUPPORTED_WRITE_OPTIONS = frozenset(
    {
        "allow_rename",  # 由 create_uris 消费的命名策略，不进领域选项白名单
        "headers",
        "mirrors",
        "out",
        "http-user",
        "http-passwd",
        "max-connection-per-server",
    }
)
_TORRENT_WRITE_OPTIONS = frozenset({"allow_rename", "select-file"})
_CHANGE_POSITION_HOW = frozenset({"POS_SET", "POS_CUR", "POS_END"})

_ACTIVE_BUCKET = frozenset({"active"})
_WAITING_BUCKET = frozenset({"waiting", "paused"})
_STOPPED_BUCKET = frozenset({"complete", "error", "removed"})


class RpcMethodError(Exception):
    """aria2 兼容方法级错误（code 1/2），中文消息，不携带异常原文。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _unsupported() -> RpcMethodError:
    return RpcMethodError(1, "该方法在 Aria2Deck 中不受支持")


def _bad(message: str) -> RpcMethodError:
    return RpcMethodError(1, message)


def _filter_write_options(
    options: dict[str, object], allowed: frozenset[str] = _SUPPORTED_WRITE_OPTIONS
) -> dict[str, object]:
    """只保留 `allowed` 内的写入选项，其余 aria2 选项丢弃并记录键名。

    已知限制：pause 不做映射——创建后立即暂停与提交管线存在竞态，因此 pause 与
    其他未知键一样只记键名后丢弃，任务按正常准入流程提交。
    """
    dropped = sorted(key for key in options if key not in allowed)
    if dropped:
        logger.debug("RPC 写入选项忽略不支持字段: %s", dropped)
    return {key: value for key, value in options.items() if key in allowed}


def _merge_header_option(options: dict[str, object]) -> dict[str, object]:
    """aria2 的 header 选项（"Name: Value" 字符串或数组）→ 内部 headers dict。

    同名头后者覆盖前者；非法项（非字符串、缺冒号、空头名）忽略，只记个数不记值。
    """
    if "header" not in options:
        return options
    raw = options.pop("header")
    existing = options.get("headers")
    headers = dict(existing) if isinstance(existing, dict) else {}
    rejected = 0
    for item in raw if isinstance(raw, list) else [raw]:
        if not isinstance(item, str) or ":" not in item:
            rejected += 1
            continue
        name, _, value = item.partition(":")
        if not name.strip():
            rejected += 1
            continue
        headers[name.strip()] = value.strip()
    if rejected:
        logger.debug("RPC header 选项忽略非法项 count=%s", rejected)
    if headers:
        options["headers"] = headers
    return options


def _keys_of(params: list[object], position: int) -> list[str] | None:
    if len(params) > position and isinstance(params[position], list):
        keys = [str(key) for key in cast(list[object], params[position])]
        return keys or None
    return None


def _ignore_extra_params(method: str, params: list[object], consumed: int) -> None:
    """多传的参数（position 等）一律忽略，只记方法名与个数，不记值。"""
    if len(params) > consumed:
        logger.debug("RPC %s 忽略多余参数 count=%s", method, len(params) - consumed)


def _coerce_int(value: object, name: str) -> int:
    """aria2 客户端常把整数参数序列化成字符串：宽容转换，只有非数值形态才报错。"""
    if isinstance(value, bool):
        raise _bad(f"{name} 参数无效")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            raise _bad(f"{name} 参数无效") from None
    raise _bad(f"{name} 参数无效")


def _int_param(params: list[object], position: int, name: str, default: int) -> int:
    if len(params) <= position:
        return default
    return _coerce_int(params[position], name)


class Aria2RpcService:
    """30-method aria2 JSON-RPC facade over existing download use cases."""

    def __init__(
        self,
        *,
        queries: DownloadQueryService,
        commands: DownloadCommandService,
        history: HistoryService,
        gids: VirtualGidSpace,
    ) -> None:
        self.queries = queries
        self.commands = commands
        self.history = history
        self.gids = gids

    async def dispatch(self, method: str, params: list[object], *, user_id: int) -> object:
        if method not in RPC_METHODS:
            raise RpcMethodError(-32601, f"方法不存在：{method}")
        handler = getattr(self, "_" + method.replace(".", "_"))
        return await handler(params, user_id)

    # --- 创建（RPC-10/11） ---

    async def _aria2_addUri(self, params: list[object], user_id: int) -> str:
        if not params or not isinstance(params[0], list) or not params[0]:
            raise _bad("缺少下载 URI 列表")
        raw_uris = params[0]
        if not isinstance(raw_uris, list):
            raise _bad("下载 URI 列表无效")
        uris = [str(uri) for uri in raw_uris]
        options: dict[str, object] = {}
        if len(params) > 1 and isinstance(params[1], dict):
            options = dict(params[1])
        if len(uris) > 1 and "mirrors" not in options:
            options["mirrors"] = uris[1:]
        options = _filter_write_options(_merge_header_option(options))
        _ignore_extra_params("aria2.addUri", params, 2)  # uris、options 之后的 position
        from .models import UriDownloadInput

        results = await self.commands.create_uris(
            user_id=user_id, items=[UriDownloadInput(uri=uris[0], options=options)]
        )
        result = results[0]
        if not result.accepted or result.task_id is None:
            raise _bad(result.message or "创建下载失败")
        return self.gids.virtual_gid(result.task_id)

    async def _aria2_addTorrent(self, params: list[object], user_id: int) -> str:
        if not params or not isinstance(params[0], str) or not params[0]:
            raise _bad("缺少 base64 种子数据")
        # aria2 规范形态 [b64, uris[], options{}]；兼容 [b64, options{}] 旧客户端形态。
        options: dict[str, object] = {}
        if len(params) > 2 and isinstance(params[2], dict):
            options = dict(params[2])
        elif len(params) > 1 and isinstance(params[1], dict):
            options = dict(params[1])
        if len(params) > 1 and isinstance(params[1], list) and params[1]:
            # 非空 URI 列表即 webseed：按 RPC-11 拒绝，不旁路提交。
            raise _bad("不支持通过种子 URI 列表提供 webseed")
        options = _filter_write_options(_merge_header_option(options), _TORRENT_WRITE_OPTIONS)
        _ignore_extra_params(
            "aria2.addTorrent", params, 3 if len(params) > 1 and isinstance(params[1], list) else 2
        )
        selected: list[int] | None = None
        allow_rename = True
        for key, value in options.items():
            if key == "select-file":
                if isinstance(value, list):
                    selected = [int(index) for index in value if isinstance(index, int)]
                elif isinstance(value, str):
                    selected = [int(part) for part in value.split(",") if part.strip()]
                else:
                    raise _bad("select-file 参数无效")
            elif key == "allow_rename":
                try:
                    allow_rename = parse_allow_rename_policy(value)
                except DomainError as error:
                    raise _bad(str(error)) from None
        result = await self.commands.create_torrent(
            user_id=user_id,
            payload_base64=params[0],
            selected_file_indexes=selected,
            allow_rename=allow_rename,
        )
        if not result.accepted or result.task_id is None:
            raise _bad(result.message or "创建种子下载失败")
        return self.gids.virtual_gid(result.task_id)

    # --- 控制（RPC-12/25/26/27/22） ---

    async def _aria2_remove(
        self, params: list[object], user_id: int, *, name: str = "aria2.remove"
    ) -> str:
        gid = self._require_gid(params)
        _ignore_extra_params(name, params, 1)
        projection = await self._resolve(user_id, gid)
        if projection.status not in LIVE_SUBSCRIPTION_STATUSES:
            raise _bad("任务已结束，不能取消")
        try:
            results = await self.commands.cancel(user_id=user_id, task_ids=[projection.id])
        except DomainError as error:
            raise _bad(str(error)) from None
        if not results or not results[0].accepted:
            raise _bad(results[0].message or "取消失败" if results else "取消失败")
        if results[0].stop_confirmed is not True:
            raise _bad("下载停止结果无法确认")
        return gid

    async def _aria2_forceRemove(self, params: list[object], user_id: int) -> str:
        return await self._aria2_remove(params, user_id, name="aria2.forceRemove")

    async def _aria2_changePosition(self, params: list[object], user_id: int) -> int:
        gid = self._require_gid(params)
        if len(params) > 1:
            _coerce_int(params[1], "pos")  # 规范内可传字符串数字；桩实现不改队列位置。
        if len(params) > 2 and params[2] not in _CHANGE_POSITION_HOW:
            raise _bad("how 参数无效：仅支持 POS_SET / POS_CUR / POS_END")
        _ignore_extra_params("aria2.changePosition", params, 3)
        await self._resolve(user_id, gid)  # 仅校验归属与存在，不改队列所有权。
        logger.debug("RPC aria2.changePosition 只校验归属，不改队列位置")
        return 0  # noqa: W0105  # 兼容位置值

    async def _aria2_pause(self, params: list[object], user_id: int) -> str:
        raise _unsupported()

    async def _aria2_forcePause(self, params: list[object], user_id: int) -> str:
        raise _unsupported()

    async def _aria2_unpause(self, params: list[object], user_id: int) -> str:
        raise _unsupported()

    async def _aria2_pauseAll(self, params: list[object], user_id: int) -> str:
        raise _unsupported()

    async def _aria2_forcePauseAll(self, params: list[object], user_id: int) -> str:
        raise _unsupported()

    async def _aria2_unpauseAll(self, params: list[object], user_id: int) -> str:
        raise _unsupported()

    async def _aria2_changeUri(self, params: list[object], user_id: int) -> str:
        raise RpcMethodError(2, "不支持修改下载来源")

    # --- 查询（RPC-13~21/23） ---

    async def _aria2_tellStatus(self, params: list[object], user_id: int) -> dict[str, object]:
        gid = self._require_gid(params)
        _ignore_extra_params("aria2.tellStatus", params, 2)  # gid、keys
        projection = await self._resolve(user_id, gid)
        return tell_status_payload(projection, gid, keys=_keys_of(params, 1))

    async def _aria2_tellActive(self, params: list[object], user_id: int) -> list[dict[str, object]]:
        keys = _keys_of(params, 0)
        _ignore_extra_params("aria2.tellActive", params, 1)
        return await self._tell_bucket(user_id, _ACTIVE_BUCKET, 0, -1, keys)

    async def _aria2_tellWaiting(self, params: list[object], user_id: int) -> list[dict[str, object]]:
        offset = _int_param(params, 0, "offset", 0)
        num = _int_param(params, 1, "num", 0)
        _ignore_extra_params("aria2.tellWaiting", params, 3)
        return await self._tell_bucket(user_id, _WAITING_BUCKET, offset, num, _keys_of(params, 2))

    async def _aria2_tellStopped(self, params: list[object], user_id: int) -> list[dict[str, object]]:
        offset = _int_param(params, 0, "offset", 0)
        num = _int_param(params, 1, "num", 0)
        _ignore_extra_params("aria2.tellStopped", params, 3)
        return await self._tell_bucket(user_id, _STOPPED_BUCKET, offset, num, _keys_of(params, 2))

    async def _aria2_getFiles(self, params: list[object], user_id: int) -> list[dict[str, str]]:
        gid = self._require_gid(params)
        _ignore_extra_params("aria2.getFiles", params, 1)
        projection = await self._resolve(user_id, gid)
        payload = tell_status_payload(projection, gid, keys=["files"])
        files = payload["files"]
        return cast(list[dict[str, str]], files)

    async def _aria2_getUris(self, params: list[object], user_id: int) -> list[dict[str, str]]:
        gid = self._require_gid(params)
        _ignore_extra_params("aria2.getUris", params, 1)
        projection = await self._resolve(user_id, gid)
        return uris_payload(projection)

    async def _aria2_getGlobalStat(self, params: list[object], user_id: int) -> dict[str, str]:
        _ignore_extra_params("aria2.getGlobalStat", params, 0)
        stats = await self.queries.stats(user_id=user_id)
        return global_stat_payload(
            stats.counts, stats.total, stats.download_speed, stats.upload_speed
        )

    async def _aria2_getVersion(self, params: list[object], user_id: int) -> dict[str, object]:
        _ignore_extra_params("aria2.getVersion", params, 0)
        return version_payload()

    async def _aria2_getPeers(self, params: list[object], user_id: int) -> list[object]:
        gid = self._require_gid(params)
        _ignore_extra_params("aria2.getPeers", params, 1)
        await self._resolve(user_id, gid)
        return []

    async def _aria2_getServers(
        self, params: list[object], user_id: int
    ) -> list[dict[str, object]]:
        gid = self._require_gid(params)
        _ignore_extra_params("aria2.getServers", params, 1)
        projection = await self._resolve(user_id, gid)
        return servers_payload(projection)

    async def _aria2_getOption(self, params: list[object], user_id: int) -> dict[str, str]:
        gid = self._require_gid(params)
        _ignore_extra_params("aria2.getOption", params, 1)
        records = await self.queries.list_user_records(user_id=user_id)
        for record in records:
            if self.gids.virtual_gid(record.id) == gid or record.gid == gid:
                return option_payload(record)
        raise _bad("未知 GID")

    async def _aria2_getGlobalOption(self, params: list[object], user_id: int) -> dict[str, str]:
        _ignore_extra_params("aria2.getGlobalOption", params, 0)
        return option_payload()

    async def _aria2_getSessionInfo(self, params: list[object], user_id: int) -> dict[str, str]:
        _ignore_extra_params("aria2.getSessionInfo", params, 0)
        return {"sessionId": _SESSION_ID}

    async def _aria2_saveSession(self, params: list[object], user_id: int) -> str:
        _ignore_extra_params("aria2.saveSession", params, 0)
        return "OK"

    # --- 清理（RPC-24） ---

    async def _aria2_removeDownloadResult(self, params: list[object], user_id: int) -> str:
        gid = self._require_gid(params)
        _ignore_extra_params("aria2.removeDownloadResult", params, 1)
        projection = await self._resolve(user_id, gid)
        if projection.status in LIVE_SUBSCRIPTION_STATUSES:
            raise _bad("进行中的任务不能删除结果")
        results = await self.history.delete(user_id=user_id, task_ids=[projection.id])
        if not results or not results[0].accepted:
            raise _bad(results[0].message or "删除失败" if results else "删除失败")
        return "OK"

    async def _aria2_purgeDownloadResult(self, params: list[object], user_id: int) -> str:
        _ignore_extra_params("aria2.purgeDownloadResult", params, 0)
        records = await self.queries.list_user_records(user_id=user_id)
        terminal = [
            record.id
            for record in records
            if record.subscription_status not in LIVE_SUBSCRIPTION_STATUSES
        ]
        if terminal:
            await self.history.delete(user_id=user_id, task_ids=terminal)
        return "OK"

    # --- 系统（RPC-28/29） ---

    async def _system_listMethods(self, params: list[object], user_id: int) -> list[str]:
        return sorted(RPC_METHODS)

    async def _system_multicall(self, params: list[object], user_id: int) -> object:
        # 传输层在分发前展开 multicall；直接到达这里即为嵌套调用。
        raise RpcMethodError(-32602, "不能嵌套调用 system.multicall")

    # --- 内部 ---

    @staticmethod
    def _require_gid(params: list[object]) -> str:
        if not params or not isinstance(params[0], str) or not params[0].strip():
            raise _bad("GID 无效")
        return str(params[0])

    async def _resolve(self, user_id: int, gid: str) -> TaskProjection:
        """虚拟 GID 精确解析；raw aria2 GID 兼容反查。响应侧只回显请求 GID。"""
        records = await self.queries.list_user_records(user_id=user_id)
        for record in records:
            if self.gids.virtual_gid(record.id) == gid or record.gid == gid:
                return await self.queries.get_task(user_id=user_id, task_id=record.id)
        raise _bad("未知 GID")

    async def _tell_bucket(
        self,
        user_id: int,
        bucket: frozenset[str],
        offset: int,
        num: int,
        keys: list[str] | None,
    ) -> list[dict[str, object]]:
        from .rpc_projection import aria2_status

        records = await self.queries.list_user_records(user_id=user_id)
        projections = [
            await self.queries.get_task(user_id=user_id, task_id=record.id)
            for record in records
        ]
        matched = [
            projection
            for projection in projections
            if aria2_status(projection.status) in bucket
        ]
        matched.sort(key=lambda projection: projection.id)
        if num < 0:
            window = matched
        else:
            window = slice_window(matched, offset, num)
        return [
            tell_status_payload(
                projection, self.gids.virtual_gid(projection.id), keys=keys
            )
            for projection in window
        ]
