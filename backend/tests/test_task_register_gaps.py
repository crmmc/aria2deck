"""Coverage gaps for app/modules/task_core/register.py error/race branches."""

from __future__ import annotations

import pytest

from app.modules.task_core import register as reg
from app.modules.task_core.register import RegisterError, ResourceSpec
from app.repositories.errors import RepositoryConflictError


def _spec(**kwargs) -> ResourceSpec:
    base = dict(
        resource_key="rk",
        source_uri="https://x.example/f",
        resource_kind="http",
    )
    base.update(kwargs)
    return ResourceSpec(**base)


def _row(**kwargs):
    base = {"id": 1, "status": "active"}
    base.update(kwargs)
    return base


@pytest.fixture
def usage_zero(monkeypatch):
    async def fake_usage(user_id, quota_bytes):
        return {"used_bytes": 0, "reserved_bytes": 0, "available_bytes": quota_bytes}

    monkeypatch.setattr(reg, "get_usage", fake_usage)


class TestAttach:
    @pytest.mark.asyncio
    async def test_attach_duplicate(self, monkeypatch, usage_zero):
        async def fake_find(key):
            return _row(id=5, completed_file_id=9)

        async def fake_task(user_id, tid):
            return {"id": 1}

        monkeypatch.setattr(reg, "find_latest_completed_global_download_by_resource_key", fake_find)
        monkeypatch.setattr(reg, "get_user_task", fake_task)
        with pytest.raises(RegisterError) as exc:
            await reg.register(user_id=1, quota_bytes=1000, resource=_spec())
        assert exc.value.code == "duplicate_task"

    @pytest.mark.asyncio
    async def test_attach_completed_over_quota(self, monkeypatch, usage_zero):
        async def fake_find(key):
            return _row(id=5, completed_file_id=9, completed_bytes=5000)

        async def none_task(user_id, tid):
            return None

        monkeypatch.setattr(reg, "find_latest_completed_global_download_by_resource_key", fake_find)
        monkeypatch.setattr(reg, "get_user_task", none_task)
        with pytest.raises(RegisterError) as exc:
            await reg.register(user_id=1, quota_bytes=1000, resource=_spec())
        assert exc.value.code == reg.ERROR_QUOTA_EXCEEDED

    @pytest.mark.asyncio
    async def test_attach_remaining_quota(self, monkeypatch):
        async def fake_find(key):
            return _row(id=5, completed_file_id=9, completed_bytes=500)

        async def none_task(user_id, tid):
            return None

        async def fake_usage(user_id, quota_bytes):
            return {"used_bytes": 900, "reserved_bytes": 0, "available_bytes": 100}

        monkeypatch.setattr(reg, "find_latest_completed_global_download_by_resource_key", fake_find)
        monkeypatch.setattr(reg, "get_user_task", none_task)
        monkeypatch.setattr(reg, "get_usage", fake_usage)
        with pytest.raises(RegisterError) as exc:
            await reg.register(user_id=1, quota_bytes=1000, resource=_spec())
        assert exc.value.code == reg.ERROR_QUOTA_EXCEEDED

    @pytest.mark.asyncio
    async def test_attach_repo_value_error_quota(self, monkeypatch, usage_zero):
        async def fake_find(key):
            return _row(id=5, completed_file_id=9, completed_bytes=500)

        async def none_task(user_id, tid):
            return None

        async def raise_quota(**kwargs):
            raise ValueError("quota exceeded during attach")

        monkeypatch.setattr(reg, "find_latest_completed_global_download_by_resource_key", fake_find)
        monkeypatch.setattr(reg, "get_user_task", none_task)
        monkeypatch.setattr(reg, "attach_completed_file_to_user", raise_quota)
        with pytest.raises(RegisterError) as exc:
            await reg.register(user_id=1, quota_bytes=1000, resource=_spec())
        assert exc.value.code == reg.ERROR_QUOTA_EXCEEDED

    @pytest.mark.asyncio
    async def test_attach_repo_value_error_other(self, monkeypatch, usage_zero):
        async def fake_find(key):
            return _row(id=5, completed_file_id=9, completed_bytes=500)

        async def none_task(user_id, tid):
            return None

        async def raise_other(**kwargs):
            raise ValueError("boom")

        monkeypatch.setattr(reg, "find_latest_completed_global_download_by_resource_key", fake_find)
        monkeypatch.setattr(reg, "get_user_task", none_task)
        monkeypatch.setattr(reg, "attach_completed_file_to_user", raise_other)
        with pytest.raises(ValueError):
            await reg.register(user_id=1, quota_bytes=1000, resource=_spec())

    @pytest.mark.asyncio
    async def test_attach_repo_conflict(self, monkeypatch, usage_zero):
        async def fake_find(key):
            return _row(id=5, completed_file_id=9, completed_bytes=500)

        async def none_task(user_id, tid):
            return None

        async def raise_conflict(**kwargs):
            raise RepositoryConflictError

        monkeypatch.setattr(reg, "find_latest_completed_global_download_by_resource_key", fake_find)
        monkeypatch.setattr(reg, "get_user_task", none_task)
        monkeypatch.setattr(reg, "attach_completed_file_to_user", raise_conflict)
        with pytest.raises(RegisterError) as exc:
            await reg.register(user_id=1, quota_bytes=1000, resource=_spec())
        assert exc.value.code == "conflict"

    @pytest.mark.asyncio
    async def test_attach_success(self, monkeypatch, usage_zero):
        async def fake_find(key):
            return _row(
                id=5,
                completed_file_id=9,
                completed_bytes=500,
                display_name="f",
                completed_at_ms=1,
            )

        async def none_task(user_id, tid):
            return None

        async def fake_attach(**kwargs):
            return {"id": 11, "status": "completed"}

        monkeypatch.setattr(reg, "find_latest_completed_global_download_by_resource_key", fake_find)
        monkeypatch.setattr(reg, "get_user_task", none_task)
        monkeypatch.setattr(reg, "attach_completed_file_to_user", fake_attach)
        result = await reg.register(user_id=1, quota_bytes=1000, resource=_spec())
        assert result.outcome == "attached_completed"


