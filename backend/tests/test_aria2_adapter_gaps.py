"""Coverage gaps for app/modules/backend/aria2_adapter.py."""

from __future__ import annotations

import pytest

from app.modules.backend import aria2_adapter as mod
from app.modules.backend.aria2_adapter import Aria2BackendAdapter
from tests.fakes import make_aria2_client


def _download(**kwargs):
    base = {
        "id": 1,
        "resource_kind": "http",
        "source_uri": "https://x.example/f",
        "size_known": 1,
        "aria2_gid": None,
        "status": "queued",
    }
    base.update(kwargs)
    return base


def _patch_download(monkeypatch, download):
    async def fake_get(tid):
        return download if download and tid == download.get("id") else None

    monkeypatch.setattr(mod, "get_global_download_by_id", fake_get)


def _patch_assign(monkeypatch):
    state = {}

    async def fake_assign(*, download_id, gid, status, error_code=None):
        state["assigned"] = (download_id, gid, status, error_code)
        return {"id": download_id}

    monkeypatch.setattr(mod, "assign_submitted_gid", fake_assign)
    return state


class TestSubmit:
    @pytest.mark.asyncio
    async def test_tid_not_found(self, monkeypatch):
        _patch_download(monkeypatch, None)
        adapter = Aria2BackendAdapter(make_aria2_client())
        with pytest.raises(ValueError):
            await adapter.submit(tid=99, uri="https://x/f", options={})

    @pytest.mark.asyncio
    async def test_invalid_out_option(self, monkeypatch):
        _patch_download(monkeypatch, _download(resource_kind="magnet"))
        adapter = Aria2BackendAdapter(make_aria2_client())
        with pytest.raises(ValueError):
            await adapter.submit(
                tid=1, uri="magnet:?xt=x", options={"out": "../evil"}
            )

    @pytest.mark.asyncio
    async def test_http_unknown_size_starts_paused(self, monkeypatch):
        _patch_download(monkeypatch, _download(size_known=0))
        state = _patch_assign(monkeypatch)
        adapter = Aria2BackendAdapter(make_aria2_client(add_uri="gid1"))
        gid = await adapter.submit(tid=1, uri="https://x.example/f", options={})
        assert gid == "gid1"
        assert state["assigned"][2] == "paused"
        assert state["assigned"][3] == mod.ERROR_ADMISSION_PAUSED

    @pytest.mark.asyncio
    async def test_http_known_size_active(self, monkeypatch):
        _patch_download(monkeypatch, _download(size_known=1))
        state = _patch_assign(monkeypatch)
        adapter = Aria2BackendAdapter(make_aria2_client(add_uri="gid2"))
        await adapter.submit(
            tid=1, uri="https://x.example/f", options={"out": "f.zip"}
        )
        assert state["assigned"][2] == "active"

    @pytest.mark.asyncio
    async def test_http_mirror_change_uri_failure_tolerated(self, monkeypatch):
        _patch_download(monkeypatch, _download(size_known=1))
        _patch_assign(monkeypatch)

        client = make_aria2_client(add_uri="gid3")
        client.change_uri.side_effect = RuntimeError("rpc down")
        adapter = Aria2BackendAdapter(client)
        gid = await adapter.submit(
            tid=1,
            uri="https://x.example/f",
            options={"mirrors": ["https://m.example/f"]},
        )
        assert gid == "gid3"

    @pytest.mark.asyncio
    async def test_http_mirror_change_uri_ok(self, monkeypatch):
        _patch_download(monkeypatch, _download(size_known=1))
        _patch_assign(monkeypatch)
        client = make_aria2_client(add_uri="gid4")
        adapter = Aria2BackendAdapter(client)
        await adapter.submit(
            tid=1,
            uri="https://x.example/f",
            options={"mirrors": ["https://m.example/f"]},
        )
        client.change_uri.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_torrent_starts_paused(self, monkeypatch):
        _patch_download(
            monkeypatch,
            _download(resource_kind="torrent", source_uri="base64:AAAA"),
        )
        state = _patch_assign(monkeypatch)
        adapter = Aria2BackendAdapter(make_aria2_client(add_torrent="gid5"))
        gid = await adapter.submit(tid=1, uri="base64:AAAA", options={})
        assert gid == "gid5"
        assert state["assigned"][2] == "paused"
        assert state["assigned"][3] == mod.ERROR_ADMISSION_PAUSED

    @pytest.mark.asyncio
    async def test_magnet_unknown_size_pause_metadata(self, monkeypatch):
        _patch_download(
            monkeypatch, _download(resource_kind="magnet", size_known=0)
        )
        state = _patch_assign(monkeypatch)
        adapter = Aria2BackendAdapter(make_aria2_client(add_uri="gid6"))
        gid = await adapter.submit(
            tid=1,
            uri="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
            options={},
        )
        assert gid == "gid6"
        assert state["assigned"][2] == "paused"
        assert state["assigned"][3] == mod.ERROR_METADATA_ADMISSION_PAUSED

    @pytest.mark.asyncio
    async def test_assign_returns_none(self, monkeypatch):
        _patch_download(monkeypatch, _download())

        async def none_assign(**kwargs):
            return None

        monkeypatch.setattr(mod, "assign_submitted_gid", none_assign)
        adapter = Aria2BackendAdapter(make_aria2_client(add_uri="gid7"))
        with pytest.raises(RuntimeError):
            await adapter.submit(tid=1, uri="https://x.example/f", options={})

    @pytest.mark.asyncio
    async def test_merge_user_options(self):
        options = {}
        Aria2BackendAdapter._merge_user_and_server_options(
            options,
            {
                "out": "a.zip",
                "header": "X: 1",
                "disallowed": "nope",
                "select-file": "1",
                "bt-tracker": "http://t/ann",
            },
        )
        assert options == {
            "out": "a.zip",
            "header": "X: 1",
            "select-file": "1",
            "bt-tracker": "http://t/ann",
        }


