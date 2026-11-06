"""全局 HTTP 请求体大小上限（A5）：content-length 预检 + 实际读取计数的双保险。"""

from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# 为 14M 字符级 base64 种子字段的 JSON 外壳留出余量。
MAX_HTTP_REQUEST_BODY_BYTES = 16 * 1024 * 1024
REQUEST_BODY_TOO_LARGE_MESSAGE = "请求体过大，最大支持 16 MiB"


class _RequestBodyTooLarge(HTTPException):
    """必须继承 HTTPException：FastAPI 会把 body 读取期的普通异常转成 400，
    只有 HTTPException 子类会被原样上抛，中间件才能改写成 413。"""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail=REQUEST_BODY_TOO_LARGE_MESSAGE)


def _reject_response() -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={
            "error": {"code": "task_too_large", "message": REQUEST_BODY_TOO_LARGE_MESSAGE}
        },
    )


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int = MAX_HTTP_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                if int(value) > self.max_body_bytes:
                    await _reject_response()(scope, receive, send)
                    return
            except ValueError:
                # 防御分支：content-length 非数字时跳过预检，交给下方实际读取计数兜底
                # （常规客户端与 httpx 测试栈都不会发出非数字长度，用原生 ASGI 断言）。
                pass

        received_bytes = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_body_bytes:
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            # 覆盖豁免：response_started=True 的分支不可稳定触发——现有端点都在读完
            # 请求体之后才开始写响应，超限异常必然先于 response.start 抛出；
            # 本分支只防御未来边流式读体边写响应的端点（此时已无法补发 413）。
            # 相邻可观测边界（未开始响应即超限 → 413）由
            # tests/api/test_request_body_limit.py 的分块传输用例锁定。
            if response_started:
                raise
            await _reject_response()(scope, receive, send)
