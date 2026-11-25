"""全端点未认证拒绝冒烟门禁。

目的：现有 69 个端点各有逐端点测试，但没有任何测试**遍历路由表**证明
「新端点漏挂认证会被发现」。认证是逐端点手写的 `Depends(required_principal)`，
漏挂不会有任何现有测试变红。本文件补上这一层。

四道断言：

1. 静态判定：每个 Route 要么挂了认证依赖，要么在公开白名单里。
2. 白名单不留死项：白名单里的每条路径都能在路由表里找到。
3. 白名单不掩盖问题：白名单里的端点确实没有认证依赖（若已补上，提示可移除）。
4. 动态验证：对每个受保护端点发未认证请求，断言状态码在 {401, 403}。

公开白名单的判定依据是「该端点按设计无需登录」。新增公开端点时必须在此登记并写明理由，
这是刻意的人工闸门：让「对外暴露」成为一次显式决定，而不是忘挂认证的副作用。
"""

from __future__ import annotations

import inspect
import re
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Route, WebSocketRoute

from aria2deck.api import dependencies as api_dependencies

# 认证依赖：端点签名里出现其中之一，即视为受保护。
AUTH_DEPENDENCIES = frozenset(
    {
        api_dependencies.required_principal,
        api_dependencies.required_session_principal,
        api_dependencies.required_admin_principal,
    }
)

# 路径参数占位值。断言点是「认证先于业务校验」，因此这些值不需要真实存在。
PATH_PARAM_VALUES: dict[str, str] = {
    "attempt_id": "1",
    "code": "deadbeefdeadbeef",
    "content_id": "1",
    "file_id": "1",
    "pack_id": "1",
    "requested_path": "index.html",
    "source_index": "0",
    "task_id": "1",
    "token_id": "1",
    "user_id": "1",
}

# 公开端点白名单：(方法, 路径) -> 免认证理由。
PUBLIC_ENDPOINTS: dict[tuple[str, str], str] = {
    ("GET", "/api/health"): "存活探针，容器编排与监控需要无凭据访问",
    ("GET", "/api/health/live"): "存活探针（同上，别名路径）",
    ("GET", "/api/health/ready"): "就绪探针，供编排判断是否接入流量",
    ("POST", "/api/v2/auth/login"): "登录入口本身不能要求已登录",
    ("POST", "/api/v2/users"): "首次部署创建初始管理员；已有用户时由业务层拒绝",
    ("GET", "/api/v2/public/site-info"): "登录页需要站点标题等公开配置",
    ("GET", "/aria2/jsonrpc"): "aria2 兼容 RPC；凭据由 rpc-secret 在报文内校验",
    ("POST", "/aria2/jsonrpc"): "aria2 兼容 RPC（同上）",
    ("OPTIONS", "/aria2/jsonrpc"): "aria2 兼容 RPC 的 CORS 预检",
    ("GET", "/api/s/{code}/info"): "公开分享：凭分享码访问，无需登录",
    ("POST", "/api/s/{code}/verify"): "公开分享：提交分享密码换取访问令牌",
    ("GET", "/api/s/{code}/entries"): "公开分享：目录浏览；目录分享需令牌",
    ("GET", "/api/s/{code}/download"): "公开分享：下载；有密码时需 Bearer 令牌",
    (
        "GET",
        "/api/v2/internal/fetch/{attempt_id}/{source_index}",
    ): "内部下载网关；由 X-Mjiz-Fetch-Capability 签名校验，非 principal 认证",
    ("GET", "/docs"): "FastAPI 自带交互文档",
    ("GET", "/docs/oauth2-redirect"): "FastAPI 自带文档的 OAuth 回跳",
    ("GET", "/openapi.json"): "OpenAPI 契约文件",
    ("GET", "/redoc"): "FastAPI 自带 ReDoc 文档",
    ("HEAD", "/docs"): "同上（HEAD 变体）",
    ("HEAD", "/docs/oauth2-redirect"): "同上（HEAD 变体）",
    ("HEAD", "/openapi.json"): "同上（HEAD 变体）",
    ("HEAD", "/redoc"): "同上（HEAD 变体）",
    ("GET", "/{requested_path:path}"): "SPA 静态兜底；只返回前端资源，不含业务数据",
}

# catch-all 路由不参与动态请求：它服务前端静态资源，任意未认证 GET 都会命中最宽匹配。
_CATCH_ALL_PATHS = frozenset({"/{requested_path:path}"})


def _iter_http_routes(app: FastAPI) -> list[Route]:
    return [route for route in app.routes if isinstance(route, Route)]


def _auth_dependency_of(endpoint: Any) -> Any | None:
    """返回端点声明的认证依赖；没有则返回 None。"""
    for parameter in inspect.signature(endpoint).parameters.values():
        dependency = getattr(parameter.default, "dependency", None)
        if dependency in AUTH_DEPENDENCIES:
            return dependency
    return None


def _method_pairs(route: Route) -> list[tuple[str, str]]:
    return [(method, route.path) for method in sorted(route.methods or ())]


def _fill_path(path: str) -> str:
    """把路径参数替换为占位值。"""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in PATH_PARAM_VALUES:
            raise AssertionError(
                f"路径参数 {name} 缺少占位值：请在 PATH_PARAM_VALUES 中补充（路径 {path}）"
            )
        return PATH_PARAM_VALUES[name]

    return re.sub(r"\{(\w+)(?::\w+)?\}", replace, path)


