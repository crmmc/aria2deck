from __future__ import annotations

from typing import cast

from fastapi import Request

from app.aria2.client import Aria2Client
from app.core.config import settings

_cached_rpc_url: str | None = None
_cached_rpc_secret: str | None = None


def resolve_aria2_config() -> tuple[str, str]:
    rpc_url = _cached_rpc_url
    if rpc_url is None:
        rpc_url = settings.aria2_rpc_url

    rpc_secret = _cached_rpc_secret
    if rpc_secret is None:
        rpc_secret = settings.aria2_rpc_secret

    return str(rpc_url), str(rpc_secret)


def get_aria2_client(
    request: Request | None = None,
) -> Aria2Client:
    rpc_url, rpc_secret = resolve_aria2_config()

    if request and hasattr(request.app.state, "aria2_client"):
        client = request.app.state.aria2_client
        if client._rpc_url == rpc_url and client._secret == rpc_secret:
            return cast(Aria2Client, client)
        new_client = Aria2Client(rpc_url, rpc_secret)
        request.app.state.aria2_client = new_client
        return new_client

    return Aria2Client(rpc_url, rpc_secret)


def create_aria2_client(rpc_url: str, rpc_secret: str = "") -> Aria2Client:
    return Aria2Client(rpc_url, rpc_secret)


def update_cached_aria2_config(
    *,
    rpc_url: str | None,
    rpc_secret: str | None,
) -> None:
    global _cached_rpc_url, _cached_rpc_secret
    _cached_rpc_url = rpc_url if rpc_url is not None else settings.aria2_rpc_url
    _cached_rpc_secret = rpc_secret if rpc_secret is not None else settings.aria2_rpc_secret