class TestJoinLive:
    @pytest.mark.asyncio
    async def test_join_over_quota(self, monkeypatch):
        async def none_find(key):
            return None

        async def live_find(key):
            return _row(id=7, total_bytes=2000, size_known=1)

        async def none_task(user_id, tid):
            return None

        monkeypatch.setattr(reg, "find_latest_completed_global_download_by_resource_key", none_find)
        monkeypatch.setattr(reg, "find_live_global_download_by_resource_key", live_find)
        monkeypatch.setattr(reg, "get_user_task", none_task)
        with pytest.raises(RegisterError) as exc:
            await reg.register(user_id=1, quota_bytes=1000, resource=_spec())
        assert exc.value.code == reg.ERROR_QUOTA_EXCEEDED

    @pytest.mark.asyncio
    async def test_join_remaining_quota(self, monkeypatch):
        async def none_find(key):
            return None

        async def live_find(key):
            return _row(id=7, total_bytes=500, size_known=1)

        async def none_task(user_id, tid):
            return None

        async def fake_usage(user_id, quota_bytes):
            return {"used_bytes": 900, "reserved_bytes": 0, "available_bytes": 100}

        monkeypatch.setattr(reg, "find_latest_completed_global_download_by_resource_key", none_find)
        monkeypatch.setattr(reg, "find_live_global_download_by_resource_key", live_find)
        monkeypatch.setattr(reg, "get_user_task", none_task)
        monkeypatch.setattr(reg, "get_usage", fake_usage)
        with pytest.raises(RegisterError) as exc:
            await reg.register(user_id=1, quota_bytes=1000, resource=_spec())
        assert exc.value.code == reg.ERROR_QUOTA_EXCEEDED

    @pytest.mark.asyncio
    async def test_join_reserve_value_error(self, monkeypatch, usage_zero):
        async def none_find(key):
            return None

        async def live_find(key):
            return _row(id=7, total_bytes=500, size_known=1)

        async def none_task(user_id, tid):
            return None

        async def raise_value(user_id, size, *, quota_bytes):
            raise ValueError("race")

        monkeypatch.setattr(reg, "find_latest_completed_global_download_by_resource_key", none_find)
        monkeypatch.setattr(reg, "find_live_global_download_by_resource_key", live_find)
        monkeypatch.setattr(reg, "get_user_task", none_task)
        monkeypatch.setattr(reg, "reserve_bytes", raise_value)
        with pytest.raises(RegisterError) as exc:
            await reg.register(user_id=1, quota_bytes=1000, resource=_spec())
        assert exc.value.code == reg.ERROR_QUOTA_EXCEEDED

    @pytest.mark.asyncio
    async def test_join_create_user_task_fails_releases(self, monkeypatch, usage_zero):
        async def none_find(key):
            return None

        async def fake_reserve(user_id, size, *, quota_bytes):
            pass

        monkeypatch.setattr(reg, "reserve_bytes", fake_reserve)

        async def live_find(key):
            return _row(id=7, total_bytes=500, size_known=1)

        async def none_task(user_id, tid):
            return None

        released = {}

        async def fake_release(user_id, size, *, quota_bytes):
            released["size"] = size

        async def raise_create(values):
            raise RuntimeError("db down")

        monkeypatch.setattr(reg, "find_latest_completed_global_download_by_resource_key", none_find)
        monkeypatch.setattr(reg, "find_live_global_download_by_resource_key", live_find)
        monkeypatch.setattr(reg, "get_user_task", none_task)
        monkeypatch.setattr(reg, "release_reserved", fake_release)
        monkeypatch.setattr(reg, "create_user_task", raise_create)
        with pytest.raises(RuntimeError):
            await reg.register(user_id=1, quota_bytes=1000, resource=_spec())
        assert released.get("size") == 500

    @pytest.mark.asyncio
    async def test_join_success(self, monkeypatch, usage_zero):
        async def none_find(key):
            return None

        async def fake_reserve(user_id, size, *, quota_bytes):
            pass

        monkeypatch.setattr(reg, "reserve_bytes", fake_reserve)

        async def live_find(key):
            return _row(id=7, total_bytes=500, size_known=1)

        async def none_task(user_id, tid):
            return None

        async def fake_create(values):
            return {"id": 12, "status": values["status"]}

        monkeypatch.setattr(reg, "find_latest_completed_global_download_by_resource_key", none_find)
        monkeypatch.setattr(reg, "find_live_global_download_by_resource_key", live_find)
        monkeypatch.setattr(reg, "get_user_task", none_task)
        monkeypatch.setattr(reg, "create_user_task", fake_create)
        result = await reg.register(user_id=1, quota_bytes=1000, resource=_spec())
        assert result.outcome == "joined_live"


