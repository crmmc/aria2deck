import asyncio

import aiohttp
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast


@dataclass
class MulticallOutcome:
    """单个 system.multicall 子调用的结果：success 值或 aria2 fault。"""

    ok: bool
    result: Any = None
    fault_code: Any = None
    fault_message: str = ""


class Aria2Client:
    # 类级别共享 Session，所有实例复用
    _session: aiohttp.ClientSession | None = None
    _session_lock: asyncio.Lock = asyncio.Lock()
    # 10s：aria2 的 add/tellStatus 正常在毫秒级（conf 中 file-allocation=none，
    # 添加任务不做预分配）。超时上限只用于兜住 aria2 卡顿，不该让用户请求长时间
    # 挂住——提交路径超时后由 reconciliation → 502「结果暂无法确认」→ 300s
    # submit_timeout stale cleanup 这条既有链路收尾。
    # WebSocket 监听有自己的超时设置（listener.py），不受此值影响。
    _timeout = aiohttp.ClientTimeout(total=10)

    def __init__(self, rpc_url: str, secret: str = "") -> None:
        self._rpc_url = rpc_url
        self._secret = secret

    @classmethod
    async def get_session(cls) -> aiohttp.ClientSession:
        """获取或创建共享的 aiohttp Session"""
        async with cls._session_lock:
            if cls._session is None or cls._session.closed:
                cls._session = aiohttp.ClientSession(timeout=cls._timeout)
            return cls._session

    @classmethod
    async def close_session(cls) -> None:
        """关闭共享 Session，应在应用关闭时调用"""
        if cls._session is not None and not cls._session.closed:
            await cls._session.close()
            cls._session = None

    def _build_params(self, params: list) -> list:
        if self._secret:
            return [f"token:{self._secret}", *params]
        return params

    async def _request(self, payload: dict) -> dict[str, Any]:
        """发送单个 JSON-RPC payload 并返回完整响应对象。"""
        session = await self.get_session()
        async with session.post(self._rpc_url, json=payload) as resp:
            # 检查 HTTP 状态码
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"aria2 RPC HTTP {resp.status}: {text[:200]}")
            # 安全解析 JSON
            try:
                data = await resp.json()
            except Exception as e:
                text = await resp.text()
                raise RuntimeError(f"aria2 RPC 返回非 JSON: {text[:200]}") from e
            if not isinstance(data, dict):
                raise RuntimeError(f"aria2 RPC 返回非法响应: {data!r}"[:200])
            return data

    async def _call(self, method: str, params: list | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": "aria2",
            "method": method,
            "params": self._build_params(params or []),
        }
        data = await self._request(payload)
        if "error" in data:
            raise RuntimeError(data["error"])
        return data["result"]

    async def multicall(
        self, calls: Sequence[Mapping[str, Any]]
    ) -> list[MulticallOutcome]:
        """一次 HTTP 请求按序执行 system.multicall。

        secret 只注入每个 nested params 首位，outer params 不带 token。
        结果逐项解析为 success 或 fault，shape/长度不符视为整批传输错误。
        """
        nested = [
            {
                "methodName": call["methodName"],
                "params": self._build_params(list(call.get("params") or [])),
            }
            for call in calls
        ]
        payload = {
            "jsonrpc": "2.0",
            "id": "aria2",
            "method": "system.multicall",
            "params": [nested],
        }
        data = await self._request(payload)
        if "error" in data:
            raise RuntimeError(data["error"])
        results = data.get("result")
        if not isinstance(results, list) or len(results) != len(calls):
            actual = len(results) if isinstance(results, list) else type(results).__name__
            raise RuntimeError(
                f"aria2 multicall 结果数量/形状不符: 期望 {len(calls)} 项, "
                f"实际 {actual}"
            )
        outcomes: list[MulticallOutcome] = []
        for index, item in enumerate(results):
            if isinstance(item, list) and len(item) == 1:
                outcomes.append(MulticallOutcome(ok=True, result=item[0]))
            elif (
                isinstance(item, dict)
                and "code" in item
                and "message" in item
            ):
                outcomes.append(
                    MulticallOutcome(
                        ok=False,
                        fault_code=item["code"],
                        fault_message=str(item["message"]),
                    )
                )
            else:
                # 只暴露项序号与类型，不携带原始结果值（可能含 URI/凭据）
                raise RuntimeError(
                    f"aria2 multicall 返回非法结果项: index={index}, "
                    f"type={type(item).__name__}"
                )
        return outcomes

    async def add_uri(
        self, uris: list[str], options: Mapping[str, Any] | None = None
    ) -> str:
        params: list[object] = [uris]
        if options:
            params.append(options)
        return cast(str, await self._call("aria2.addUri", params))

    async def add_torrent(
        self,
        torrent: str,
        uris: list[str] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> str:
        """添加种子任务

        Args:
            torrent: Base64 编码的种子文件内容
            uris: 可选的 Web Seeding URI 列表
            options: 可选的下载选项

        Returns:
            任务 GID
        """
        params: list[object] = [torrent]
        params.append(uris or [])
        if options:
            params.append(options)
        return cast(str, await self._call("aria2.addTorrent", params))

    async def tell_status(self, gid: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("aria2.tellStatus", [gid]))

    async def pause(self, gid: str) -> str:
        return cast(str, await self._call("aria2.pause", [gid]))

    async def unpause(self, gid: str) -> str:
        return cast(str, await self._call("aria2.unpause", [gid]))

    async def change_option(
        self, gid: str, options: Mapping[str, Any]
    ) -> str:
        return cast(str, await self._call("aria2.changeOption", [gid, options]))

    async def remove(self, gid: str) -> str:
        return cast(str, await self._call("aria2.remove", [gid]))

    async def remove_download_result(self, gid: str) -> str:
        return cast(str, await self._call("aria2.removeDownloadResult", [gid]))

    async def get_global_stat(self) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("aria2.getGlobalStat", []))

    async def get_files(self, gid: str) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], await self._call("aria2.getFiles", [gid]))

    async def get_uris(self, gid: str) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], await self._call("aria2.getUris", [gid]))

    async def get_peers(self, gid: str) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], await self._call("aria2.getPeers", [gid]))

    async def get_servers(self, gid: str) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], await self._call("aria2.getServers", [gid]))

    async def change_uri(
        self,
        gid: str,
        file_index: int,
        del_uris: list[str],
        add_uris: list[str],
    ) -> list[int]:
        """向任务的某个文件追加/移除下载 URI（用于 mirror 补发）。"""
        params: list[object] = [gid, file_index, del_uris, add_uris]
        return cast(list[int], await self._call("aria2.changeUri", params))

    async def tell_active(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], await self._call("aria2.tellActive", []))

    async def tell_waiting(
        self, offset: int = 0, num: int = 1000
    ) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]], await self._call("aria2.tellWaiting", [offset, num])
        )

    async def tell_stopped(
        self, offset: int = 0, num: int = 1000
    ) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]], await self._call("aria2.tellStopped", [offset, num])
        )

    async def force_remove(self, gid: str) -> str:
        return cast(str, await self._call("aria2.forceRemove", [gid]))

    async def get_version(self) -> dict[str, Any]:
        """获取 aria2 版本信息"""
        return cast(dict[str, Any], await self._call("aria2.getVersion", []))

    async def change_position(self, gid: str, pos: int, how: str) -> int:
        """调整任务在队列中的位置

        Args:
            gid: 任务 GID
            pos: 位置参数
            how: 定位方式 (POS_SET, POS_CUR, POS_END)

        Returns:
            新位置
        """
        return cast(int, await self._call("aria2.changePosition", [gid, pos, how]))
