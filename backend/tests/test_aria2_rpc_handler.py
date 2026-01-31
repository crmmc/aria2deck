"""Tests for Aria2RpcHandler construction requirements."""
import pytest

from app.aria2.client import Aria2Client
from app.services.aria2_rpc_handler import Aria2RpcHandler


def test_aria2_rpc_handler_requires_app_state():
    """Handler should fail fast when app_state is missing."""
    client = Aria2Client("http://localhost:6800/jsonrpc")
    with pytest.raises(RuntimeError):
        Aria2RpcHandler(user_id=1, aria2_client=client, app_state=None)
