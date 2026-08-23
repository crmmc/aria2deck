"""Coverage gaps for app/services/user_service.py (error / rare branches)."""

from __future__ import annotations

import pytest

from app.auth import AuthUser
from app.domain.errors import BadRequestError, ForbiddenError, NotFoundError
from app.repositories import auth as auth_repo
from app.schemas import UserCreate, UserUpdate
from app.services import user_service as svc


def _admin(user_id: int = 1, is_admin: bool = True) -> AuthUser:
    return AuthUser(
        id=user_id,
        username="admin",
        password_hash="x",
        is_admin=is_admin,
        quota=1,
        quota_bytes=1,
        is_initial_password=False,
    )


class TestCreateUser:
    @pytest.mark.asyncio
    async def test_first_user_must_be_admin(self, temp_db):
        payload = UserCreate(username="u1", password="pass1234", is_admin=False)
        with pytest.raises(BadRequestError):
            await svc.create_user(
                payload=payload, client_ip="127.0.0.1", request_id="r", admin=None
            )

    @pytest.mark.asyncio
    async def test_first_user_race_returns_none(self, temp_db, monkeypatch):
        async def none_user(**kwargs):
            return None

        monkeypatch.setattr(auth_repo, "create_first_user_if_none", none_user)
        payload = UserCreate(username="u1", password="pass1234", is_admin=True)
        with pytest.raises(ForbiddenError):
            await svc.create_user(
                payload=payload, client_ip="127.0.0.1", request_id="r", admin=None
            )

    @pytest.mark.asyncio
    async def test_duplicate_username(self, test_admin, temp_db):
        payload = UserCreate(username="admin", password="pass1234", is_admin=False)
        with pytest.raises(BadRequestError):
            await svc.create_user(
                payload=payload,
                client_ip="127.0.0.1",
                request_id="r",
                admin=_admin(test_admin["id"]),
            )

    @pytest.mark.asyncio
    async def test_duplicate_user_error_from_repo(self, test_admin, temp_db, monkeypatch):
        async def dup(**kwargs):
            raise auth_repo.DuplicateUserError

        monkeypatch.setattr(auth_repo, "create_user", dup)
        payload = UserCreate(username="u2", password="pass1234", is_admin=False)
        with pytest.raises(BadRequestError):
            await svc.create_user(
                payload=payload,
                client_ip="127.0.0.1",
                request_id="r",
                admin=_admin(test_admin["id"]),
            )

    @pytest.mark.asyncio
    async def test_first_user_success(self, temp_db):
        payload = UserCreate(username="first", password="pass1234", is_admin=True)
        user = await svc.create_user(
            payload=payload, client_ip="127.0.0.1", request_id="r", admin=None
        )
        assert user["is_admin"] is True


class TestListAndGetUser:
    @pytest.mark.asyncio
    async def test_list_users(self, test_admin, test_user):
        rows = await svc.list_users(test_admin["id"])
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_get_user_not_found(self, temp_db):
        with pytest.raises(NotFoundError):
            await svc.get_user(actor_id=1, user_id=999)

    @pytest.mark.asyncio
    async def test_get_user(self, test_user):
        user = await svc.get_user(actor_id=test_user["id"], user_id=test_user["id"])
        assert user["username"] == "testuser"


