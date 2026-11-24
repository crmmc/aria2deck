from __future__ import annotations

import pytest

from aria2deck.domain.accounts import (
    DEFAULT_QUOTA_BYTES,
    CredentialKind,
    Permission,
    Role,
    api_token_allowed,
    authorize,
    authorize_admin_session,
    can_finalize_user_deletion,
    ensure_quota_change,
    ensure_user_delete,
    ensure_user_role_change,
    ensure_username_change,
    normalize_username,
    require_credential_invalidation,
    require_password_generation,
    require_session,
    session_is_active,
    validate_api_token_name,
    validate_new_user,
    validate_password_change,
    validate_password_hash,
)
from aria2deck.domain.errors import DomainError, ErrorCode


def test_first_user_must_be_admin_and_default_quota_is_100_gib() -> None:
    user = validate_new_user(" Alice ", quota_bytes=None, is_admin=True, existing_users=0)
    assert user.username == "Alice"
    assert user.username_key == "alice"
    assert user.quota_bytes == DEFAULT_QUOTA_BYTES == 100 * 1024**3
    assert user.is_admin
    with pytest.raises(DomainError):
        validate_new_user("Alice", None, False, 0)
    assert not validate_new_user("Bob", 1, False, 1).is_admin


@pytest.mark.parametrize("username", ["", " ", "a" * 65, "a/b", "a\x00b"])
def test_username_validation(username: str) -> None:
    with pytest.raises(DomainError):
        validate_new_user(username, None, True, 0)


def test_user_quota_must_be_nonnegative() -> None:
    with pytest.raises(DomainError):
        validate_new_user("Alice", -1, True, 0)


@pytest.mark.parametrize(
    "role,permission,allowed",
    [
        (Role.ADMIN, Permission.MANAGE_USERS, True),
        (Role.ADMIN, Permission.MANAGE_SETTINGS, True),
        (Role.ADMIN, Permission.MANAGE_STORAGE, True),
        (Role.USER, Permission.ACCESS_OWN_DATA, True),
        (Role.USER, Permission.MANAGE_USERS, False),
        (Role.USER, Permission.INVALIDATE_CREDENTIALS, False),
    ],
)
def test_permissions_are_explicit(role: Role, permission: Permission, allowed: bool) -> None:
    if allowed:
        authorize(role, permission)
    else:
        with pytest.raises(DomainError) as caught:
            authorize(role, permission)
        assert caught.value.code is ErrorCode.PERMISSION_DENIED


def test_user_delete_requires_admin_and_never_deletes_self() -> None:
    ensure_user_delete(actor_id=1, target_id=2, actor_role=Role.ADMIN)
    with pytest.raises(DomainError) as caught:
        ensure_user_delete(actor_id=1, target_id=1, actor_role=Role.ADMIN)
    assert caught.value.code is ErrorCode.SELF_DELETE
    with pytest.raises(DomainError):
        ensure_user_delete(actor_id=1, target_id=2, actor_role=Role.USER)


def test_last_admin_and_self_demotion_are_protected() -> None:
    ensure_user_role_change(
        actor_id=1,
        target_id=2,
        actor_role=Role.ADMIN,
        current_is_admin=False,
        new_is_admin=True,
        admin_count=1,
    )
    with pytest.raises(DomainError) as caught:
        ensure_user_role_change(actor_id=1, target_id=1, actor_role=Role.ADMIN, current_is_admin=True, new_is_admin=False, admin_count=2)
    assert caught.value.code is ErrorCode.PERMISSION_DENIED
    with pytest.raises(DomainError) as caught:
        ensure_user_role_change(actor_id=1, target_id=2, actor_role=Role.ADMIN, current_is_admin=True, new_is_admin=False, admin_count=1)
    assert caught.value.code is ErrorCode.LAST_ADMIN
    ensure_user_role_change(actor_id=1, target_id=2, actor_role=Role.ADMIN, current_is_admin=True, new_is_admin=True, admin_count=1)


def test_async_user_delete_only_finalizes_after_all_owned_work_is_gone() -> None:
    can_finalize_user_deletion(active_tasks=0, file_references=0, active_packs=0)
    for values in [(1, 0, 0), (0, 1, 0), (0, 0, 1), (-1, 0, 0)]:
        with pytest.raises(DomainError):
            can_finalize_user_deletion(active_tasks=values[0], file_references=values[1], active_packs=values[2])


@pytest.mark.parametrize("admin,pending,allowed", [(False, False, True), (True, False, False), (False, True, False)])
def test_api_token_cannot_be_used_by_admin_or_pending_user(
    admin: bool, pending: bool, allowed: bool
) -> None:
    assert api_token_allowed(is_admin=admin, pending_delete=pending) is allowed


