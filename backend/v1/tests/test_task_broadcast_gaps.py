"""Coverage gaps for app/services/task_broadcast.py reservation & cleanup paths."""

from __future__ import annotations

import pytest

from app.services import task_broadcast as tb


class FakeWs:
    def __init__(self, fail_send: bool = False, fail_close: bool = False):
        self.sent = []
        self.fail_send = fail_send
        self.fail_close = fail_close
        self.closed = None

    async def send_json(self, payload):
        if self.fail_send:
            raise RuntimeError("send failed")
        self.sent.append(payload)

    async def close(self, code=None):
        if self.fail_close:
            raise RuntimeError("close failed")
        self.closed = code


@pytest.fixture(autouse=True)
def _clean_state():
    tb._ws_connections.clear()
    tb._ws_connections_by_ip.clear()
    tb._ws_connections_by_session.clear()
    tb._ws_connection_info.clear()
    tb._ws_reservations_by_user.clear()
    tb._ws_reservations_by_ip.clear()
    tb._ws_reservations_by_session.clear()
    tb._ws_reservation_info.clear()
    yield
    for d in (
        tb._ws_connections,
        tb._ws_connections_by_ip,
        tb._ws_connections_by_session,
        tb._ws_connection_info,
        tb._ws_reservations_by_user,
        tb._ws_reservations_by_ip,
        tb._ws_reservations_by_session,
        tb._ws_reservation_info,
    ):
        d.clear()


@pytest.mark.asyncio
async def test_reserve_user_limit():
    for _ in range(tb.MAX_WS_CONNECTIONS_PER_USER):
        r = await tb.reserve_ws_slot(1, "s", "1.2.3.4")
        assert r is not None
    assert await tb.reserve_ws_slot(1, "s2", "1.2.3.4") is None


@pytest.mark.asyncio
async def test_activate_unknown_reservation():
    assert await tb.activate_ws_slot(object(), FakeWs()) is False


@pytest.mark.asyncio
async def test_activate_already_registered_ws():
    ws = FakeWs()
    r = await tb.reserve_ws_slot(1, "s", "ip")
    await tb.activate_ws_slot(r, ws)
    r2 = await tb.reserve_ws_slot(1, "s2", "ip2")
    assert await tb.activate_ws_slot(r2, ws) is False
    # 原连接保持注册，新预约被释放
    assert ws in tb._ws_connections[1]
    assert r2 not in tb._ws_reservation_info


@pytest.mark.asyncio
async def test_set_connections_for_user_replaces_and_closes():
    old = FakeWs()
    new = FakeWs()
    r = await tb.reserve_ws_slot(1, "s", "ip")
    await tb.activate_ws_slot(r, old)
    await tb.set_connections_for_user(1, {new})
    assert old.closed == 1001
    assert tb._ws_connections[1] == {new}
    await tb.set_connections_for_user(1, set())
    assert 1 not in tb._ws_connections
    assert new.closed == 1001


@pytest.mark.asyncio
async def test_remove_connections_for_user_closes():
    ws = FakeWs(fail_close=True)
    r = await tb.reserve_ws_slot(1, "s", "ip")
    await tb.activate_ws_slot(r, ws)
    await tb.remove_connections_for_user(1)
    assert not tb._ws_connections


@pytest.mark.asyncio
async def test_remove_connections_for_user_releases_reservations():
    r = await tb.reserve_ws_slot(1, "s", "ip")
    await tb.remove_connections_for_user(1)
    assert not tb._ws_reservations_by_user


@pytest.mark.asyncio
async def test_broadcast_detaches_failing_socket(monkeypatch):
    ws = FakeWs(fail_send=True)
    r = await tb.reserve_ws_slot(1, "s", "ip")
    await tb.activate_ws_slot(r, ws)

    async def fake_list(task_id):
        return [
            {
                "user_id": 1,
                "id": 1,
                "global_download_id": 1,
                "status": "active",
                "display_name": "f",
                "gid": "g",
                "uri": "https://x/f",
                "name": "f",
                "error": None,
            }
        ]

    async def fake_attach(rows):
        return rows

    monkeypatch.setattr(tb, "list_user_tasks_for_download", fake_list)
    monkeypatch.setattr(tb, "attach_snapshots_to_rows", fake_attach)
    await tb.broadcast_task_update_to_subscribers(1)
    assert ws not in tb._ws_connection_info
