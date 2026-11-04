from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from ..domain.errors import DomainError, ErrorCode
from .contracts import ErrorResponse

logger = logging.getLogger(__name__)

_STATUS = {
    ErrorCode.INVALID_INPUT: 422,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.CREDENTIAL_INVALID: 401,
    ErrorCode.MISSING_CREDENTIALS: 401,
    ErrorCode.INVALID_CREDENTIALS: 403,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.DUPLICATE_RESOURCE: 409,
    ErrorCode.INVALID_STATE: 409,
    ErrorCode.QUOTA_EXCEEDED: 409,
    ErrorCode.PHYSICAL_SPACE_EXCEEDED: 409,
    ErrorCode.CONTENT_BUSY: 409,
    ErrorCode.CONTENT_PENDING_DELETE: 409,
    ErrorCode.LAST_ADMIN: 409,
    ErrorCode.SELF_DELETE: 409,
    ErrorCode.SHARE_LIMIT_EXCEEDED: 409,
    ErrorCode.STALE_EVENT: 409,
    ErrorCode.SHARE_EXPIRED: 410,
    ErrorCode.SHARE_REVOKED: 410,
    ErrorCode.SHARE_EXHAUSTED: 410,
    ErrorCode.SHARE_UNAVAILABLE: 410,
    ErrorCode.RANGE_NOT_SATISFIABLE: 416,
    ErrorCode.SUBMISSION_UNCERTAIN: 502,
    ErrorCode.TASK_TOO_LARGE: 413,
    ErrorCode.DOWNLOAD_UNAVAILABLE: 410,
    ErrorCode.UPSTREAM_FAILURE: 502,
    ErrorCode.RECOVERY_UNSAFE: 503,
    ErrorCode.CONFIGURATION_INVALID: 500,
    ErrorCode.SCHEMA_MISMATCH: 500,
    ErrorCode.RATE_LIMITED: 429,
}

ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    400: {"model": ErrorResponse, "description": "请求无效"},
    401: {"model": ErrorResponse, "description": "未认证"},
    403: {"model": ErrorResponse, "description": "无权限"},
    404: {"model": ErrorResponse, "description": "资源不存在"},
    409: {"model": ErrorResponse, "description": "状态冲突"},
    413: {"model": ErrorResponse, "description": "内容过大"},
    422: {"model": ErrorResponse, "description": "输入无效"},
    429: {"model": ErrorResponse, "description": "请求过于频繁"},
    500: {"model": ErrorResponse, "description": "服务器错误"},
    502: {"model": ErrorResponse, "description": "外部服务结果不可确认"},
    503: {"model": ErrorResponse, "description": "服务暂不可用"},
}


def error_responses(*status_codes: int) -> dict[int | str, dict[str, object]]:
    return {status: ERROR_RESPONSES[status] for status in status_codes}


_HTTP_MESSAGES = {
    400: "请求无效",
    401: "请先登录",
    403: "没有执行该操作的权限",
    404: "接口不存在",
    405: "请求方法不允许",
    409: "请求状态冲突",
    413: "请求体过大",
    422: "请求参数无效",
    429: "请求过于频繁",
    500: "服务器内部错误",
    502: "外部服务暂不可用",
    503: "服务暂不可用",
}


def _content(code: str, message: str) -> dict[str, object]:
    return {"error": {"code": code, "message": message}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, error: DomainError) -> JSONResponse:
        headers = (
            {"Retry-After": str(error.retry_after_seconds)}
            if error.retry_after_seconds is not None
            else None
        )
        return JSONResponse(
            status_code=_STATUS.get(error.code, 500),
            content=_content(str(error.code), str(error)),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_content(ErrorCode.INVALID_INPUT, "请求参数无效"),
        )

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException) -> JSONResponse:
        code = {
            401: ErrorCode.UNAUTHENTICATED,
            403: ErrorCode.PERMISSION_DENIED,
            404: ErrorCode.NOT_FOUND,
            409: ErrorCode.INVALID_STATE,
            422: ErrorCode.INVALID_INPUT,
        }.get(error.status_code, "http_error")
        detail = error.detail
        message = (
            detail
            if isinstance(detail, str)
            and any("\u4e00" <= character <= "\u9fff" for character in detail)
            else _HTTP_MESSAGES.get(error.status_code, "请求失败")
        )
        return JSONResponse(
            status_code=error.status_code,
            content=_content(str(code), message),
            headers=error.headers,
        )

    @app.exception_handler(Exception)
    async def internal_error(_request: Request, error: Exception) -> JSONResponse:
        logger.error("未处理请求异常 error_type=%s", type(error).__name__)
        return JSONResponse(
            status_code=500,
            content=_content("internal_error", "服务器内部错误"),
        )
