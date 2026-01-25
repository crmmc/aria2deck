"""删除用户时清理任务测试

测试场景：
1. 删除用户时同时删除其任务记录
2. 删除用户时可选删除用户目录
3. 删除用户时同时删除其打包任务记录
"""
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import settings
from app.db import execute, fetch_one, fetch_all


class TestDeleteUserCleanup:
    """删除用户时清理任务测试套件"""

    def test_delete_user_removes_tasks(
        self, client: TestClient, test_admin: dict, admin_session: str
    ):
        """测试删除用户时同时删除其任务记录"""
        # 创建测试用户
        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.post(
            "/api/users",
            json={
                "username": "testuser_tasks",
                "password": "password123",
                "is_admin": False
            }
        )
        assert response.status_code == 200
        user_id = response.json()["id"]

        # 为该用户创建任务记录
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        task_id = execute(
            """
            INSERT INTO tasks (owner_id, gid, uri, status, name, total_length, completed_length,
                              download_speed, upload_speed, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [user_id, "test_gid_123", "https://example.com/file.zip", "complete",
             "file.zip", 1000000, 1000000, 0, 0, now, now]
        )
        assert task_id is not None

        # 确认任务存在
        task = fetch_one("SELECT * FROM tasks WHERE id = ?", [task_id])
        assert task is not None

        # 删除用户
        response = client.delete(f"/api/users/{user_id}")
        assert response.status_code == 200

        # 确认任务被删除
        task = fetch_one("SELECT * FROM tasks WHERE id = ?", [task_id])
        assert task is None

    def test_delete_user_removes_pack_tasks(
        self, client: TestClient, test_admin: dict, admin_session: str
    ):
        """测试删除用户时同时删除其打包任务记录"""
        # 创建测试用户
        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.post(
            "/api/users",
            json={
                "username": "testuser_pack",
                "password": "password123",
                "is_admin": False
            }
        )
        assert response.status_code == 200
        user_id = response.json()["id"]

        # 为该用户创建打包任务记录
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        pack_task_id = execute(
            """
            INSERT INTO pack_tasks (owner_id, folder_path, folder_size, reserved_space, status, progress, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [user_id, "/test/path", 1000000, 1000000, "complete", 100, now, now]
        )
        assert pack_task_id is not None

        # 确认打包任务存在
        pack_task = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [pack_task_id])
        assert pack_task is not None

        # 删除用户
        response = client.delete(f"/api/users/{user_id}")
        assert response.status_code == 200

        # 确认打包任务被删除
        pack_task = fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [pack_task_id])
        assert pack_task is None

    def test_delete_user_with_files(
        self, client: TestClient, test_admin: dict, admin_session: str
    ):
        """测试删除用户时可选删除用户目录"""
        # 创建测试用户
        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.post(
            "/api/users",
            json={
                "username": "testuser_files",
                "password": "password123",
                "is_admin": False
            }
        )
        assert response.status_code == 200
        user_id = response.json()["id"]

        # 创建用户目录和测试文件
        user_dir = Path(settings.download_dir) / str(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        test_file = user_dir / "test.txt"
        test_file.write_text("test content")

        # 确认目录存在
        assert user_dir.exists()
        assert test_file.exists()

        # 删除用户但不删除文件
        response = client.delete(f"/api/users/{user_id}?delete_files=false")
        assert response.status_code == 200

        # 确认目录仍然存在
        assert user_dir.exists()

        # 清理
        import shutil
        shutil.rmtree(user_dir, ignore_errors=True)

    def test_delete_user_with_files_cleanup(
        self, client: TestClient, test_admin: dict, admin_session: str
    ):
        """测试删除用户时同时删除用户目录"""
        # 创建测试用户
        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.post(
            "/api/users",
            json={
                "username": "testuser_cleanup",
                "password": "password123",
                "is_admin": False
            }
        )
        assert response.status_code == 200
        user_id = response.json()["id"]

        # 创建用户目录和测试文件
        user_dir = Path(settings.download_dir) / str(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        test_file = user_dir / "test.txt"
        test_file.write_text("test content")

        # 确认目录存在
        assert user_dir.exists()
        assert test_file.exists()

        # 删除用户并删除文件
        response = client.delete(f"/api/users/{user_id}?delete_files=true")
        assert response.status_code == 200

        # 确认目录被删除
        assert not user_dir.exists()

    def test_delete_user_multiple_tasks(
        self, client: TestClient, test_admin: dict, admin_session: str
    ):
        """测试删除用户时删除其所有任务"""
        # 创建测试用户
        client.cookies.set(settings.session_cookie_name, admin_session)
        response = client.post(
            "/api/users",
            json={
                "username": "testuser_multi_tasks",
                "password": "password123",
                "is_admin": False
            }
        )
        assert response.status_code == 200
        user_id = response.json()["id"]

        # 为该用户创建多个任务记录
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        task_ids = []
        for i in range(5):
            task_id = execute(
                """
                INSERT INTO tasks (owner_id, gid, uri, status, name, total_length, completed_length,
                                  download_speed, upload_speed, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [user_id, f"test_gid_{i}", f"https://example.com/file{i}.zip", "complete",
                 f"file{i}.zip", 1000000, 1000000, 0, 0, now, now]
            )
            task_ids.append(task_id)

        # 确认任务存在
        tasks = fetch_all("SELECT * FROM tasks WHERE owner_id = ?", [user_id])
        assert len(tasks) == 5

        # 删除用户
        response = client.delete(f"/api/users/{user_id}")
        assert response.status_code == 200

        # 确认所有任务被删除
        tasks = fetch_all("SELECT * FROM tasks WHERE owner_id = ?", [user_id])
        assert len(tasks) == 0
