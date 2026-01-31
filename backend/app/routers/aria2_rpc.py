"""aria2 RPC 兼容接口路由

为外部 aria2 客户端（如 AriaNg、Motrix）提供 JSON-RPC 兼容接口。
使用 token:xxx 参数认证，支持用户隔离和数据脱敏。

接口路径: POST /aria2/jsonrpc
"""
from __future__ import annotations

import secrets
from collections import defaultdict
from time import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.db import fetch_one
from app.services.aria2_rpc_handler import Aria2RpcHandler, RpcError, RpcErrorCode

router = APIRouter(tags=["aria2-rpc"])


# ============================================================================
# 限流器
# ============================================================================

class RpcRateLimiter:
    """基于 IP 的 RPC 速率限制器

    默认: 1 分钟内最多 100 次请求
    """

    def __init__(self, max_requests: int = 100, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)

    def is_blocked(self, key: str) -> bool:
        """检查是否被限制"""
        now = time()
        self._requests[key] = [t for t in self._requests[key] if now - t < self.window]
        return len(self._requests[key]) >= self.max_requests

    def record_request(self, key: str) -> None:
        """记录请求"""
        self._requests[key].append(time())


rpc_limiter = RpcRateLimiter()


# ============================================================================
# 用户认证
# ============================================================================

def get_user_by_rpc_secret(secret: str) -> dict | None:
    """通过 RPC Secret 获取用户信息（常量时间验证）

    Args:
        secret: RPC Secret

    Returns:
        用户信息字典，包含 id, username 等，无效 Secret 返回 None
    """
    user = fetch_one(
        """
        SELECT id, username, is_admin, quota
        FROM users
        WHERE rpc_secret = ?
        """,
        [secret]
    )

    if not user:
        # 执行虚拟比较以保持时间一致，防止时序攻击
        secrets.compare_digest(secret, "dummy_secret_placeholder_value")
        return None

    return dict(user)


def extract_secret_from_params(params: list) -> tuple[str | None, list]:
    """从 params 提取 secret，返回 (secret, remaining_params)

    aria2 RPC 协议中，如果使用 --rpc-secret，第一个参数通常是 token:xxx

    Args:
        params: 原始参数列表

    Returns:
        (secret, remaining_params) 元组
    """
    if not params:
        return None, params

    first_param = params[0]
    if isinstance(first_param, str) and first_param.startswith("token:"):
        secret = first_param[6:]  # 移除 "token:" 前缀
        return secret, params[1:]

    return None, params


# ============================================================================
# JSON-RPC 辅助函数
# ============================================================================

def build_jsonrpc_response(result: Any, request_id: str | int | None) -> dict:
    """构建 JSON-RPC 2.0 成功响应"""
    return {
        "jsonrpc": "2.0",
        "result": result,
        "id": request_id,
    }


def build_jsonrpc_error(code: int, message: str, request_id: str | int | None, data: Any = None) -> dict:
    """构建 JSON-RPC 2.0 错误响应"""
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {
        "jsonrpc": "2.0",
        "error": error,
        "id": request_id,
    }


# ============================================================================
# 请求处理
# ============================================================================

async def process_single_request(
    request_body: dict,
    handler: Aria2RpcHandler,
    remaining_params_override: list | None = None
) -> dict:
    """处理单个 JSON-RPC 请求

    Args:
        request_body: JSON-RPC 请求体
        handler: RPC 方法处理器
        remaining_params_override: 如果提供，使用此参数列表替代 request_body 中的 params

    Returns:
        JSON-RPC 响应
    """
    request_id = request_body.get("id")

    # 验证 JSON-RPC 格式
    if request_body.get("jsonrpc") != "2.0":
        return build_jsonrpc_error(
            RpcErrorCode.INVALID_REQUEST,
            "Invalid JSON-RPC version, must be 2.0",
            request_id
        )

    method = request_body.get("method")
    if not method or not isinstance(method, str):
        return build_jsonrpc_error(
            RpcErrorCode.INVALID_REQUEST,
            "Method is required",
            request_id
        )

    # 使用 override 参数或原始参数
    if remaining_params_override is not None:
        params = remaining_params_override
    else:
        params = request_body.get("params", [])
        if not isinstance(params, list):
            return build_jsonrpc_error(
                RpcErrorCode.INVALID_PARAMS,
                "Params must be an array",
                request_id
            )
        # 移除 token 前缀参数（用于批量请求中的每个请求）
        _, params = extract_secret_from_params(params)

    try:
        result = await handler.handle(method, params)
        return build_jsonrpc_response(result, request_id)
    except RpcError as exc:
        return build_jsonrpc_error(exc.code, exc.message, request_id, exc.data)
    except Exception:
        return build_jsonrpc_error(
            RpcErrorCode.INTERNAL_ERROR,
            "Internal server error",
            request_id
        )


