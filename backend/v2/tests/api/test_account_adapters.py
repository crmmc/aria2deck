from __future__ import annotations

from http.cookies import SimpleCookie
from pathlib import Path

from fastapi import Request, Response

from aria2deck.api import accounts
from aria2deck.api.contracts import (
    ApiTokenCreateRequest,
    CredentialInvalidateRequest,
    LoginRequest,
    PasswordChangeRequest,
    UserCreateRequest,
)
from aria2deck.api.dependencies import SESSION_COOKIE, optional_principal
from aria2deck.application.accounts.models import Principal
from aria2deck.domain.accounts import CredentialKind
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
    deleted = await accounts.delete_user(user.id, principal=principal, service=service)
    assert deleted.accepted is True

    created = await accounts.create_api_token(
        ApiTokenCreateRequest(name="CLI"), principal=principal, service=service
    )
    assert created.token.startswith("aria2_")
    tokens = await accounts.list_api_tokens(
        page=1, page_size=20, principal=principal, service=service
    )
    assert tokens.total == 1
    assert (await accounts.delete_api_token(created.id, principal=principal, service=service)).ok
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



async def test_session_and_token_dependencies_share_the_authenticated_limiter(
    tmp_path: Path,
) -> None:
    app, service = await make_service(tmp_path)
    await app.state.settings_service.initialize()
    admin = await service.create_user(
        None,
        username="admin",
        password_hash=PASSWORD,
        is_admin=True,
        is_initial_password=False,
        quota_bytes=None,
    )
    login = await service.login(username="admin", password_hash=PASSWORD)
    session_request = Request(
        {
            "type": "http",
            "app": app,
            "method": "GET",
            "path": "/api/v2/auth/me",
            "headers": [(b"cookie", f"{SESSION_COOKIE}={login.session_id}".encode())],
        }
    )
    session_principal = await optional_principal(
        session_request, service=service, limiter=app.state.rate_limiter
    )
    assert session_principal is not None
    assert session_principal.user.id == admin.user.id

    user = await service.create_user(
        session_principal,
        username="user",
        password_hash=OTHER_PASSWORD,
        is_admin=False,
        is_initial_password=False,
        quota_bytes=100,
    )
    token = await service.create_api_token(
        Principal(user.user, CredentialKind.SESSION), name="CLI"
    )
    token_request = Request(
        {
            "type": "http",
            "app": app,
            "method": "GET",
            "path": "/api/v2/auth/me",
            "headers": [(b"authorization", f"Bearer {token.raw_token}".encode())],
        }
    )
    token_principal = await optional_principal(
        token_request, service=service, limiter=app.state.rate_limiter
    )
    assert token_principal is not None
    assert token_principal.user.id == user.user.id
