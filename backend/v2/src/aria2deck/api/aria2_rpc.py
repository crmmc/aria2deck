from __future__ import annotations

import json
import logging
from typing import cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from ..application.accounts.models import Principal
from ..application.accounts.service import AccountService
from ..application.downloads.rpc_service import Aria2RpcService, RpcMethodError
from ..application.settings.rate_limit import RateLimiter
from ..domain.errors import DomainError
from .dependencies import account_service, client_ip, rate_limiter
from .rpc_cors import cors_headers

logger = logging.getLogger(__name__)

router = APIRouter(tags=["aria2-rpc"])

MAX_BATCH = 20
_JSON_RPC_VERSION = "2.0"
_TOKEN_PREFIX = "token:"  # noqa: S105  # JSON-RPC 参数前缀标记，非密钥


def _result(payload_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": _JSON_RPC_VERSION, "id": payload_id, "result": result}


def _error(payload_id: object | None, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": _JSON_RPC_VERSION, "id": payload_id, "error": {"code": code, "message": message}}


def _strip_token(params: object) -> tuple[object, str | None]:
    """Remove the leading token:<secret> parameter; return (rest, secret)."""
    if isinstance(params, list) and params:
        first = params[0]
        if isinstance(first, str) and first.startswith(_TOKEN_PREFIX):
            return params[1:], first[len(_TOKEN_PREFIX) :]
    return params, None


def _valid_envelope(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("jsonrpc") != _JSON_RPC_VERSION:
        return None
    method = payload.get("method")
    if not isinstance(method, str) or not method:
        return None
    if "params" in payload and not isinstance(payload.get("params"), list | dict):
        return None
    return payload


def _params_list(params: object) -> list[object]:
    if isinstance(params, list):
        return list(params)
    if isinstance(params, dict):
        return [params]
    return []


def _payload_id(item: object) -> object | None:
    return item.get("id") if isinstance(item, dict) else None


def _extract_secret(payload: dict[str, object]) -> tuple[object, str | None]:
    """Extract the RPC secret from outer params or, for multicall, first sub-call.

    兼容两种标准形态：外层 params[0] 的 token:<secret>，以及 AriaNg 形态——
    外层无 token、token 在第一个子调用的 params[0]。多子调用 token 不一致时以第一个为准。
    """
    params, secret = _strip_token(payload.get("params"))
    if secret or str(payload.get("method")) != "system.multicall":
        return params, secret
    if not isinstance(params, list) or not params or not isinstance(params[0], list):
        return params, None
    calls = cast(list[object], params[0])
    for call in calls:
        if not isinstance(call, dict):
            continue
        _child_params, child_secret = _strip_token(call.get("params"))
        if child_secret:
            return params, child_secret
    return params, None


def _multicall_cost(params: object) -> int:
    if not isinstance(params, list) or not params or not isinstance(params[0], list):
        return 1
    calls = cast(list[object], params[0])
    return max(1, sum(isinstance(call, dict) for call in calls))


def _single_cost(payload: dict[str, object]) -> int:
    if str(payload.get("method")) != "system.multicall":
        return 1
    return _multicall_cost(payload.get("params"))


def _request_cost(body: object) -> int:
    if isinstance(body, list):
        return max(1, sum(_single_cost(item) for item in body if isinstance(item, dict)))
    if isinstance(body, dict):
        return _single_cost(body)
    return 1


def _is_notification(payload: dict[str, object]) -> bool:
    return "id" not in payload


@router.api_route("/aria2/jsonrpc", methods=["GET", "POST", "OPTIONS"], include_in_schema=False)
async def aria2_jsonrpc(
    request: Request,
    service: AccountService = Depends(account_service),
    limiter: RateLimiter = Depends(rate_limiter),
) -> Response:
    cors = request.app.state.rpc_cors
    headers = cors_headers(
        request.headers.get("origin"),
        allow_all_origins=cors.allow_all,
        allowed_origins=cors.allowed_origins,
    )
    if request.method == "GET":
        return Response(status_code=405, headers={"Allow": "POST, OPTIONS"})
    if request.method == "OPTIONS":
        # 未配置来源时不放行预检。
        return Response(status_code=204 if headers else 403, headers=headers)

    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            status_code=200, content=_error(None, -32700, "请求体不是合法 JSON"), headers=headers
        )

    # 限流按实际执行数计费：单请求 1、batch 按项数、multicall 按子调用数（RPC-04）。
    try:
        for _ in range(_request_cost(body)):
            limiter.check_rpc(client_ip(request))
    except DomainError as error:
        retry_after = error.retry_after_seconds or 1
        return JSONResponse(
            status_code=200,
            content=_error(None, -32000, "请求过于频繁，请稍后重试"),
            headers={**headers, "Retry-After": str(retry_after)},
        )

    rpc_service: Aria2RpcService = request.app.state.aria2_rpc_service
    if isinstance(body, list):
        if not body:
            return JSONResponse(
                status_code=200, content=_error(None, -32600, "批量请求不能为空"), headers=headers
            )
        if len(body) > MAX_BATCH:
            return JSONResponse(
                status_code=200,
                content=_error(None, -32600, f"批量请求最多 {MAX_BATCH} 项"),
                headers=headers,
            )
        responses = [
            response
            for item in body
            if (response := await _handle_item(item, service, rpc_service)) is not None
        ]
        if not responses:
            return Response(status_code=204, headers=headers)
        return JSONResponse(status_code=200, content=responses, headers=headers)

    response = await _handle_item(body, service, rpc_service)
    if response is None:
        return Response(status_code=204, headers=headers)
    return JSONResponse(status_code=200, content=response, headers=headers)


async def _handle_item(
    item: object, service: AccountService, rpc_service: Aria2RpcService
) -> dict[str, object] | None:
    """Handle one request object; None = notification with no error (no response)."""
    payload_id = _payload_id(item)
    payload = _valid_envelope(item)
    if payload is None:
        return _error(payload_id, -32600, "请求信封不符合 JSON-RPC 2.0")
    method = str(payload["method"])
    params, secret = _extract_secret(payload)
    if not secret:
        # 缺 token：带 id → code 1；通知（无 id）→ 不产生响应。
        return None if _is_notification(payload) else _error(payload_id, 1, "缺少 RPC 访问令牌")
    try:
        principal = await service.authenticate_rpc_secret(secret)
    except DomainError:
        return None if _is_notification(payload) else _error(payload_id, 1, "RPC 访问令牌无效")
    if _is_notification(payload):
        await _execute(method, _params_list(params), payload_id, principal, rpc_service)
        return None  # 通知：副作用执行，不产生响应。
    return await _execute(method, _params_list(params), payload_id, principal, rpc_service)


async def _execute(
    method: str,
    params: list[object],
    payload_id: object,
    principal: Principal,
    rpc_service: Aria2RpcService,
) -> dict[str, object]:
    if method == "system.multicall":
        return await _multicall(params, payload_id, principal, rpc_service)
    try:
        result = await rpc_service.dispatch(method, params, user_id=principal.user.id)
        return _result(payload_id, result)
    except RpcMethodError as error:
        return _error(payload_id, error.code, str(error))
    except DomainError as error:
        # 业务失败按 aria2 惯例 code 1 + 中文消息（如提交结果暂无法确认）。
        return _error(payload_id, 1, str(error))
    except Exception as error:  # noqa: BLE001  # 内部异常只记类型，不泄露原文
        logger.warning("RPC 内部错误 method=%s error_type=%s", method, type(error).__name__)
        return _error(payload_id, -32603, "服务器内部错误")


async def _multicall(
    params: list[object],
    payload_id: object,
    principal: Principal,
    rpc_service: Aria2RpcService,
) -> dict[str, object]:
    if not params or not isinstance(params[0], list):
        return _error(payload_id, -32602, "multicall 参数无效")
    calls = cast(list[object], params[0])
    if not calls:
        return _error(payload_id, -32602, "multicall 不能为空")
    if len(calls) > MAX_BATCH:
        return _error(payload_id, -32602, f"multicall 最多 {MAX_BATCH} 个子调用")
    results: list[object] = []
    for call in calls:
        results.append(await _multicall_child(call, principal, rpc_service))
    return _result(payload_id, results)


async def _multicall_child(
    call: object, principal: Principal, rpc_service: Aria2RpcService
) -> object:
    """One sub-call: same user as the authenticated outer request; [result] or fault."""
    if not isinstance(call, dict) or not isinstance(call.get("methodName"), str):
        return {"faultCode": -32602, "faultString": "子调用信封无效"}
    method = str(call["methodName"])
    if method == "system.multicall":
        return {"faultCode": -32602, "faultString": "不能嵌套调用 system.multicall"}
    params, _child_secret = _strip_token(call.get("params"))
    # 鉴权已由外层/首子调用 token 完成；子级 token 一律剥离、同一用户执行。
    try:
        result = await rpc_service.dispatch(method, _params_list(params), user_id=principal.user.id)
        return [result]
    except RpcMethodError as error:
        return {"faultCode": error.code, "faultString": str(error)}
    except DomainError as error:
        return {"faultCode": 1, "faultString": str(error)}
    except Exception as error:  # noqa: BLE001
        logger.warning("RPC 子调用错误 method=%s error_type=%s", method, type(error).__name__)
        return {"faultCode": -32603, "faultString": "服务器内部错误"}