class TestDeleteUser:
    @pytest.mark.asyncio
    async def test_self_delete_rejected(self, temp_db, test_admin):
        with pytest.raises(BadRequestError):
            await svc.delete_user(
                actor=_admin(test_admin["id"]), user_id=test_admin["id"], request_id="r"
            )

    @pytest.mark.asyncio
    async def test_not_found(self, temp_db, test_admin):
        with pytest.raises(NotFoundError):
            await svc.delete_user(actor=_admin(test_admin["id"]), user_id=999, request_id="r")

    @pytest.mark.asyncio
    async def test_delete_last_admin_rejected(self, test_admin, temp_db, monkeypatch):
        async def fake_delete(**kwargs):
            return {"id": 1}

        monkeypatch.setattr(auth_repo, "delete_user_as_admin", fake_delete)
        with pytest.raises(BadRequestError):
            await svc.delete_user(
                actor=_admin(test_admin["id"] + 1),
                user_id=test_admin["id"],
                request_id="r",
            )

    @pytest.mark.asyncio
    async def test_pending_delete_short_circuit(self, test_admin, test_user, temp_db):
        from app.db.engine import transaction
        from app.db.schema import users
        from sqlalchemy import update

        async with transaction() as conn:
            await conn.execute(
                update(users).where(users.c.id == test_user["id"]).values(pending_delete=1)
            )
        result = await svc.delete_user(
            actor=_admin(test_admin["id"]), user_id=test_user["id"], request_id="r"
        )
        assert result["state"] == "pending"

    @pytest.mark.asyncio
    async def test_delete_repo_last_admin_error(self, test_admin, test_user, temp_db, monkeypatch):
        async def raise_last_admin(**kwargs):
            raise auth_repo.LastAdminError

        monkeypatch.setattr(auth_repo, "delete_user_as_admin", raise_last_admin)
        with pytest.raises(BadRequestError):
            await svc.delete_user(
                actor=_admin(test_admin["id"]), user_id=test_user["id"], request_id="r"
            )

    @pytest.mark.asyncio
    async def test_admin_actor_invalid(self, test_admin, test_user, temp_db, monkeypatch):
        async def raise_invalid(**kwargs):
            raise auth_repo.AdminActorInvalidError

        monkeypatch.setattr(auth_repo, "delete_user_as_admin", raise_invalid)
        with pytest.raises(ForbiddenError):
            await svc.delete_user(
                actor=_admin(test_admin["id"]), user_id=test_user["id"], request_id="r"
            )

    @pytest.mark.asyncio
    async def test_cannot_mutate_self(self, test_admin, test_user, temp_db, monkeypatch):
        async def raise_self(**kwargs):
            raise auth_repo.CannotMutateSelfError

        monkeypatch.setattr(auth_repo, "delete_user_as_admin", raise_self)
        with pytest.raises(BadRequestError):
            await svc.delete_user(
                actor=_admin(test_admin["id"]), user_id=test_user["id"], request_id="r"
            )

    @pytest.mark.asyncio
    async def test_mutation_conflict(self, test_admin, test_user, temp_db, monkeypatch):
        async def raise_conflict(**kwargs):
            raise auth_repo.AdminMutationConflictError

        monkeypatch.setattr(auth_repo, "delete_user_as_admin", raise_conflict)
        with pytest.raises(BadRequestError):
            await svc.delete_user(
                actor=_admin(test_admin["id"]), user_id=test_user["id"], request_id="r"
            )

    @pytest.mark.asyncio
    async def test_deleted_none(self, test_admin, test_user, temp_db, monkeypatch):
        async def none_deleted(**kwargs):
            return None

        monkeypatch.setattr(auth_repo, "delete_user_as_admin", none_deleted)
        with pytest.raises(NotFoundError):
            await svc.delete_user(
                actor=_admin(test_admin["id"]), user_id=test_user["id"], request_id="r"
            )

    @pytest.mark.asyncio
    async def test_delete_success(self, test_admin, test_user, temp_db):
        result = await svc.delete_user(
            actor=_admin(test_admin["id"]), user_id=test_user["id"], request_id="r"
        )
        assert result["ok"] is True


