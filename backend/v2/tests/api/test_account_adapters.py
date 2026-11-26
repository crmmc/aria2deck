from __future__ import annotations

from http.cookies import SimpleCookie
from pathlib import Path

from fastapi import Request, Response

from aria2deck.api import accounts
from aria2deck.api.contracts import (
    CredentialInvalidateRequest,
    LoginRequest,
    PasswordChangeRequest,
    UserCreateRequest,
)
from aria2deck.domain.accounts import AccountStatus
from tests.application.test_account_cleanup import OTHER_PASSWORD, PASSWORD, make_service


def raw_session(response: Response) -> str:
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return cookie["aria2deck_v2_session"].value


async def test_account_adapters_map_real_service_results(tmp_path: Path) -> None:
    app, service = await make_service(tmp_path)
    app.state.cookie_secure = False
    await app.state.settings_service.initialize()
    request = Request(
        {"type": "http", "app": app, "method": "POST", "path": "/", "headers": []}
    )

    admin_response = await accounts.create_user(
        UserCreateRequest(username="admin", password=PASSWORD, is_admin=True),
        actor=None,
        service=service,
    )
    assert admin_response.is_admin is True

    login_response = Response()
    logged_in = await accounts.login(
        LoginRequest(username="admin", password=PASSWORD),
        request,
        login_response,
        service,
        limiter=app.state.rate_limiter,
    )
    assert logged_in.username == "admin"
    assert "HttpOnly" in login_response.headers["set-cookie"]
    principal = await service.authenticate_session(raw_session(login_response))

    user = await accounts.create_user(
        UserCreateRequest(username="user", password=OTHER_PASSWORD),
        actor=principal,
        service=service,
    )
    users = await accounts.list_users(page=1, page_size=20, principal=principal, service=service)
    assert users.total == 2
    assert users.items[1].status is AccountStatus.ACTIVE

    reset = await accounts.reset_user_password(user.id, principal=principal, service=service)
    assert reset.user.status is AccountStatus.ACTIVE
    assert reset.user.is_initial_password is True
    assert len(reset.temporary_password) == 16
    assert reset.temporary_password not in reset.model_dump_json(
        exclude={"temporary_password"}
    )
    deleted = await accounts.delete_user(user.id, principal=principal, service=service)
    assert deleted.accepted is True

    invalidated = await accounts.invalidate_credentials(
        CredentialInvalidateRequest(confirmation="INVALIDATE_ALL_CREDENTIALS"),
        principal=principal,
        service=service,
    )
    assert invalidated.ok is True

    changed_response = Response()
    changed = await accounts.change_password(
        PasswordChangeRequest(old_password=PASSWORD, new_password=OTHER_PASSWORD),
        request,
        changed_response,
        principal=principal,
        service=service,
    )
    assert changed.ok is True
    rotated = await service.authenticate_session(raw_session(changed_response))
    logout_response = Response()
    logged_out = await accounts.logout(logout_response, principal=rotated, service=service)
    assert logged_out.ok is True
    assert "Max-Age=0" in logout_response.headers["set-cookie"]



