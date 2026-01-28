"""测试 Hook 空间检查和 followingGid 跟踪

测试场景：
1. BT 任务在 start 事件时检查空间
2. 磁力链接转换后通过 followingGid 跟踪
3. 超过系统限制时终止任务
4. 超过用户可用空间时终止任务
"""
import pytest
from unittest.mock import AsyncMock, patch
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.database import get_session, reset_engine
from app.models import User, Task
from app.db import init_db


@pytest.fixture
async def setup_db(tmp_path, monkeypatch):
    """创建临时数据库和目录"""
    import uuid

    # 使用唯一的数据库文件名
    db_path = tmp_path / f"test_{uuid.uuid4().hex}.db"
    download_dir = tmp_path / "downloads"
    download_dir.mkdir(exist_ok=True)

    monkeypatch.setattr("app.core.config.settings.database_path", str(db_path))
    monkeypatch.setattr("app.core.config.settings.download_dir", str(download_dir))
    monkeypatch.setattr("app.core.config.settings.hook_secret", "test_secret")

    # 重新初始化数据库引擎
    reset_engine()
    init_db()

    # 创建测试用户和任务
    async with get_session() as db:
        user = User(
            username=f"testuser_{uuid.uuid4().hex[:8]}",
            password_hash="$pbkdf2-sha256$120000$...",
            is_admin=False,
            created_at="2024-01-01T00:00:00Z",
            quota=10 * 1024 * 1024 * 1024,  # 10 GB 配额
            is_initial_password=0,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        # 创建一个已有的任务（模拟磁力链接）
        task = Task(
            owner_id=user.id,
            gid=f"original_gid_{uuid.uuid4().hex[:8]}",
            uri="magnet:?xt=urn:btih:test",
            status="active",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

        yield {"user": user, "task": task, "download_dir": download_dir}


class TestFollowingGidTracking:
    """测试磁力链接 followingGid 跟踪"""

    @pytest.mark.asyncio
    async def test_hook_finds_task_by_following_gid(self, setup_db):
        """测试通过 followingGid 找到原任务并更新 GID"""
        data = setup_db
        original_gid = data["task"].gid
        new_gid = "new_gid_xyz"

        # Mock aria2 client
        mock_aria2_status = {
            "gid": new_gid,
            "followingGid": original_gid,  # 指向原任务
            "status": "active",
            "totalLength": "1073741824",  # 1 GB
            "completedLength": "0",
            "downloadSpeed": "1000000",
            "uploadSpeed": "0",
            "files": [{"path": "/downloads/test.mkv"}],
            "bittorrent": {"info": {"name": "test.mkv"}},
        }

        with patch("app.routers.hooks.get_aria2_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.tell_status = AsyncMock(return_value=mock_aria2_status)
            mock_get_client.return_value = mock_client

            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/hooks/aria2",
                    json={"gid": new_gid, "event": "start"},
                    headers={"X-Hook-Secret": "test_secret"},
                )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["status"] == "active"

        # 验证数据库中的 GID 已更新
        async with get_session() as db:
            from sqlmodel import select
            stmt = select(Task).where(Task.id == data["task"].id)
            result = await db.exec(stmt)
            task = result.first()
            assert task.gid == new_gid  # GID 应该被更新为新的

    @pytest.mark.asyncio
    async def test_hook_returns_404_for_unknown_gid(self, setup_db):
        """测试找不到任务时返回 404"""
        # Mock aria2 client 返回没有 followingGid 的状态
        mock_aria2_status = {
            "gid": "unknown_gid",
            "status": "active",
            "totalLength": "0",
        }

        with patch("app.routers.hooks.get_aria2_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.tell_status = AsyncMock(return_value=mock_aria2_status)
            mock_get_client.return_value = mock_client

            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/hooks/aria2",
                    json={"gid": "unknown_gid", "event": "start"},
                    headers={"X-Hook-Secret": "test_secret"},
                )

        assert response.status_code == 404


class TestBTTaskSpaceCheck:
    """测试 BT 任务空间检查"""

    @pytest.mark.asyncio
    async def test_task_terminated_when_exceeds_user_quota(self, setup_db):
        """测试任务大小超过用户配额时终止任务"""
        data = setup_db
        task_gid = data["task"].gid

        # 任务大小 15 GB，用户配额 10 GB
        mock_aria2_status = {
            "gid": task_gid,
            "status": "active",
            "totalLength": str(15 * 1024 * 1024 * 1024),  # 15 GB
            "completedLength": "0",
            "downloadSpeed": "0",
            "uploadSpeed": "0",
            "files": [{"path": "/downloads/huge.mkv"}],
            "bittorrent": {"info": {"name": "huge.mkv"}},
        }

        # Mock get_max_task_size 返回 100GB，确保测试的是用户配额限制
        with patch("app.routers.hooks.get_aria2_client") as mock_get_client, \
             patch("app.routers.hooks.get_max_task_size", return_value=100 * 1024 * 1024 * 1024):
            mock_client = AsyncMock()
            mock_client.tell_status = AsyncMock(return_value=mock_aria2_status)
            mock_client.force_remove = AsyncMock()
            mock_client.remove_download_result = AsyncMock()
            mock_get_client.return_value = mock_client

            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/hooks/aria2",
                    json={"gid": task_gid, "event": "start"},
                    headers={"X-Hook-Secret": "test_secret"},
                )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["status"] == "cancelled"
        assert result["reason"] == "insufficient_space"

        # 验证 aria2 任务被删除
        mock_client.force_remove.assert_called_once_with(task_gid)

        # 验证数据库任务保留为 error 状态（支持重试）
        async with get_session() as db:
            from sqlmodel import select
            stmt = select(Task).where(Task.id == data["task"].id)
            result = await db.exec(stmt)
            task = result.first()
            assert task is not None  # 任务应该保留
            assert task.status == "error"
            assert task.gid is None  # GID 应该被清除
            assert task.error is not None  # 应该有错误信息
            assert "可用空间" in task.error

    @pytest.mark.asyncio
    async def test_task_terminated_when_exceeds_max_task_size(self, setup_db):
        """测试任务大小超过系统最大限制时终止任务"""
        data = setup_db
        task_gid = data["task"].gid

        # 任务大小 50 GB，系统限制 10 GB
        mock_aria2_status = {
            "gid": task_gid,
            "status": "active",
            "totalLength": str(50 * 1024 * 1024 * 1024),  # 50 GB
            "completedLength": "0",
            "downloadSpeed": "0",
            "uploadSpeed": "0",
            "files": [{"path": "/downloads/huge.mkv"}],
        }

        with patch("app.routers.hooks.get_aria2_client") as mock_get_client, \
             patch("app.routers.hooks.get_max_task_size", return_value=10 * 1024 * 1024 * 1024):  # 10 GB 限制
            mock_client = AsyncMock()
            mock_client.tell_status = AsyncMock(return_value=mock_aria2_status)
            mock_client.force_remove = AsyncMock()
            mock_client.remove_download_result = AsyncMock()
            mock_get_client.return_value = mock_client

            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/hooks/aria2",
                    json={"gid": task_gid, "event": "start"},
                    headers={"X-Hook-Secret": "test_secret"},
                )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["status"] == "cancelled"
        assert result["reason"] == "exceeded_max_task_size"

        # 验证数据库任务保留为 error 状态（支持重试）
        async with get_session() as db:
            from sqlmodel import select
            stmt = select(Task).where(Task.id == data["task"].id)
            result = await db.exec(stmt)
            task = result.first()
            assert task is not None  # 任务应该保留
            assert task.status == "error"
            assert task.gid is None  # GID 应该被清除
            assert task.error is not None  # 应该有错误信息
            assert "系统限制" in task.error

    @pytest.mark.asyncio
    async def test_task_proceeds_when_within_quota(self, setup_db):
        """测试任务大小在配额内时正常进行"""
        data = setup_db
        task_gid = data["task"].gid

        # 任务大小 1 GB，用户配额 10 GB
        mock_aria2_status = {
            "gid": task_gid,
            "status": "active",
            "totalLength": str(1 * 1024 * 1024 * 1024),  # 1 GB
            "completedLength": "0",
            "downloadSpeed": "1000000",
            "uploadSpeed": "0",
            "files": [{"path": "/downloads/small.mkv"}],
            "bittorrent": {"info": {"name": "small.mkv"}},
        }

        with patch("app.routers.hooks.get_aria2_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.tell_status = AsyncMock(return_value=mock_aria2_status)
            mock_get_client.return_value = mock_client

            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/hooks/aria2",
                    json={"gid": task_gid, "event": "start"},
                    headers={"X-Hook-Secret": "test_secret"},
                )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["status"] == "active"  # 正常进行

        # 验证数据库状态
        async with get_session() as db:
            from sqlmodel import select
            stmt = select(Task).where(Task.id == data["task"].id)
            result = await db.exec(stmt)
            task = result.first()
            assert task.status == "active"
            assert task.name == "small.mkv"

    @pytest.mark.asyncio
    async def test_space_check_only_on_start_event(self, setup_db):
        """测试空间检查只在 start 事件时执行"""
        data = setup_db
        task_gid = data["task"].gid

        # 任务大小超过配额，但是 complete 事件不应该触发检查
        mock_aria2_status = {
            "gid": task_gid,
            "status": "complete",
            "totalLength": str(50 * 1024 * 1024 * 1024),  # 50 GB
            "completedLength": str(50 * 1024 * 1024 * 1024),
            "downloadSpeed": "0",
            "uploadSpeed": "0",
            "files": [{"path": "/downloads/huge.mkv"}],
        }

        with patch("app.routers.hooks.get_aria2_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.tell_status = AsyncMock(return_value=mock_aria2_status)
            mock_get_client.return_value = mock_client

            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/hooks/aria2",
                    json={"gid": task_gid, "event": "complete"},
                    headers={"X-Hook-Secret": "test_secret"},
                )

        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "complete"  # 不检查空间，直接标记完成
