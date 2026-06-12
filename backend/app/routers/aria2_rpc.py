"""aria2 RPC 兼容接口路由

为外部 aria2 客户端（如 AriaNg、Motrix）提供 JSON-RPC 兼容接口。
使用 token:xxx 参数认证，支持用户隔离和数据脱敏。

接口路径: POST/GET /aria2/jsonrpc
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.aria2.gateway import get_aria2_client
from app.auth import get_user_by_rpc_secret
from app.core.config import settings
from app.core.rate_limit import rpc_limiter
from app.core.rate_limit_config import rate_limit_config
from app.services.aria2_rpc_handler import Aria2RpcHandler, RpcError, RpcErrorCode

router = APIRouter(tags=["aria2-rpc"])
logger = logging.getLogger(__name__)


def _build_rate_limit_response() -> JSONResponse:
    return JSONResponse(
        content=build_jsonrpc_error(
            -32000,  # Server error
            "Rate limit exceeded, please try again later",
            None,
        ),
        status_code=200,
    )


async def _authenticate_from_params(
    params: Any,
    request_id: str | int | None,
    client_ip: str,
    outer_request_id: str,
    method: str | None = None,
) -> tuple[dict | None, list | None, dict | None]:
    if not isinstance(params, list):
        logger.warning("RPC参数类型错误 ip=%s request_id=%s", client_ip, outer_request_id)
        return None, None, build_jsonrpc_error(
            RpcErrorCode.INVALID_PARAMS,
            "Params must be an array",
            request_id,
        )

    if method == "system.multicall":
        secret, remaining_params = extract_secret_for_multicall(params)
    else:
        secret, remaining_params = extract_secret_from_params(params)
    if not secret:
        logger.warning("RPC缺少Token ip=%s request_id=%s", client_ip, outer_request_id)
        return None, None, build_jsonrpc_error(
            1,  # Unauthorized
            "Missing token parameter",
            request_id,
        )

    user = await get_user_by_rpc_secret(secret)
    if not user:
        logger.warning("RPC鉴权失败 ip=%s request_id=%s", client_ip, outer_request_id)
        return None, None, build_jsonrpc_error(
            1,  # Unauthorized
            "Invalid token",
            request_id,
        )

    return user, remaining_params, None


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


def extract_secret_for_multicall(params: list) -> tuple[str | None, list]:
    """从 system.multicall 的 nested calls 中提取 token

    system.multicall 的 params 结构是 [[{methodName, params}, ...]]，
    token 放在每个 nested call 的 params[0] 而非顶层。
    从第一个 nested call 中提取 token 做顶层鉴权。
    """
    if not params or not isinstance(params[0], list) or not params[0]:
        return None, params

    first_call = params[0][0]
    if not isinstance(first_call, dict):
        return None, params

    call_params = first_call.get("params")
    if not isinstance(call_params, list) or not call_params:
        return None, params

    first_param = call_params[0]
    if isinstance(first_param, str) and first_param.startswith("token:"):
        return first_param[6:], params

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


def _extract_rpc_method(request_body: dict) -> str:
    method = request_body.get("method")
    if isinstance(method, str) and method:
        return method
    return "<invalid>"


def _extract_response_error(response: dict) -> tuple[int | None, str]:
    error = response.get("error")
    if not isinstance(error, dict):
        return None, ""

    code = error.get("code")
    message = error.get("message")
    error_code = code if isinstance(code, int) else None
    error_message = message if isinstance(message, str) else ""
    return error_code, error_message


def _log_rpc_method_response(
    method: str,
    rpc_id: str | int | None,
    user_id: int | None,
    request_id: str,
    response: dict,
) -> None:
    error_code, error_message = _extract_response_error(response)
    if error_code is None:
        success_level = logging.INFO if settings.debug else logging.DEBUG
        logger.log(
            success_level,
            "RPC方法响应成功 method=%s rpc_id=%s user_id=%s request_id=%s",
            method,
            rpc_id,
            user_id,
            request_id,
        )
        return

    log_message = (
        "RPC方法响应失败 method=%s rpc_id=%s user_id=%s request_id=%s "
        "code=%s message=%s"
    )
    if error_code == RpcErrorCode.INTERNAL_ERROR:
        logger.error(log_message, method, rpc_id, user_id, request_id, error_code, error_message or "<empty>")
        return
    logger.warning(log_message, method, rpc_id, user_id, request_id, error_code, error_message or "<empty>")


def _decode_query_params(raw_params: str | None) -> list | None:
    if raw_params is None:
        return []

    value = raw_params.strip()
    if not value:
        return []

    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    padded = value + "=" * (-len(value) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            decoded = decoder(padded.encode("utf-8"))
            parsed = json.loads(decoded.decode("utf-8"))
            if isinstance(parsed, list):
                return parsed
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            continue

    return None


def _build_body_from_query(request: Request) -> tuple[dict | None, dict | None]:
    method = request.query_params.get("method")
    if not method:
        return None, build_jsonrpc_error(
            RpcErrorCode.INVALID_REQUEST,
            "Method is required",
            request.query_params.get("id"),
        )

    params = _decode_query_params(request.query_params.get("params"))
    if params is None:
        return None, build_jsonrpc_error(
            RpcErrorCode.INVALID_PARAMS,
            "Params must be JSON array or Base64-encoded JSON array",
            request.query_params.get("id"),
        )

    body = {
        "jsonrpc": request.query_params.get("jsonrpc", "2.0"),
        "method": method,
        "params": params,
        "id": request.query_params.get("id"),
    }
    return body, None


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
        logger.warning(
            "RPC方法调用失败 method=%s user_id=%s code=%s request_id=%s",
            method,
            handler.user_id,
            exc.code,
            request_id,
        )
        return build_jsonrpc_error(exc.code, exc.message, request_id, exc.data)
    except Exception:
        logger.exception("RPC方法内部异常 method=%s user_id=%s request_id=%s", method, handler.user_id, request_id)
        return build_jsonrpc_error(
            RpcErrorCode.INTERNAL_ERROR,
            "Internal server error",
            request_id
        )


# ============================================================================
# 路由
# ============================================================================

async def _handle_jsonrpc_request_body(request: Request, body: Any) -> JSONResponse:
    client_ip = request.client.host if request.client else "unknown"
    request_id = getattr(request.state, "request_id", "-")
    if not await rpc_limiter.is_allowed(client_ip, limit=rate_limit_config.rpc):
        logger.warning("RPC请求被限流 ip=%s request_id=%s", client_ip, request_id)
        return _build_rate_limit_response()

    if isinstance(body, list):
        if not body:
            logger.warning("RPC空批量请求 ip=%s request_id=%s", client_ip, request_id)
            return JSONResponse(
                content=build_jsonrpc_error(
                    RpcErrorCode.INVALID_REQUEST,
                    "Empty batch request",
                    None
                ),
                status_code=200
            )

        MAX_BATCH_SIZE = 20
        if len(body) > MAX_BATCH_SIZE:
            logger.warning(
                "RPC批量请求过大 ip=%s request_id=%s count=%s",
                client_ip, request_id, len(body),
            )
            return JSONResponse(
                content=build_jsonrpc_error(
                    RpcErrorCode.INVALID_REQUEST,
                    f"Batch too large, max {MAX_BATCH_SIZE} requests",
                    None,
                ),
                status_code=200,
            )

        for _ in range(max(0, len(body) - 1)):
            if not await rpc_limiter.is_allowed(client_ip, limit=rate_limit_config.rpc):
                logger.warning("RPC批量请求被限流 ip=%s request_id=%s", client_ip, request_id)
                return _build_rate_limit_response()
    elif not isinstance(body, dict):
        return JSONResponse(
            content=build_jsonrpc_error(
                RpcErrorCode.INVALID_REQUEST,
                "Request must be an object or array",
                None
            ),
            status_code=200
        )

    aria2_client = get_aria2_client(request=request)
    if isinstance(body, list):
        responses = []
        for item in body:
            if isinstance(item, dict):
                item_request_id = item.get("id")
                user, remaining_params, auth_error = await _authenticate_from_params(
                    item.get("params", []),
                    item_request_id,
                    client_ip,
                    request_id,
                )
                if auth_error is not None:
                    _log_rpc_method_response(
                        method=_extract_rpc_method(item),
                        rpc_id=item_request_id,
                        user_id=None,
                        request_id=request_id,
                        response=auth_error,
                    )
                    responses.append(auth_error)
                    continue
                if user is None or remaining_params is None:
                    response = build_jsonrpc_error(
                        RpcErrorCode.INTERNAL_ERROR,
                        "Internal server error",
                        item_request_id,
                    )
                    _log_rpc_method_response(
                        method=_extract_rpc_method(item),
                        rpc_id=item_request_id,
                        user_id=None,
                        request_id=request_id,
                        response=response,
                    )
                    responses.append(response)
                    continue

                handler = Aria2RpcHandler(user["id"], aria2_client)
                response = await process_single_request(item, handler, remaining_params)
                _log_rpc_method_response(
                    method=_extract_rpc_method(item),
                    rpc_id=item_request_id,
                    user_id=user["id"],
                    request_id=request_id,
                    response=response,
                )
                responses.append(response)
            else:
                response = build_jsonrpc_error(
                    RpcErrorCode.INVALID_REQUEST,
                    "Invalid request in batch",
                    None
                )
                _log_rpc_method_response(
                    method="<invalid>",
                    rpc_id=None,
                    user_id=None,
                    request_id=request_id,
                    response=response,
                )
                responses.append(response)
        logger.info("RPC批量请求完成 count=%s request_id=%s", len(responses), request_id)
        return JSONResponse(content=responses, status_code=200)

    user, remaining_params, auth_error = await _authenticate_from_params(
        body.get("params", []),
        body.get("id"),
        client_ip,
        request_id,
        method=body.get("method") if isinstance(body, dict) else None,
    )
    if auth_error is not None:
        _log_rpc_method_response(
            method=_extract_rpc_method(body),
            rpc_id=body.get("id"),
            user_id=None,
            request_id=request_id,
            response=auth_error,
        )
        return JSONResponse(content=auth_error, status_code=200)
    if user is None or remaining_params is None:
        response = build_jsonrpc_error(
            RpcErrorCode.INTERNAL_ERROR,
            "Internal server error",
            body.get("id"),
        )
        _log_rpc_method_response(
            method=_extract_rpc_method(body),
            rpc_id=body.get("id"),
            user_id=None,
            request_id=request_id,
            response=response,
        )
        return JSONResponse(
            content=response,
            status_code=200,
        )

    handler = Aria2RpcHandler(user["id"], aria2_client)
    logger.info("RPC请求通过鉴权 user_id=%s ip=%s request_id=%s", user["id"], client_ip, request_id)

    response = await process_single_request(body, handler, remaining_params)
    _log_rpc_method_response(
        method=_extract_rpc_method(body),
        rpc_id=body.get("id"),
        user_id=user["id"],
        request_id=request_id,
        response=response,
    )
    logger.info("RPC单请求完成 user_id=%s request_id=%s", user["id"], request_id)
    return JSONResponse(content=response, status_code=200)


@router.post("/aria2/jsonrpc")
async def jsonrpc_handler(request: Request) -> JSONResponse:
    """aria2 JSON-RPC 兼容接口（POST body，使用 token:xxx 参数认证）

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
    try:
        body = await request.json()
    except Exception:
        client_ip = request.client.host if request.client else "unknown"
        request_id = getattr(request.state, "request_id", "-")
        logger.warning("RPC请求解析JSON失败 ip=%s request_id=%s", client_ip, request_id)
        return JSONResponse(
            content=build_jsonrpc_error(
                RpcErrorCode.PARSE_ERROR,
                "Parse error: Invalid JSON",
                None
            ),
            status_code=200
        )

    return await _handle_jsonrpc_request_body(request, body)


@router.get("/aria2/jsonrpc")
async def jsonrpc_handler_get(request: Request) -> JSONResponse:
    body, error = _build_body_from_query(request)
    if error is not None:
        return JSONResponse(content=error, status_code=200)
    return await _handle_jsonrpc_request_body(request, body)
