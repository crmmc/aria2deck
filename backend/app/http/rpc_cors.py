from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send


def is_rpc_http_request(scope: Scope) -> bool:
    return bool(scope["type"] == "http" and scope["path"] == "/aria2/jsonrpc")


class NonRpcCORSMiddleware(CORSMiddleware):
    """Leave RPC CORS to the outer, credential-free policy."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if is_rpc_http_request(scope):
            await self.app(scope, receive, send)
        else:
            await super().__call__(scope, receive, send)


class RpcCORSMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.rpc_app = CORSMiddleware(
            app,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["POST", "OPTIONS"],
            allow_headers=["Content-Type"],
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if is_rpc_http_request(scope):
            await self.rpc_app(scope, receive, send)
        else:
            await self.app(scope, receive, send)


class RpcCorsFastAPI(FastAPI):
    def build_middleware_stack(self) -> ASGIApp:
        # Wrap ServerErrorMiddleware too, so unhandled 500s remain readable.
        return RpcCORSMiddleware(super().build_middleware_stack())