class TestUpdateUser:
    @pytest.mark.asyncio
    async def test_not_found(self, temp_db, test_admin):
        payload = UserUpdate()
        with pytest.raises(NotFoundError):
            await svc.update_user(
                actor=_admin(test_admin["id"]),
                user_id=999,
                payload=payload,
                request_id="r",
            )

    @pytest.mark.asyncio
    async def test_username_without_password(self, test_admin, test_user):
        payload = UserUpdate(username="newname")
        with pytest.raises(BadRequestError):
            await svc.update_user(
                actor=_admin(test_admin["id"]),
                user_id=test_user["id"],
                payload=payload,
                request_id="r",
            )

    @pytest.mark.asyncio
    async def test_username_taken(self, test_admin, test_user):
        payload = UserUpdate(username="admin", password="newpass123")
        with pytest.raises(BadRequestError):
            await svc.update_user(
                actor=_admin(test_admin["id"]),
                user_id=test_user["id"],
                payload=payload,
                request_id="r",
            )

    @pytest.mark.asyncio
    async def test_demote_self(self, test_admin):
        payload = UserUpdate(is_admin=False)
        with pytest.raises(BadRequestError):
            await svc.update_user(
                actor=_admin(test_admin["id"]),
                user_id=test_admin["id"],
                payload=payload,
                request_id="r",
            )

    @pytest.mark.asyncio
    async def test_update_success_password_and_quota(self, test_admin, test_user):
        payload = UserUpdate(password="newpass123", quota=1024 * 1024)
        result = await svc.update_user(
            actor=_admin(test_admin["id"]),
            user_id=test_user["id"],
            payload=payload,
            request_id="r",
        )
        assert result["quota"] == 1024 * 1024

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc,expected",
        [
            (auth_repo.DuplicateUserError, BadRequestError),
            (auth_repo.AdminActorInvalidError, ForbiddenError),
            (auth_repo.CannotMutateSelfError, BadRequestError),
            (auth_repo.LastAdminError, BadRequestError),
            (auth_repo.UsernamePasswordRequiredError, BadRequestError),
            (auth_repo.AdminMutationConflictError, BadRequestError),
        ],
    )
    async def test_repo_exceptions(
        self, test_admin, test_user, monkeypatch, exc, expected
    ):
        async def raiser(**kwargs):
            raise exc

        monkeypatch.setattr(auth_repo, "update_user_as_admin", raiser)
        with pytest.raises(expected):
            await svc.update_user(
                actor=_admin(test_admin["id"]),
                user_id=test_user["id"],
                payload=UserUpdate(quota=2048),
                request_id="r",
            )

    @pytest.mark.asyncio
    async def test_quota_below_usage(self, test_admin, test_user, monkeypatch):
        async def raiser(**kwargs):
            raise auth_repo.QuotaBelowUsageError(100, 1)

        monkeypatch.setattr(auth_repo, "update_user_as_admin", raiser)
        with pytest.raises(BadRequestError):
            await svc.update_user(
                actor=_admin(test_admin["id"]),
                user_id=test_user["id"],
                payload=UserUpdate(quota=1),
                request_id="r",
            )

    @pytest.mark.asyncio
    async def test_updated_none(self, test_admin, test_user, monkeypatch):
        async def none_updated(**kwargs):
            return None

        monkeypatch.setattr(auth_repo, "update_user_as_admin", none_updated)
        with pytest.raises(NotFoundError):
            await svc.update_user(
                actor=_admin(test_admin["id"]),
                user_id=test_user["id"],
                payload=UserUpdate(quota=2048),
                request_id="r",
            )


