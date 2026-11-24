from __future__ import annotations

import asyncio
from pathlib import Path
from types import TracebackType

import pytest
from fastapi import Request
from sqlalchemy import text

from aria2deck.api.contracts import SettingsUpdateRequest
from aria2deck.api.settings import public_site_info, update_settings
from aria2deck.application.accounts.models import Principal
from aria2deck.application.accounts.service import AccountService
from aria2deck.domain.accounts import CredentialKind
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.settings import SystemSettings
from aria2deck.infrastructure.sqlite.repositories.accounts import AccountRepository
from aria2deck.infrastructure.sqlite.repositories.settings import SettingsRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork
from tests.application.test_account_cleanup import (
    OTHER_PASSWORD,
    PASSWORD,
    THIRD_PASSWORD,
    create_admin,
    make_service,
)


async def login_session(
    service: AccountService, username: str, password: str
) -> tuple[Principal, str]:
    result = await service.login(username=username, password_hash=password)
    return await service.authenticate_session(result.session_id), result.session_id


async def login_principal(service: AccountService, username: str, password: str) -> Principal:
    principal, _raw_session = await login_session(service, username, password)
    return principal


async def test_session_login_logout_and_generation_validation(tmp_path: Path) -> None:
    _app, service = await make_service(tmp_path)
    admin = await create_admin(service)
    users, total = await service.list_users(admin, page=1, page_size=20)
    assert total == 1
    assert users[0].user.id == admin.user.id
    admin_token = await service.create_api_token(admin, name="admin-token")
    with pytest.raises(DomainError):
        await service.authenticate_api_token(admin_token.raw_token)

    for username, password in (("missing", PASSWORD), ("admin", OTHER_PASSWORD)):
        with pytest.raises(DomainError) as caught:
            await service.login(username=username, password_hash=password)
        assert caught.value.code is ErrorCode.UNAUTHENTICATED
    with pytest.raises(DomainError):
        await service.authenticate_session("missing")

    authenticated, authenticated_raw = await login_session(service, "admin", PASSWORD)
    assert authenticated.user.id == admin.user.id
    assert authenticated.session_id is not None
    await service.logout(authenticated)
    with pytest.raises(DomainError):
        await service.authenticate_session(authenticated_raw)

    fresh, fresh_raw = await login_session(service, "admin", PASSWORD)
    changed_session = await service.change_password(
        fresh, old_password_hash=PASSWORD, new_password_hash=OTHER_PASSWORD
    )
    with pytest.raises(DomainError):
        await service.authenticate_session(fresh_raw)
    rotated = await service.authenticate_session(changed_session)
    assert rotated.user.credential_generation == fresh.user.credential_generation + 1


async def test_expired_session_and_cas_failure_leave_recoverable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, service = await make_service(tmp_path)
    await create_admin(service)
    principal, raw_session = await login_session(service, "admin", PASSWORD)

    async def reject_change(*args: object, **kwargs: object) -> bool:
        return False

    monkeypatch.setattr(AccountRepository, "change_password", reject_change)
    with pytest.raises(DomainError) as caught:
        await service.change_password(
            principal, old_password_hash=PASSWORD, new_password_hash=OTHER_PASSWORD
        )
    assert caught.value.code is ErrorCode.INVALID_STATE
    assert (await service.authenticate_session(raw_session)).user.id == principal.user.id

    async with service.uow_factory() as uow:
        await uow.connection.execute(
            text("UPDATE sessions SET expires_at_ms = created_at_ms + 1")
        )
    with pytest.raises(DomainError):
        await service.authenticate_session(raw_session)


