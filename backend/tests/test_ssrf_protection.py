"""SSRF 职责边界测试（M24 数组契约后）

M24 后 REST 创建任务不再在请求阶段做外部 probe / SSRF 校验：
aria2 只获得 internal gateway URI（capability 签名），SSRF/DNS/redirect/
max-size 的拒绝由 internal fetch gateway 承接，证据见：
- tests/test_security_utils.py：本机/内网/共享地址段、DNS 解析拒绝
- tests/test_internal_fetch.py：redirect 到内网、oversize、capability 校验
- tests/test_internal_fetch_aria2.py：真实 aria2 仅经 gateway 下载

本文件保留 REST endpoint 层有意义的安全边界：
1. 旧 object body（{"uri": ...}）拒绝，不恢复兼容
2. 原始外部 URI（含内网/本机目标）不直达 aria2 提交调用
3. 协议白名单逐项拒绝且不触发 aria2 RPC
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.aria2.client import MulticallOutcome


def _find_planned_gid(params: list[Any]) -> str | None:
    for param in params:
        if isinstance(param, dict) and isinstance(param.get("gid"), str):
            return param["gid"]
        if isinstance(param, list):
            found = _find_planned_gid(param)
            if found is not None:
                return found
    return None


class CapturingAria2Client:
    """记录 multicall 调用并按 planned gid 返回 ok outcome。"""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def multicall(self, calls: list[dict]) -> list[MulticallOutcome]:
        self.calls.append(calls)
        return [
            MulticallOutcome(ok=True, result=_find_planned_gid(call.get("params") or []))
            for call in calls
        ]

    async def add_uri(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("array 契约不应调用 legacy add_uri")


def _submitted_blob(calls: list[list[dict]]) -> str:
    import json

    return json.dumps(calls, ensure_ascii=False, default=str)


class TestLegacyObjectBodyRejected:
    def test_old_object_body_returns_422(self, authenticated_client: TestClient):
        """旧 object body 不再兼容，恢复 array-only 契约"""
        response = authenticated_client.post(
            "/api/tasks", json={"uri": "http://example.com/file.zip"}
        )
        assert response.status_code == 422


class TestExternalURINotDeliveredToAria2:
    """内网/本机外部 URI 不在请求阶段 400（无外部 probe），
    但原始 URI 绝不出现在 aria2 提交调用中，下载只会通过
    internal gateway（gateway 侧 SSRF 拒绝见 test_internal_fetch.py）。"""

    @pytest.mark.parametrize(
        "uri",
        [
            "http://127.0.0.1:8080/file.zip",
            "http://localhost:8080/file.zip",
            "http://[::1]:8080/file.zip",
            "http://0.0.0.0:8080/file.zip",
            "http://192.168.1.1/file.zip",
            "http://10.0.0.1/file.zip",
            "http://100.64.0.1/file.zip",
            "http://172.16.0.1/file.zip",
            "http://169.254.169.254/latest/meta-data/",
            "https://127.0.0.1:8443/file.zip",
        ],
    )
    @patch("app.services.task_service._get_client")
    def test_private_uri_accepted_but_only_gateway_uri_submitted(
        self,
        mock_get_client: Any,
        authenticated_client: TestClient,
        uri: str,
    ):
        fake = CapturingAria2Client()
        mock_get_client.return_value = fake
        response = authenticated_client.post(
            "/api/tasks", json={"tasks": [{"uri": uri}]}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["accepted_count"] == 1
        assert body["results"][0]["accepted"] is True
        assert fake.calls, "accepted HTTP 项应产生 aria2 提交调用"
        blob = _submitted_blob(fake.calls)
        assert uri not in blob
        # 只提交 internal gateway 镜像 URI
        assert "/_internal/fetch/" in blob
        # 原始目标 host 不得泄漏进提交调用
        host = uri.split("//", 1)[1].split("/", 1)[0]
        assert host not in blob

    @patch("app.services.task_service._get_client")
    def test_magnet_submitted_canonical_only(
        self,
        mock_get_client: Any,
        authenticated_client: TestClient,
    ):
        """magnet 通过 canonical 形式提交，tracker 等外部组件不进入 aria2 调用
        （与 test_tasks_router.test_canonicalizes_magnet_before_submit 一致）。"""
        fake = CapturingAria2Client()
        mock_get_client.return_value = fake
        magnet_uri = (
            "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
            "&tr=https://tracker.example/announce"
        )
        response = authenticated_client.post(
            "/api/tasks", json={"tasks": [{"uri": magnet_uri}]}
        )

        assert response.status_code == 200
        assert response.json()["accepted_count"] == 1
        blob = _submitted_blob(fake.calls)
        assert "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567" in blob
        assert "tracker.example" not in blob


class TestSchemeAllowlist:
    @pytest.mark.parametrize(
        "uri",
        ["ftp://ftp.example.com/file.zip", "ftp://192.168.1.1/file.zip", "custom:data"],
    )
    @patch("app.services.task_service._get_client")
    def test_reject_unsupported_scheme_per_item_without_aria2(
        self,
        mock_get_client: Any,
        authenticated_client: TestClient,
        uri: str,
    ):
        fake = CapturingAria2Client()
        mock_get_client.return_value = fake
        response = authenticated_client.post(
            "/api/tasks", json={"tasks": [{"uri": uri}]}
        )

        assert response.status_code == 200
        item = response.json()["results"][0]
        assert item["accepted"] is False
        assert item["error"] == "仅支持磁力链接和 HTTP(S) 下载链接"
        assert item["task_id"] is None
        assert fake.calls == []