def test_every_route_has_authentication_or_is_whitelisted(app: FastAPI) -> None:
    """每个端点要么挂了认证依赖，要么显式进入公开白名单。"""
    unprotected: list[str] = []
    for route in _iter_http_routes(app):
        if _auth_dependency_of(route.endpoint) is not None:
            continue
        for method, path in _method_pairs(route):
            if (method, path) not in PUBLIC_ENDPOINTS:
                unprotected.append(f"{method} {path}")

    assert not unprotected, (
        "以下端点没有认证依赖，也不在公开白名单里。\n"
        "若应受保护：给处理函数加 `principal: Principal = Depends(required_principal)`。\n"
        "若应公开：在 PUBLIC_ENDPOINTS 登记并写明理由。\n"
        + "\n".join(f"  - {item}" for item in sorted(unprotected))
    )


def test_whitelist_entries_all_exist(app: FastAPI) -> None:
    """白名单不能有死项：每条都必须对应真实注册的路由。"""
    registered: set[tuple[str, str]] = set()
    for route in _iter_http_routes(app):
        registered.update(_method_pairs(route))

    stale = sorted(set(PUBLIC_ENDPOINTS) - registered)
    assert not stale, (
        "公开白名单里有不存在的端点（路由已删除或改名），请同步清理：\n"
        + "\n".join(f"  - {method} {path}" for method, path in stale)
    )


def test_whitelist_entries_are_actually_unauthenticated(app: FastAPI) -> None:
    """白名单不能掩盖已补上的认证：若端点已挂认证，应从白名单移除。"""
    mismatched: list[str] = []
    for route in _iter_http_routes(app):
        if _auth_dependency_of(route.endpoint) is None:
            continue
        for method, path in _method_pairs(route):
            if (method, path) in PUBLIC_ENDPOINTS:
                mismatched.append(f"{method} {path}")

    assert not mismatched, (
        "以下端点已挂认证依赖，却仍留在公开白名单里，请移除对应条目：\n"
        + "\n".join(f"  - {item}" for item in sorted(mismatched))
    )


def test_unauthenticated_requests_are_rejected(client: TestClient, app: FastAPI) -> None:
    """对每个端点发未认证请求，必须被明确拒绝。

    遍历**全部**端点（不只看已挂认证的），只排除公开白名单与 catch-all。
    若只看已挂认证的端点，漏挂认证的端点会被直接跳过，这个测试就永远发现
    不了本部类缺陷——这是本门禁最容易被写错的地方（实测：把 list_files 的
    认证改成 None 后，原写法仍绿）。

    只接受 401/403。漏挂认证的端点往往不是返回 2xx，而是带着 None 继续
    执行然后 500，所以 5xx 同样属「未拒绝」。
    """
    exposed: list[str] = []
    for route in _iter_http_routes(app):
        if route.path in _CATCH_ALL_PATHS:
            continue
        url = _fill_path(route.path)
        for method in sorted(route.methods or ()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            if (method, route.path) in PUBLIC_ENDPOINTS:
                continue
            response = client.request(method, url)
            if response.status_code not in {401, 403}:
                exposed.append(f"{method} {route.path} -> {response.status_code}")

    assert not exposed, (
        "以下端点在未认证时没有得到明确的 401/403：\n"
        + "\n".join(f"  - {item}" for item in sorted(exposed))
    )


def test_internal_fetch_endpoint_requires_capability(client: TestClient) -> None:
    """内部下载网关不走 principal 认证，但缺 capability 时必须 401。

    它出现在公开白名单里（无 principal 依赖），所以单独断言其真实防护有效，
    避免「白名单」变成「无防护」。
    """
    response = client.get("/api/v2/internal/fetch/1/0")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"

    forged = client.get(
        "/api/v2/internal/fetch/1/0", headers={"X-Mjiz-Fetch-Capability": "forged"}
    )
    assert forged.status_code == 403
    assert forged.json()["error"]["code"] == "permission_denied"


def test_websocket_requires_session(client: TestClient, app: FastAPI) -> None:
    """WebSocket 端点未认证时必须关闭连接（4401）。"""
    ws_routes = [route for route in app.routes if isinstance(route, WebSocketRoute)]
    assert ws_routes, "未找到任何 WebSocket 路由，若已移除请同步删除本测试"

    with pytest.raises(Exception) as caught:  # noqa: B017  # 关闭原因随 starlette 版本而异
        with client.websocket_connect("/ws/tasks"):
            pass
    assert getattr(caught.value, "code", None) == 4401


def test_guard_counts_are_consistent(app: FastAPI) -> None:
    """受保护与公开端点的数量关系自洽，防止枚举逻辑悄悄失效。"""
    protected: set[tuple[str, str]] = set()
    public: set[tuple[str, str]] = set()
    for route in _iter_http_routes(app):
        target = protected if _auth_dependency_of(route.endpoint) is not None else public
        target.update(_method_pairs(route))

    assert protected, "没有识别到任何受保护端点，认证判定逻辑可能已失效"
    assert public, "没有识别到任何公开端点，判定逻辑可能已失效"
    assert protected.isdisjoint(public)
    # 白名单必须恰好覆盖公开端点集，多一个少一个都会在其它测试里报错，
    # 这里再断言一次确保三者关系不会被单点修改破坏。
    assert public == set(PUBLIC_ENDPOINTS)