async def test_user_update_rejects_pending_invalid_hash_and_cas_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, service = await make_service(tmp_path)
    admin = await create_admin(service)
    user = await service.create_user(
        admin,
        username="user",
        password_hash=OTHER_PASSWORD,
        is_admin=False,
        is_initial_password=False,
        quota_bytes=100,
    )
    with pytest.raises(DomainError) as caught:
        await service.create_user(
            None,
            username="anonymous",
            password_hash=PASSWORD,
            is_admin=False,
            is_initial_password=False,
            quota_bytes=None,
        )
    assert caught.value.code is ErrorCode.UNAUTHENTICATED

    with pytest.raises(DomainError) as caught:
        await service.update_user(admin, user_id=user.user.id, changes={"password_hash": "bad"})
    assert caught.value.code is ErrorCode.INVALID_INPUT

    async def reject_change(*args: object, **kwargs: object) -> bool:
        return False

    monkeypatch.setattr(AccountRepository, "change_password", reject_change)
    with pytest.raises(DomainError) as caught:
        await service.update_user(
            admin,
            user_id=user.user.id,
            changes={
                "username": "renamed",
                "password_hash": THIRD_PASSWORD,
                "quota_bytes": 50,
            },
        )
    assert caught.value.code is ErrorCode.INVALID_STATE
    unchanged = await service.get_user(admin, user.user.id)
    assert unchanged.user.username == "user"
    assert unchanged.user.quota_bytes == 100

    await service.delete_user(admin, user_id=user.user.id)
    with pytest.raises(DomainError) as caught:
        await service.update_user(admin, user_id=user.user.id, changes={"quota_bytes": 10})
    assert caught.value.code is ErrorCode.INVALID_STATE


async def test_user_update_empty_and_password_success_paths(tmp_path: Path) -> None:
    _app, service = await make_service(tmp_path)
    admin = await create_admin(service)
    user = await service.create_user(
        admin,
        username="user",
        password_hash=OTHER_PASSWORD,
        is_admin=False,
        is_initial_password=False,
        quota_bytes=100,
    )

    with pytest.raises(DomainError) as caught:
        await service.update_user(
            admin, user_id=user.user.id, changes={"is_initial_password": True}
        )
    assert caught.value.code is ErrorCode.INVALID_INPUT
    unchanged = await service.update_user(admin, user_id=user.user.id, changes={})
    assert unchanged.user.username == "user"
    updated = await service.update_user(
        admin,
        user_id=user.user.id,
        changes={"password_hash": THIRD_PASSWORD, "is_initial_password": False},
    )
    assert updated.user.credential_generation == user.user.credential_generation + 1
    assert updated.user.is_initial_password is False


async def test_token_and_rpc_service_paths_use_current_user_state(tmp_path: Path) -> None:
    _app, service = await make_service(tmp_path)
    admin = await create_admin(service)
    user_view = await service.create_user(
        admin,
        username="user",
        password_hash=OTHER_PASSWORD,
        is_admin=False,
        is_initial_password=False,
        quota_bytes=100,
    )
    user = Principal(user_view.user, CredentialKind.SESSION)

    created = await service.create_api_token(user, name="CLI")
    assert created.raw_token.startswith("aria2_")
    assert (await service.authenticate_api_token(created.raw_token)).user.id == user.user.id
    with pytest.raises(DomainError):
        await service.authenticate_api_token("invalid")
    tokens, total = await service.list_api_tokens(user, page=1, page_size=1)
    assert total == 1
    assert tokens[0].last_used_at_ms is not None
    beyond, total = await service.list_api_tokens(user, page=2, page_size=1)
    assert beyond == []
    assert total == 1
    await service.delete_api_token(user, token_id=created.token.id)
    with pytest.raises(DomainError):
        await service.authenticate_api_token(created.raw_token)

    assert (await service.rpc_access(user)).enabled is False
    enabled = await service.set_rpc_access(user, enabled=True)
    current = await current_principal(service, user.user.id)
    assert (await service.rpc_access(current)).secret == enabled.secret
    assert (await service.set_rpc_access(current, enabled=True)).secret == enabled.secret
    rotated = await service.refresh_rpc_access(current)
    assert rotated.secret != enabled.secret
    current = await current_principal(service, user.user.id)
    assert (await service.set_rpc_access(current, enabled=False)).secret is None
    with pytest.raises(DomainError):
        await service.refresh_rpc_access(user)

    await service.create_api_token(user, name="to-invalidate")
    await service.set_rpc_access(user, enabled=True)
    assert await service.invalidate_credentials(
        admin, confirmation="INVALIDATE_ALL_CREDENTIALS"
    ) == (1, 1)


