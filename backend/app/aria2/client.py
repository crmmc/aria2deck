import aiohttp


class Aria2Client:
    def __init__(self, rpc_url: str, secret: str = "") -> None:
        self._rpc_url = rpc_url
        self._secret = secret

    def _build_params(self, params: list) -> list:
        if self._secret:
            return [f"token:{self._secret}", *params]
        return params

    async def _call(self, method: str, params: list | None = None) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": "aria2",
            "method": method,
            "params": self._build_params(params or []),
        }
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self._rpc_url, json=payload) as resp:
                data = await resp.json()
                if "error" in data:
                    raise RuntimeError(data["error"])
                return data["result"]

    async def add_uri(self, uris: list[str], options: dict | None = None) -> str:
        params = [uris]
        if options:
            params.append(options)
        return await self._call("aria2.addUri", params)

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
        params: list = [torrent]
        params.append(uris or [])
        if options:
            params.append(options)
        return await self._call("aria2.addTorrent", params)

    async def tell_status(self, gid: str) -> dict:
        return await self._call("aria2.tellStatus", [gid])

    async def pause(self, gid: str) -> str:
        return await self._call("aria2.pause", [gid])

    async def unpause(self, gid: str) -> str:
        return await self._call("aria2.unpause", [gid])

    async def remove(self, gid: str) -> str:
        return await self._call("aria2.remove", [gid])

    async def remove_download_result(self, gid: str) -> str:
        return await self._call("aria2.removeDownloadResult", [gid])

    async def get_global_stat(self) -> dict:
        return await self._call("aria2.getGlobalStat", [])

    async def get_files(self, gid: str) -> list[dict]:
        return await self._call("aria2.getFiles", [gid])

    async def tell_active(self) -> list[dict]:
        return await self._call("aria2.tellActive", [])

    async def tell_waiting(self, offset: int = 0, num: int = 1000) -> list[dict]:
        return await self._call("aria2.tellWaiting", [offset, num])

    async def tell_stopped(self, offset: int = 0, num: int = 1000) -> list[dict]:
        return await self._call("aria2.tellStopped", [offset, num])

    async def force_remove(self, gid: str) -> str:
        return await self._call("aria2.forceRemove", [gid])

    async def get_version(self) -> dict:
        """获取 aria2 版本信息"""
        return await self._call("aria2.getVersion", [])

    async def change_position(self, gid: str, pos: int, how: str) -> int:
        """调整任务在队列中的位置

        Args:
            gid: 任务 GID
            pos: 位置参数
            how: 定位方式 (POS_SET, POS_CUR, POS_END)

        Returns:
            新位置
        """
        return await self._call("aria2.changePosition", [gid, pos, how])
