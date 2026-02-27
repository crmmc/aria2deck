"""Tests for state cache semantics and secret key safety checks."""
import asyncio
import gc
import weakref
from types import SimpleNamespace

import pytest

from app.aria2.client import Aria2Client
from app.core.config import DEFAULT_SECRET_KEY, check_secret_key, settings
from app.core.state import AppState, get_aria2_client, get_task_complete_lock, get_user_space_lock, refresh_aria2_config


def test_check_secret_key_raises_on_default_in_non_debug(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", DEFAULT_SECRET_KEY)

    with pytest.raises(RuntimeError):
        check_secret_key()


def test_check_secret_key_allows_default_in_debug(monkeypatch):
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "secret_key", DEFAULT_SECRET_KEY)

    check_secret_key()


def test_get_aria2_client_preserves_empty_cached_secret(monkeypatch):
    state = AppState()
    state._cached_rpc_url = "http://cached:6800/jsonrpc"
    state._cached_rpc_secret = ""

    monkeypatch.setattr(settings, "aria2_rpc_url", "http://env:6800/jsonrpc")
    monkeypatch.setattr(settings, "aria2_rpc_secret", "ENV_SECRET_SHOULD_NOT_APPLY")

    client = get_aria2_client(state=state)

    assert client._rpc_url == "http://cached:6800/jsonrpc"
    assert client._secret == ""


def test_get_aria2_client_refreshes_request_client_when_cache_changed():
    state = AppState()
    state._cached_rpc_url = "http://new-rpc:6800/jsonrpc"
    state._cached_rpc_secret = "new-secret"

    app_state = SimpleNamespace(
        aria2_client=Aria2Client("http://old-rpc:6800/jsonrpc", "old-secret"),
        app_state=state,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=app_state))

    client = get_aria2_client(request=request)

    assert client._rpc_url == "http://new-rpc:6800/jsonrpc"
    assert client._secret == "new-secret"
    assert request.app.state.aria2_client is client


@pytest.mark.asyncio
async def test_refresh_aria2_config_preserves_empty_secret(monkeypatch):
    async def fake_get_config_value_async(key: str):
        if key == "aria2_rpc_url":
            return "http://db:6800/jsonrpc"
        if key == "aria2_rpc_secret":
            return ""
        return None

    from app.routers import config as config_router
    monkeypatch.setattr(config_router, "get_config_value_async", fake_get_config_value_async)
    monkeypatch.setattr(settings, "aria2_rpc_secret", "ENV_SECRET_SHOULD_NOT_APPLY")

    state = AppState()
    await refresh_aria2_config(state)

    assert state._cached_rpc_url == "http://db:6800/jsonrpc"
    assert state._cached_rpc_secret == ""


@pytest.mark.asyncio
async def test_lock_maps_use_weak_refs_for_cleanup():
    state = AppState()

    user_lock = await get_user_space_lock(state, 123)
    complete_lock = await get_task_complete_lock(state, 456)
    submit_lock = asyncio.Lock()
    state.task_submit_locks[789] = submit_lock

    user_ref = weakref.ref(user_lock)
    complete_ref = weakref.ref(complete_lock)
    submit_ref = weakref.ref(submit_lock)

    del user_lock
    del complete_lock
    del submit_lock
    gc.collect()

    assert user_ref() is None
    assert complete_ref() is None
    assert submit_ref() is None
    assert 123 not in state.user_space_locks
    assert 456 not in state.task_complete_locks
    assert 789 not in state.task_submit_locks
