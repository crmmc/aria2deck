from __future__ import annotations

from typing import cast

from ...domain.downloads import LIVE_SUBSCRIPTION_STATUSES
from ...domain.errors import DomainError
from .commands import DownloadCommandService
from .history import HistoryService
from .models import TaskProjection
from .queries import DownloadQueryService
from .rpc_projection import (
    VirtualGidSpace,
    global_stat_payload,
    option_payload,
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

_ACTIVE_BUCKET = frozenset({"active"})
_WAITING_BUCKET = frozenset({"queued", "waiting", "paused"})
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


def _keys_of(params: list[object], position: int) -> list[str] | None:
    if len(params) > position and isinstance(params[position], list):
        keys = [str(key) for key in cast(list[object], params[position])]
        return keys or None
    return None


def _int_param(params: list[object], position: int, default: int) -> int:
    if len(params) > position and isinstance(params[position], int) and not isinstance(
        params[position], bool
    ):
        return cast(int, params[position])
    return default


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
        selected: list[int] | None = None
        for key, value in options.items():
            if key == "select-file":
                if isinstance(value, list):
                    selected = [int(index) for index in value if isinstance(index, int)]
                elif isinstance(value, str):
                    selected = [int(part) for part in value.split(",") if part.strip()]
                else:
                    raise _bad("select-file 参数无效")
            else:
                # webseed / tracker 等参数一律拒绝，不旁路提交。
                raise _bad(f"不支持的种子选项：{key}")
        result = await self.commands.create_torrent(
            user_id=user_id,
            payload_base64=params[0],
            selected_file_indexes=selected,
        )
        if not result.accepted or result.task_id is None:
            raise _bad(result.message or "创建种子下载失败")
        return self.gids.virtual_gid(result.task_id)

    # --- 控制（RPC-12/25/26/27/22） ---

    async def _aria2_remove(self, params: list[object], user_id: int) -> str:
        gid = self._require_gid(params)
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
        return await self._aria2_remove(params, user_id)

    async def _aria2_changePosition(self, params: list[object], user_id: int) -> int:
        gid = self._require_gid(params)
        await self._resolve(user_id, gid)  # 仅校验归属与存在，不改队列所有权。
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
        projection = await self._resolve(user_id, gid)
        return tell_status_payload(projection, gid, keys=_keys_of(params, 1))

    async def _aria2_tellActive(self, params: list[object], user_id: int) -> list[dict[str, object]]:
        keys = _keys_of(params, 0)
        return await self._tell_bucket(user_id, _ACTIVE_BUCKET, 0, -1, keys)

    async def _aria2_tellWaiting(self, params: list[object], user_id: int) -> list[dict[str, object]]:
        offset = _int_param(params, 0, 0)
        num = _int_param(params, 1, 0)
        return await self._tell_bucket(user_id, _WAITING_BUCKET, offset, num, _keys_of(params, 2))

    async def _aria2_tellStopped(self, params: list[object], user_id: int) -> list[dict[str, object]]:
        offset = _int_param(params, 0, 0)
        num = _int_param(params, 1, 0)
        return await self._tell_bucket(user_id, _STOPPED_BUCKET, offset, num, _keys_of(params, 2))

    async def _aria2_getFiles(self, params: list[object], user_id: int) -> list[dict[str, str]]:
        gid = self._require_gid(params)
        projection = await self._resolve(user_id, gid)
        payload = tell_status_payload(projection, gid, keys=["files"])
        files = payload["files"]
        return cast(list[dict[str, str]], files)

    async def _aria2_getUris(self, params: list[object], user_id: int) -> list[dict[str, str]]:
        gid = self._require_gid(params)
        projection = await self._resolve(user_id, gid)
        return uris_payload(projection)

    async def _aria2_getGlobalStat(self, params: list[object], user_id: int) -> dict[str, str]:
        stats = await self.queries.stats(user_id=user_id)
        return global_stat_payload(
            stats.counts, stats.total, stats.download_speed, stats.upload_speed
        )

    async def _aria2_getVersion(self, params: list[object], user_id: int) -> dict[str, object]:
        return version_payload()

    async def _aria2_getPeers(self, params: list[object], user_id: int) -> list[object]:
        gid = self._require_gid(params)
        await self._resolve(user_id, gid)
        return []

    async def _aria2_getServers(self, params: list[object], user_id: int) -> list[object]:
        gid = self._require_gid(params)
        await self._resolve(user_id, gid)
        return []

    async def _aria2_getOption(self, params: list[object], user_id: int) -> dict[str, str]:
        gid = self._require_gid(params)
        records = await self.queries.list_user_records(user_id=user_id)
        for record in records:
            if self.gids.virtual_gid(record.id) == gid or record.gid == gid:
                return option_payload(record)
        raise _bad("未知 GID")

    async def _aria2_getGlobalOption(self, params: list[object], user_id: int) -> dict[str, str]:
        return option_payload()

    async def _aria2_getSessionInfo(self, params: list[object], user_id: int) -> dict[str, str]:
        return {"sessionId": _SESSION_ID}

    async def _aria2_saveSession(self, params: list[object], user_id: int) -> str:
        return "OK"

    # --- 清理（RPC-24） ---

    async def _aria2_removeDownloadResult(self, params: list[object], user_id: int) -> str:
        gid = self._require_gid(params)
        projection = await self._resolve(user_id, gid)
        if projection.status in LIVE_SUBSCRIPTION_STATUSES:
            raise _bad("进行中的任务不能删除结果")
        results = await self.history.delete(user_id=user_id, task_ids=[projection.id])
        if not results or not results[0].accepted:
            raise _bad(results[0].message or "删除失败" if results else "删除失败")
        return "OK"

    async def _aria2_purgeDownloadResult(self, params: list[object], user_id: int) -> str:
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