class TestCreate:
    @pytest.mark.asyncio
    async def test_known_size_over_quota(self, monkeypatch):
        with pytest.raises(RegisterError) as exc:
            await reg.register(
                user_id=1,
                quota_bytes=100,
                resource=_spec(size_bytes=1000, size_known=True),
            )
        assert exc.value.code == reg.ERROR_QUOTA_EXCEEDED

    @pytest.mark.asyncio
    async def test_create_remaining_quota(self, monkeypatch):
        async def none_find(key):
            return None

        async def fake_usage(user_id, quota_bytes):
            return {"used_bytes": 900, "reserved_bytes": 0, "available_bytes": 100}

        monkeypatch.setattr(reg, "find_latest_completed_global_download_by_resource_key", none_find)
        monkeypatch.setattr(reg, "find_live_global_download_by_resource_key", none_find)
        monkeypatch.setattr(reg, "get_usage", fake_usage)
        with pytest.raises(RegisterError) as exc:
            await reg.register(
                user_id=1,
                quota_bytes=1000,
                resource=_spec(size_bytes=500, size_known=True),
            )
        assert exc.value.code == reg.ERROR_QUOTA_EXCEEDED

    @pytest.mark.asyncio
    async def test_create_reserve_value_error(self, monkeypatch, usage_zero):
        async def none_find(key):
            return None

        async def raise_value(user_id, size, *, quota_bytes):
            raise ValueError("race")

        monkeypatch.setattr(reg, "find_latest_completed_global_download_by_resource_key", none_find)
        monkeypatch.setattr(reg, "find_live_global_download_by_resource_key", none_find)
        monkeypatch.setattr(reg, "reserve_bytes", raise_value)
        with pytest.raises(RegisterError) as exc:
            await reg.register(
                user_id=1,
                quota_bytes=1000,
                resource=_spec(size_bytes=500, size_known=True),
            )
        assert exc.value.code == reg.ERROR_QUOTA_EXCEEDED

    @pytest.mark.asyncio
    async def test_create_race_conflict_then_stale(self, monkeypatch, usage_zero):
        async def none_find(key):
            return None
        async def fake_source(values):
            return {"id": 3}

        monkeypatch.setattr(reg, "create_download_source", fake_source)

        async def fake_reserve(user_id, size, *, quota_bytes):
            pass

        monkeypatch.setattr(reg, "reserve_bytes", fake_reserve)

        async def raise_conflict(values):
            raise RepositoryConflictError

        monkeypatch.setattr(reg, "find_latest_completed_global_download_by_resource_key", none_find)
        monkeypatch.setattr(reg, "find_live_global_download_by_resource_key", none_find)
        monkeypatch.setattr(reg, "create_global_download_attempt", raise_conflict)

        async def fake_release(user_id, size, *, quota_bytes):
            pass

        monkeypatch.setattr(reg, "release_reserved", fake_release)
        with pytest.raises(RegisterError) as exc:
            await reg.register(
                user_id=1,
                quota_bytes=1000,
                resource=_spec(size_bytes=500, size_known=True),
            )
        assert exc.value.code == "stale"

    @pytest.mark.asyncio
    async def test_create_race_conflict_then_join(self, monkeypatch, usage_zero):
        async def none_find(key):
            return None
        async def fake_source(values):
            return {"id": 3}

        monkeypatch.setattr(reg, "create_download_source", fake_source)

        async def fake_reserve(user_id, size, *, quota_bytes):
            pass

        monkeypatch.setattr(reg, "reserve_bytes", fake_reserve)

        async def live_find(key):
            return _row(id=7, total_bytes=0, size_known=0)

        async def raise_conflict(values):
            raise RepositoryConflictError

        async def fake_release(user_id, size, *, quota_bytes):
            pass

        async def fake_create_task(values):
            return {"id": 13, "status": values["status"]}

        monkeypatch.setattr(reg, "find_latest_completed_global_download_by_resource_key", none_find)
        monkeypatch.setattr(reg, "find_live_global_download_by_resource_key", live_find)
        monkeypatch.setattr(reg, "create_global_download_attempt", raise_conflict)
        monkeypatch.setattr(reg, "release_reserved", fake_release)
        monkeypatch.setattr(reg, "create_user_task", fake_create_task)

        async def none_task(user_id, tid):
            return None

        monkeypatch.setattr(reg, "get_user_task", none_task)
        result = await reg.register(
            user_id=1,
            quota_bytes=1000,
            resource=_spec(size_bytes=500, size_known=True),
        )
        assert result.outcome == "joined_live"

    @pytest.mark.asyncio
    async def test_create_user_task_fails_releases(self, monkeypatch, usage_zero):
        async def none_find(key):
            return None
        async def fake_source(values):
            return {"id": 3}

        monkeypatch.setattr(reg, "create_download_source", fake_source)

        async def fake_reserve(user_id, size, *, quota_bytes):
            pass

        monkeypatch.setattr(reg, "reserve_bytes", fake_reserve)

        released = {}

        async def fake_release(user_id, size, *, quota_bytes):
            released["size"] = size

        async def fake_source(values):
            return {"id": 3}

        async def fake_gd(values):
            return {"id": 8}

        async def raise_create(values):
            raise RuntimeError("db down")

        monkeypatch.setattr(reg, "find_latest_completed_global_download_by_resource_key", none_find)
        monkeypatch.setattr(reg, "find_live_global_download_by_resource_key", none_find)
        monkeypatch.setattr(reg, "release_reserved", fake_release)
        monkeypatch.setattr(reg, "create_download_source", fake_source)
        monkeypatch.setattr(reg, "create_global_download_attempt", fake_gd)
        monkeypatch.setattr(reg, "create_user_task", raise_create)
        with pytest.raises(RuntimeError):
            await reg.register(
                user_id=1,
                quota_bytes=1000,
                resource=_spec(size_bytes=500, size_known=True),
            )
        assert released.get("size") == 500