def test_session_expiry_revocation_and_user_deletion_are_unambiguous() -> None:
    assert session_is_active(expires_at_ms=100, now_ms=99, revoked_at_ms=None, pending_delete=False)
    assert not session_is_active(expires_at_ms=100, now_ms=100, revoked_at_ms=None, pending_delete=False)
    assert not session_is_active(expires_at_ms=100, now_ms=1, revoked_at_ms=50, pending_delete=False)
    assert not session_is_active(expires_at_ms=100, now_ms=1, revoked_at_ms=None, pending_delete=True)


def test_global_credential_invalidation_requires_exact_confirmation() -> None:
    require_credential_invalidation("INVALIDATE_ALL_CREDENTIALS")
    with pytest.raises(DomainError) as caught:
        require_credential_invalidation("yes")
    assert caught.value.code is ErrorCode.INVALID_INPUT


def test_password_change_and_concurrent_generation_rules() -> None:
    new_hash = "ab" * 32
    invalid_hash = "bad"
    validate_password_change(is_initial=True, old_password_matches=False, new_password_hash=new_hash)
    validate_password_change(is_initial=False, old_password_matches=True, new_password_hash=new_hash)
    with pytest.raises(DomainError):
        validate_password_change(is_initial=False, old_password_matches=False, new_password_hash=new_hash)
    with pytest.raises(DomainError):
        validate_password_change(is_initial=True, old_password_matches=False, new_password_hash=invalid_hash)
    require_password_generation(expected=2, current=2)
    with pytest.raises(DomainError) as caught:
        require_password_generation(expected=2, current=3)
    assert caught.value.code is ErrorCode.INVALID_STATE


def test_account_counters_reject_impossible_values() -> None:
    with pytest.raises(DomainError):
        validate_new_user("Alice", None, True, -1)
    with pytest.raises(DomainError) as caught:
        ensure_user_role_change(actor_id=1, target_id=2, actor_role=Role.ADMIN, current_is_admin=True, new_is_admin=False, admin_count=0)
    assert caught.value.code is ErrorCode.INVALID_STATE


def test_last_admin_cannot_be_deleted() -> None:
    with pytest.raises(DomainError) as caught:
        ensure_user_delete(
            actor_id=1,
            target_id=2,
            actor_role=Role.ADMIN,
            target_is_admin=True,
            admin_count=1,
        )
    assert caught.value.code is ErrorCode.LAST_ADMIN


def test_username_normalization_rejects_path_separators() -> None:
    assert normalize_username("  Alice  ") == ("Alice", "alice")
    for username in ("", " ", "a" * 65, "a/b", "a" + chr(92) + "b", "a" + chr(0) + "b"):
        with pytest.raises(DomainError):
            normalize_username(username)


def test_quota_change_allows_exact_usage_and_rejects_overcommit() -> None:
    ensure_quota_change(quota_bytes=75, used_bytes=50, reserved_bytes=25)
    with pytest.raises(DomainError) as caught:
        ensure_quota_change(quota_bytes=74, used_bytes=50, reserved_bytes=25)
    assert caught.value.code is ErrorCode.QUOTA_EXCEEDED
    with pytest.raises(DomainError):
        ensure_quota_change(quota_bytes=-1, used_bytes=0, reserved_bytes=0)


def test_username_change_requires_the_derived_password() -> None:
    ensure_username_change(username_changed=False, password_hash_supplied=False)
    ensure_username_change(username_changed=True, password_hash_supplied=True)
    with pytest.raises(DomainError) as caught:
        ensure_username_change(username_changed=True, password_hash_supplied=False)
    assert caught.value.code is ErrorCode.INVALID_INPUT


def test_password_hash_and_token_name_validation() -> None:
    validate_password_hash("AB" * 32)
    with pytest.raises(DomainError):
        validate_password_hash("bad")
    assert validate_api_token_name("  CLI  ") == "CLI"
    assert validate_api_token_name(" ") == "默认 Token"
    with pytest.raises(DomainError):
        validate_api_token_name("x" * 101)
    with pytest.raises(DomainError):
        validate_api_token_name("bad" + chr(0))


def test_admin_operations_require_a_session_even_with_an_admin_token() -> None:
    require_session(CredentialKind.SESSION)
    authorize_admin_session(Role.ADMIN, CredentialKind.SESSION, Permission.MANAGE_USERS)
    with pytest.raises(DomainError) as caught:
        require_session(CredentialKind.API_TOKEN)
    assert caught.value.code is ErrorCode.PERMISSION_DENIED
    with pytest.raises(DomainError):
        authorize_admin_session(Role.ADMIN, CredentialKind.API_TOKEN, Permission.MANAGE_USERS)
    with pytest.raises(DomainError):
        authorize_admin_session(Role.USER, CredentialKind.SESSION, Permission.MANAGE_USERS)