# ============================================================================
# 路由
# ============================================================================

@router.post("/aria2/jsonrpc")
async def jsonrpc_handler(request: Request) -> JSONResponse:
    """aria2 JSON-RPC 兼容接口（使用 token:xxx 参数认证）

    接收标准的 aria2 JSON-RPC 请求，支持单个请求和批量请求。
    认证方式：在 params[0] 中传入 token:xxx，其中 xxx 为用户的 RPC Secret。

    请求体:
        JSON-RPC 2.0 格式:
        {
            "jsonrpc": "2.0",
            "method": "aria2.addUri",
            "params": ["token:your_secret", ["http://example.com/file.zip"]],
            "id": "1"
        }

        或批量请求（数组）:
        [
            {"jsonrpc": "2.0", "method": "...", "params": ["token:xxx", ...], "id": "1"},
            {"jsonrpc": "2.0", "method": "...", "params": ["token:xxx", ...], "id": "2"}
        ]

    返回:
        JSON-RPC 2.0 响应格式
    """
    # 0. 限流检查
    client_ip = request.client.host if request.client else "unknown"
    if rpc_limiter.is_blocked(client_ip):
        return JSONResponse(
            content=build_jsonrpc_error(
                -32000,  # Server error
                "Rate limit exceeded, please try again later",
                None
            ),
            status_code=200
        )
    rpc_limiter.record_request(client_ip)

    # 1. 解析请求体
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            content=build_jsonrpc_error(
                RpcErrorCode.PARSE_ERROR,
                "Parse error: Invalid JSON",
                None
            ),
            status_code=200
        )

    # 2. 提取 token 并验证用户
    # 对于单个请求，从 params[0] 提取
    # 对于批量请求，从第一个请求的 params[0] 提取
    if isinstance(body, list):
        if not body:
            return JSONResponse(
                content=build_jsonrpc_error(
                    RpcErrorCode.INVALID_REQUEST,
                    "Empty batch request",
                    None
                ),
                status_code=200
            )
        first_request = body[0] if isinstance(body[0], dict) else {}
        params = first_request.get("params", [])
    elif isinstance(body, dict):
        params = body.get("params", [])
    else:
        return JSONResponse(
            content=build_jsonrpc_error(
                RpcErrorCode.INVALID_REQUEST,
                "Request must be an object or array",
                None
            ),
            status_code=200
        )

    if not isinstance(params, list):
        return JSONResponse(
            content=build_jsonrpc_error(
                RpcErrorCode.INVALID_PARAMS,
                "Params must be an array",
                None
            ),
            status_code=200
        )

    secret, remaining_params = extract_secret_from_params(params)

    if not secret:
        return JSONResponse(
            content=build_jsonrpc_error(
                1,  # Unauthorized
                "Missing token parameter",
                None
            ),
            status_code=200
        )

    user = get_user_by_rpc_secret(secret)
    if not user:
        return JSONResponse(
            content=build_jsonrpc_error(
                1,  # Unauthorized
                "Invalid token",
                None
            ),
            status_code=200
        )

    # 3. 创建处理器
    aria2_client = request.app.state.aria2_client
    app_state = request.app.state.app_state
    handler = Aria2RpcHandler(user["id"], aria2_client, app_state)

    # 4. 处理请求（支持单个和批量）
    if isinstance(body, list):
        # 批量请求
        responses = []
        for idx, item in enumerate(body):
            if isinstance(item, dict):
                # 第一个请求使用已提取的 remaining_params
                if idx == 0:
                    response = await process_single_request(item, handler, remaining_params)
                else:
                    response = await process_single_request(item, handler)
                responses.append(response)
            else:
                responses.append(build_jsonrpc_error(
                    RpcErrorCode.INVALID_REQUEST,
                    "Invalid request in batch",
                    None
                ))
        return JSONResponse(content=responses, status_code=200)
    else:
        # 单个请求
        response = await process_single_request(body, handler, remaining_params)
        return JSONResponse(content=response, status_code=200)