async def current_principal(service: AccountService, user_id: int) -> Principal:
    async with service.uow_factory() as uow:
        user = await service.repository_factory(uow).get_user(user_id)
    return Principal(user, CredentialKind.SESSION)


async def test_settings_service_requires_initialization_then_updates_atomically(
    tmp_path: Path,
) -> None:
    app, account_service = await make_service(tmp_path)
    service = app.state.settings_service
    with pytest.raises(DomainError) as caught:
        service.current()
    assert caught.value.code is ErrorCode.INVALID_STATE

    await service.initialize()
    admin = await create_admin(account_service)
    assert service.public_site_title() == "Aria2Deck"
    assert service.read(admin).site_title == "Aria2Deck"
    updated = await service.update(admin, {"site_title": "V2"})
    assert updated.site_title == "V2"
    assert service.current() == updated
    response = await update_settings(
        SettingsUpdateRequest(site_title="Adapter"), principal=admin, service=service
    )
    assert response.site_title == "Adapter"
    public = await public_site_info(
        request=Request({"type": "http", "client": ("192.0.2.1", 1234)}),
        service=service,
        limiter=app.state.rate_limiter,
    )
    assert public.site_title == "Adapter"



async def test_settings_update_lock_covers_commit_and_memory_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, account_service = await make_service(tmp_path)
    service = app.state.settings_service
    await service.initialize()
    admin = await create_admin(account_service)

    original_exit = SQLiteUnitOfWork.__aexit__
    original_save = SettingsRepository.save_settings
    first_committed = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    committed_updates = 0

    async def controlled_exit(
        self: SQLiteUnitOfWork,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        nonlocal committed_updates
        await original_exit(self, exception_type, exception, traceback)
        if exception_type is None:
            committed_updates += 1
            if committed_updates == 1:
                first_committed.set()
                await release_first.wait()

    async def observe_save(self: SettingsRepository, updated: SystemSettings) -> None:
        if updated.max_task_size == 12345:
            second_started.set()
        await original_save(self, updated)

    monkeypatch.setattr(SQLiteUnitOfWork, "__aexit__", controlled_exit)
    monkeypatch.setattr(SettingsRepository, "save_settings", observe_save)

    first = asyncio.create_task(service.update(admin, {"site_title": "First"}))
    await first_committed.wait()
    second = asyncio.create_task(service.update(admin, {"max_task_size": 12345}))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(second_started.wait(), timeout=0.1)

    assert not second_started.is_set()
    assert service.current().site_title == "Aria2Deck"
    release_first.set()
    await asyncio.gather(first, second)

    assert second_started.is_set()
    assert service.current().site_title == "First"
    assert service.current().max_task_size == 12345
    async with service.uow_factory() as uow:
        stored = await service.repository_factory(uow).get_settings()
    assert stored == service.current()



async def test_initial_admin_rule_covers_required_optional_short_and_existing_users(
    tmp_path: Path,
) -> None:
    _app, service = await make_service(tmp_path)

    with pytest.raises(DomainError) as missing:
        await service.ensure_initial_admin(password=None, required=True)
    assert missing.value.code is ErrorCode.CONFIGURATION_INVALID
    assert await service.ensure_initial_admin(password=None, required=False) is False
    with pytest.raises(DomainError) as short:
        await service.ensure_initial_admin(password="short", required=False)  # noqa: S106  # 短密码边界
    assert short.value.code is ErrorCode.CONFIGURATION_INVALID

    assert await service.ensure_initial_admin(password=PASSWORD, required=True) is True
    assert await service.ensure_initial_admin(password=OTHER_PASSWORD, required=True) is False
    derived = service.credentials.derive_password_hash(username="admin", password=PASSWORD)
    logged_in = await service.login(username="admin", password_hash=derived)
    assert logged_in.view.user.is_admin is True
    assert logged_in.view.user.is_initial_password is True