class TestRpcAccess:
    @pytest.mark.asyncio
    async def test_get_rpc_access_not_found(self, temp_db):
        with pytest.raises(NotFoundError):
            await svc.get_rpc_access(999)

    @pytest.mark.asyncio
    async def test_issue_secret_loop_exhausted(self, test_user, monkeypatch):
        async def dup(*args, **kwargs):
            raise auth_repo.DuplicateCredentialError

        monkeypatch.setattr(auth_repo, "set_rpc_secret", dup)
        with pytest.raises(BadRequestError):
            await svc.set_rpc_access(
                user_id=test_user["id"], enabled=True, request_id="r"
            )

    @pytest.mark.asyncio
    async def test_issue_secret_user_deleted_midway(self, test_user, monkeypatch):
        async def not_updated(*args, **kwargs):
            return None

        async def none_user(user_id):
            return None

        monkeypatch.setattr(auth_repo, "set_rpc_secret", not_updated)
        monkeypatch.setattr(auth_repo, "get_user_by_id", none_user)
        with pytest.raises(NotFoundError):
            await svc.set_rpc_access(
                user_id=test_user["id"], enabled=True, request_id="r"
            )

    @pytest.mark.asyncio
    async def test_issue_secret_require_enabled(self, test_user, monkeypatch):
        async def not_updated(*args, **kwargs):
            return None

        monkeypatch.setattr(auth_repo, "set_rpc_secret", not_updated)
        with pytest.raises(BadRequestError):
            await svc.refresh_rpc_secret(user_id=test_user["id"], request_id="r")

    @pytest.mark.asyncio
    async def test_set_rpc_access_disable_not_found(self, test_user, monkeypatch):
        async def not_updated(*args, **kwargs):
            return None

        monkeypatch.setattr(auth_repo, "set_rpc_secret", not_updated)
        with pytest.raises(NotFoundError):
            await svc.set_rpc_access(
                user_id=test_user["id"], enabled=False, request_id="r"
            )

    @pytest.mark.asyncio
    async def test_refresh_rpc_secret_not_enabled(self, test_user):
        with pytest.raises(BadRequestError):
            await svc.refresh_rpc_secret(user_id=test_user["id"], request_id="r")

    @pytest.mark.asyncio
    async def test_refresh_rpc_secret_not_found(self, temp_db):
        with pytest.raises(NotFoundError):
            await svc.refresh_rpc_secret(user_id=999, request_id="r")

    @pytest.mark.asyncio
    async def test_enable_and_get_and_refresh(self, test_user):
        issued = await svc.set_rpc_access(
            user_id=test_user["id"], enabled=True, request_id="r"
        )
        assert issued.secret.startswith("aria2_")
        status = await svc.get_rpc_access(test_user["id"])
        assert status.enabled is True
        assert status.secret == issued.secret
        refreshed = await svc.refresh_rpc_secret(user_id=test_user["id"], request_id="r")
        assert refreshed.secret != issued.secret
        disabled = await svc.set_rpc_access(
            user_id=test_user["id"], enabled=False, request_id="r"
        )
        assert disabled.enabled is False


class TestRemainingBranches:
    @pytest.mark.asyncio
    async def test_create_requires_admin_when_users_exist(self, test_admin):
        payload = UserCreate(username="u9", password="pass1234", is_admin=False)
        with pytest.raises(ForbiddenError):
            await svc.create_user(
                payload=payload, client_ip="127.0.0.1", request_id="r", admin=None
            )

    @pytest.mark.asyncio
    async def test_create_user_with_admin_success(self, test_admin):
        payload = UserCreate(username="u10", password="pass1234", is_admin=False)
        user = await svc.create_user(
            payload=payload,
            client_ip="127.0.0.1",
            request_id="r",
            admin=_admin(test_admin["id"]),
        )
        assert user["username"] == "u10"

    @pytest.mark.asyncio
    async def test_update_username_with_password(self, test_admin, test_user):
        result = await svc.update_user(
            actor=_admin(test_admin["id"]),
            user_id=test_user["id"],
            payload=UserUpdate(username="renamed", password="pass1234"),
            request_id="r",
        )
        assert result["username"] == "renamed"

    @pytest.mark.asyncio
    async def test_update_promote_admin(self, test_admin, test_user):
        result = await svc.update_user(
            actor=_admin(test_admin["id"]),
            user_id=test_user["id"],
            payload=UserUpdate(is_admin=True),
            request_id="r",
        )
        assert result["is_admin"] is True

    @pytest.mark.asyncio
    async def test_issue_secret_require_enabled_not_enabled(self, test_user, monkeypatch):
        async def not_updated(*args, **kwargs):
            return None

        monkeypatch.setattr(auth_repo, "set_rpc_secret", not_updated)
        with pytest.raises(BadRequestError):
            await svc.set_rpc_access(
                user_id=test_user["id"], enabled=True, request_id="r"
            )
