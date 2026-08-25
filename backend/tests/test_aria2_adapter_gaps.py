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


class TestBuildSubmissionCall:
    """M24 Task 1: shared submission descriptor builder."""

    def _patch_env(self, monkeypatch):
        monkeypatch.setattr(mod, "get_task_download_dir", lambda tid: f"/data/{tid}")
        monkeypatch.setattr(
            mod, "get_aria2_bt_stop_timeout_seconds", lambda: 300
        )

        def fake_gateway(*, download_id, source_uri, options, source_uris=None):
            uris = [f"http://gw/_internal/fetch/{download_id}/{i}" for i in range(len(source_uris or [source_uri]))]
            return uris, {"header": ["X-Aria2Deck-Capability: cap"], "out": "payload"}

        monkeypatch.setattr(mod, "build_gateway_submission", fake_gateway)

    def test_http_with_planned_gid_forces_pause(self, monkeypatch):
        self._patch_env(monkeypatch)
        call = mod.build_submission_call(
            _download(id=7, size_known=1),
            uri="https://x.example/f",
            options={},
            planned_gid="0123456789abcdef",
        )
        assert call.method == "aria2.addUri"
        uris, opts = call.params
        assert uris == ["http://gw/_internal/fetch/7/0"]
        assert opts["gid"] == "0123456789abcdef"
        assert opts["pause"] == "true"
        assert opts["dir"] == "/data/7"
        assert opts["seed-time"] == "0"
        assert opts["bt-stop-timeout"] == "300"
        assert opts["header"] == ["X-Aria2Deck-Capability: cap"]
        assert call.status == "paused"
        assert call.error_code == mod.ERROR_ADMISSION_PAUSED
        assert call.extra_uris == []

    def test_http_without_planned_gid_keeps_single_task_behavior(self, monkeypatch):
        self._patch_env(monkeypatch)
        call = mod.build_submission_call(
            _download(id=7, size_known=1), uri="https://x.example/f", options={}
        )
        _, opts = call.params
        assert "gid" not in opts
        assert "pause" not in opts
        assert call.status == "active"
        assert call.error_code is None

    def test_http_unknown_size_without_gid_pauses(self, monkeypatch):
        self._patch_env(monkeypatch)
        call = mod.build_submission_call(
            _download(id=7, size_known=0), uri="https://x.example/f", options={}
        )
        _, opts = call.params
        assert opts["pause"] == "true"
        assert call.status == "paused"
        assert call.error_code == mod.ERROR_ADMISSION_PAUSED

    def test_http_mirrors_in_extra_uris(self, monkeypatch):
        self._patch_env(monkeypatch)
        call = mod.build_submission_call(
            _download(id=7, size_known=1),
            uri="https://x/f",
            options={"mirrors": ["https://m1/f", "https://m2/f"]},
        )
        _, opts = call.params
        assert call.extra_uris == [
            "http://gw/_internal/fetch/7/1",
            "http://gw/_internal/fetch/7/2",
        ]
        assert opts["out"] == "payload"

    def test_magnet_unknown_size_pause_metadata(self, monkeypatch):
        self._patch_env(monkeypatch)
        call = mod.build_submission_call(
            _download(id=8, resource_kind="magnet", size_known=0),
            uri="magnet:?xt=urn:btih:x",
            options={"bt-tracker": "http://tr/ann", "out": "f"},
            planned_gid="aaaabbbbccccdddd",
        )
        assert call.method == "aria2.addUri"
        uris, opts = call.params
        assert uris == ["magnet:?xt=urn:btih:x"]
        assert opts["pause-metadata"] == "true"
        assert opts["gid"] == "aaaabbbbccccdddd"
        assert opts["bt-tracker"] == "http://tr/ann"
        assert opts["out"] == "f"
        assert call.status == "paused"
        assert call.error_code == mod.ERROR_METADATA_ADMISSION_PAUSED
        assert call.extra_uris == []

    def test_magnet_known_size_active(self, monkeypatch):
        self._patch_env(monkeypatch)
        call = mod.build_submission_call(
            _download(id=8, resource_kind="magnet", size_known=1),
            uri="magnet:?xt=urn:btih:x",
            options={},
        )
        _, opts = call.params
        assert "pause-metadata" not in opts
        assert call.status == "active"
        assert call.error_code is None

    def test_torrent_pauses_and_select_file(self, monkeypatch):
        self._patch_env(monkeypatch)
        call = mod.build_submission_call(
            _download(id=9, resource_kind="torrent", size_known=1),
            uri="base64:AAAA",
            options={"select-file": "1,2"},
        )
        assert call.method == "aria2.addTorrent"
        torrent, uris, opts = call.params
        assert torrent == "AAAA"
        assert uris == []
        assert opts["pause"] == "true"
        assert opts["select-file"] == "1,2"
        assert call.status == "paused"
        assert call.error_code == mod.ERROR_ADMISSION_PAUSED

    def test_unknown_kind_falls_back_to_add_uri(self, monkeypatch):
        self._patch_env(monkeypatch)
        call = mod.build_submission_call(
            _download(id=10, resource_kind="magnet", size_known=1),
            uri="magnet:?xt=urn:btih:y",
            options={},
        )
        assert call.method == "aria2.addUri"


