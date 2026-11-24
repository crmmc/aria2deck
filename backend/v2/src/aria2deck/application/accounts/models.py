from __future__ import annotations

from dataclasses import dataclass

from ...domain.accounts import ApiToken, CredentialKind, User
from ...domain.quota import Quota


@dataclass(frozen=True, slots=True)
class Principal:
    user: User
    kind: CredentialKind
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class UserView:
    user: User
    quota: Quota


@dataclass(frozen=True, slots=True)
class LoginResult:
    view: UserView
    session_id: str


@dataclass(frozen=True, slots=True)
class CreatedApiToken:
    token: ApiToken
    raw_token: str


@dataclass(frozen=True, slots=True)
class RpcAccess:
    enabled: bool
    secret_prefix: str | None
    secret: str | None


@dataclass(frozen=True, slots=True)
class CleanupReport:
    deleted: int
    busy: int
    failed: int
