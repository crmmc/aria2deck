import pytest
from fastapi.testclient import TestClient


class TestFirstUserFlow:
    def test_create_first_user(self, client: TestClient, temp_db: str):
        response = client.post("/api/users", json={
            "username": "firstuser",
            "password": "password123",
            "is_admin": True,
        })
        assert response.status_code == 200
        assert response.json()["username"] == "firstuser"
        assert response.json()["is_admin"] is True

    @pytest.mark.parametrize(
        ("payload", "expected_quota"),
        [
            (
                {"username": "firstadmin", "password": "password123", "is_admin": True},
                107374182400,
            ),
            (
                {
                    "username": "firstadmin",
                    "password": "password123",
                    "is_admin": True,
                    "quota": 50 * 1024 * 1024 * 1024,
                },
                50 * 1024 * 1024 * 1024,
            ),
        ],
        ids=["default-quota", "custom-quota"],
    )
    def test_create_first_user_quota_behavior(
        self,
        client: TestClient,
        temp_db: str,
        payload: dict,
        expected_quota: int,
    ):
        response = client.post("/api/users", json=payload)
        assert response.status_code == 200
        assert response.json()["quota"] == expected_quota

    def test_create_second_user_requires_admin(self, client: TestClient, temp_db: str):
        client.post("/api/users", json={
            "username": "firstadmin",
            "password": "password123",
            "is_admin": True,
        })

        response = client.post("/api/users", json={
            "username": "seconduser",
            "password": "password123",
            "is_admin": False,
        })
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_first_user_creation_blocked_after_many_attempts(self, client: TestClient, temp_db: str):
        from app.core.rate_limit import login_limiter

        for _ in range(5):
            await login_limiter.record_failure("testclient")

        response = client.post("/api/users", json={
            "username": "firstuser",
            "password": "password123",
            "is_admin": True,
        })
        assert response.status_code == 429
        assert response.json()["detail"] == "请求过于频繁，请稍后再试"

    def test_first_user_race_condition_second_request_rejected(
        self, client: TestClient, temp_db: str
    ):
        response1 = client.post("/api/users", json={
            "username": "firstuser",
            "password": "password123",
            "is_admin": True,
        })
        assert response1.status_code == 200

        response2 = client.post("/api/users", json={
            "username": "seconduser",
            "password": "password123",
            "is_admin": False,
        })
        assert response2.status_code == 401