@pytest.mark.asyncio
class TestSubmitUsesBuilder:
    async def test_submit_params_match_builder(self, monkeypatch):
        TestBuildSubmissionCall()._patch_env(monkeypatch)
        _patch_download(monkeypatch, _download(id=7, size_known=1))
        state = _patch_assign(monkeypatch)
        recorded: dict = {}

        real_builder = mod.build_submission_call

        def spy(download, *, uri, options, planned_gid=None):
            recorded["args"] = (download, uri, options, planned_gid)
            return real_builder(download, uri=uri, options=options, planned_gid=planned_gid)

        monkeypatch.setattr(mod, "build_submission_call", spy)
        client = make_aria2_client(add_uri="gid7")
        adapter = Aria2BackendAdapter(client)
        gid = await adapter.submit(tid=7, uri="https://x.example/f", options={"out": "f.zip"})
        assert gid == "gid7"
        download, uri, options, planned_gid = recorded["args"]
        assert download["id"] == 7 and uri == "https://x.example/f"
        assert planned_gid is None
        uris, opts = client.add_uri.await_args.args
        expected = real_builder(_download(id=7, size_known=1), uri=uri, options={"out": "f.zip"})
        assert list(uris) == expected.params[0]
        assert opts == expected.params[1]
        assert state["assigned"][2] == expected.status
        assert state["assigned"][3] == expected.error_code

    async def test_submit_torrent_uses_builder(self, monkeypatch):
        TestBuildSubmissionCall()._patch_env(monkeypatch)
        _patch_download(monkeypatch, _download(id=9, resource_kind="torrent", size_known=1))
        state = _patch_assign(monkeypatch)
        client = make_aria2_client(add_torrent="gid9")
        adapter = Aria2BackendAdapter(client)
        gid = await adapter.submit(tid=9, uri="base64:QQ==", options={})
        assert gid == "gid9"
        torrent, uris, opts = client.add_torrent.await_args.args
        assert torrent == "QQ=="
        assert opts["pause"] == "true"
        assert state["assigned"][2] == "paused"
        assert state["assigned"][3] == mod.ERROR_ADMISSION_PAUSED


class TestBuildSubmissionCallFallbackStatus:
    """非 http/非 magnet fallback：unknown-size 与 status 派生的表驱动验证。"""

    @pytest.mark.parametrize(
        "kind,size_known,expected_status,expected_error_code",
        [
            # torrent + unknown size：非 base64 URI 走 addUri fallback → waiting
            ("torrent", 0, "waiting", None),
            # torrent + 已知大小 → active
            ("torrent", 1, "active", None),
            # magnet + unknown size → pause-metadata + paused
            (
                "magnet",
                0,
                "paused",
                mod.ERROR_METADATA_ADMISSION_PAUSED,
            ),
            # magnet + 已知大小 → active
            ("magnet", 1, "active", None),
        ],
    )
    def test_fallback_status_table(
        self, kind, size_known, expected_status, expected_error_code
    ):
        download = _download(
            resource_kind=kind,
            source_uri="magnet:?xt=urn:btih:" + "0" * 40,
            size_known=size_known,
        )
        call = mod.build_submission_call(
            download,
            uri="magnet:?xt=urn:btih:" + "0" * 40,
            options=None,
        )
        assert call.method == "aria2.addUri"
        assert call.params[0] == ["magnet:?xt=urn:btih:" + "0" * 40]
        assert call.status == expected_status
        assert call.error_code == expected_error_code
        # magnet unknown-size 时才注入 pause-metadata
        if kind == "magnet" and not size_known:
            assert call.params[1]["pause-metadata"] == "true"
        else:
            assert "pause-metadata" not in call.params[1]
        assert "pause" not in call.params[1]

    def test_planned_gid_injected_into_fallback_options(self):
        download = _download(
            resource_kind="torrent",
            source_uri="magnet:?xt=urn:btih:" + "1" * 40,
            size_known=0,
        )
        call = mod.build_submission_call(
            download,
            uri="magnet:?xt=urn:btih:" + "1" * 40,
            options=None,
            planned_gid="abcdef0123456789",
        )
        assert call.params[1]["gid"] == "abcdef0123456789"
        assert call.status == "waiting"
