import asyncio

import aiohttp
from typing import Any, cast


class Aria2Client:
    # 类级别共享 Session，所有实例复用
    _session: aiohttp.ClientSession | None = None
    _session_lock: asyncio.Lock = asyncio.Lock()
    _timeout = aiohttp.ClientTimeout(total=30)

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

    async def _call(self, method: str, params: list | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": "aria2",
            "method": method,
            "params": self._build_params(params or []),
        }
        session = await self.get_session()
        async with session.post(self._rpc_url, json=payload) as resp:
            data = await resp.json()
            if "error" in data:
                raise RuntimeError(data["error"])
            return data["result"]

    async def add_uri(self, uris: list[str], options: dict | None = None) -> str:
        params: list[object] = [uris]
        if options:
            params.append(options)
        return cast(str, await self._call("aria2.addUri", params))

    async def add_torrent(
        self,
        torrent: str,
        uris: list[str] | None = None,
        options: dict | None = None,
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

    async def tell_status(self, gid: str) -> dict:
        return cast(dict, await self._call("aria2.tellStatus", [gid]))

    async def pause(self, gid: str) -> str:
        return cast(str, await self._call("aria2.pause", [gid]))

    async def unpause(self, gid: str) -> str:
        return cast(str, await self._call("aria2.unpause", [gid]))

    async def remove(self, gid: str) -> str:
        return cast(str, await self._call("aria2.remove", [gid]))

    async def remove_download_result(self, gid: str) -> str:
        return cast(str, await self._call("aria2.removeDownloadResult", [gid]))

    async def get_global_stat(self) -> dict:
        return cast(dict, await self._call("aria2.getGlobalStat", []))

    async def get_files(self, gid: str) -> list[dict]:
        return cast(list[dict], await self._call("aria2.getFiles", [gid]))

    async def get_uris(self, gid: str) -> list[dict]:
        return cast(list[dict], await self._call("aria2.getUris", [gid]))

    async def get_peers(self, gid: str) -> list[dict]:
        return cast(list[dict], await self._call("aria2.getPeers", [gid]))

    async def get_servers(self, gid: str) -> list[dict]:
        return cast(list[dict], await self._call("aria2.getServers", [gid]))

    async def tell_active(self) -> list[dict]:
        return cast(list[dict], await self._call("aria2.tellActive", []))

    async def tell_waiting(self, offset: int = 0, num: int = 1000) -> list[dict]:
        return cast(list[dict], await self._call("aria2.tellWaiting", [offset, num]))

    async def tell_stopped(self, offset: int = 0, num: int = 1000) -> list[dict]:
        return cast(list[dict], await self._call("aria2.tellStopped", [offset, num]))

    async def force_remove(self, gid: str) -> str:
        return cast(str, await self._call("aria2.forceRemove", [gid]))

    async def get_version(self) -> dict:
        """获取 aria2 版本信息"""
        return cast(dict, await self._call("aria2.getVersion", []))

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