class TestTellMany:
    @pytest.mark.asyncio
    async def test_missing_rows_and_rpc_failures_skipped(self, monkeypatch):
        downloads = {
            1: _download(id=1, aria2_gid="g1"),
            2: None,
            3: _download(id=3, aria2_gid=None),
            4: _download(id=4, aria2_gid="g4"),
        }

        async def fake_get(tid):
            return downloads.get(tid)

        monkeypatch.setattr(mod, "get_global_download_by_id", fake_get)
        client = make_aria2_client()
        client.tell_status.side_effect = [
            RuntimeError("rpc down"),
            {"gid": "g4", "status": "active"},
        ]
        adapter = Aria2BackendAdapter(client)
        snapshots = await adapter.tell_many([1, 2, 3, 4])
        assert [snap.tid for snap in snapshots] == [4]


class TestPauseUnpause:
    @pytest.mark.asyncio
    async def test_resolve_missing(self, monkeypatch):
        _patch_download(monkeypatch, None)
        adapter = Aria2BackendAdapter(make_aria2_client())
        with pytest.raises(ValueError):
            await adapter.pause(1)

    @pytest.mark.asyncio
    async def test_no_gid(self, monkeypatch):
        _patch_download(monkeypatch, _download(aria2_gid=None))
        adapter = Aria2BackendAdapter(make_aria2_client())
        with pytest.raises(ValueError):
            await adapter.pause(1)

    @pytest.mark.asyncio
    async def test_pause_and_unpause(self, monkeypatch):
        _patch_download(monkeypatch, _download(aria2_gid="g9"))
        client = make_aria2_client()
        adapter = Aria2BackendAdapter(client)
        await adapter.pause(1)
        await adapter.unpause(1)
        client.pause.assert_awaited_once_with("g9")
        client.unpause.assert_awaited_once_with("g9")


class TestRemove:
    @pytest.mark.asyncio
    async def test_no_gid_noop(self, monkeypatch):
        _patch_download(monkeypatch, _download(aria2_gid=None))
        adapter = Aria2BackendAdapter(make_aria2_client())
        await adapter.remove(1)

    @pytest.mark.asyncio
    async def test_remove_success_clears_gid(self, monkeypatch):
        _patch_download(monkeypatch, _download(aria2_gid="g10"))
        cleared = {}

        async def fake_clear(tid, *, expected_gid):
            cleared["gid"] = expected_gid

        monkeypatch.setattr(mod, "clear_terminal_download_gid", fake_clear)
        client = make_aria2_client()
        adapter = Aria2BackendAdapter(client)
        await adapter.remove(1)
        client.remove.assert_awaited_once_with("g10")
        assert cleared["gid"] == "g10"

    @pytest.mark.asyncio
    async def test_remove_fallback_download_result(self, monkeypatch):
        _patch_download(monkeypatch, _download(aria2_gid="g11"))
        monkeypatch.setattr(mod, "clear_terminal_download_gid", _async_none())
        client = make_aria2_client()
        client.remove.side_effect = RuntimeError("not found")
        adapter = Aria2BackendAdapter(client)
        await adapter.remove(1)
        client.remove_download_result.assert_awaited_once_with("g11")

    @pytest.mark.asyncio
    async def test_remove_all_failures(self, monkeypatch):
        _patch_download(monkeypatch, _download(aria2_gid="g12"))
        monkeypatch.setattr(mod, "clear_terminal_download_gid", _async_none())
        client = make_aria2_client()
        client.remove.side_effect = RuntimeError("nope")
        client.remove_download_result.side_effect = RuntimeError("nope2")
        adapter = Aria2BackendAdapter(client)
        await adapter.remove(1)

    @pytest.mark.asyncio
    async def test_remove_clear_gid_failure_tolerated(self, monkeypatch):
        _patch_download(monkeypatch, _download(aria2_gid="g13"))

        async def boom(tid, *, expected_gid):
            raise RuntimeError("db down")

        monkeypatch.setattr(mod, "clear_terminal_download_gid", boom)
        client = make_aria2_client()
        adapter = Aria2BackendAdapter(client)
        await adapter.remove(1)


def _async_none():
    async def inner(*args, **kwargs):
        return None

    return inner


@pytest.mark.asyncio
async def test_join_submission_no_uris():
    adapter = Aria2BackendAdapter(make_aria2_client())
    await adapter.join_submission(tid=1, gid="g", uris=[])
