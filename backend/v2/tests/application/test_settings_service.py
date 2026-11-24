from __future__ import annotations

import pytest

from aria2deck.application.accounts.models import Principal
from aria2deck.application.settings.service import (
    Aria2TestResult,
    SettingsService,
)
from aria2deck.domain.accounts import CredentialKind, User
from aria2deck.domain.errors import DomainError, ErrorCode


class FakeVersionPort:
    def __init__(self, outcome: tuple[str, list[str]] | Exception) -> None:
        self.outcome = outcome
        self.calls = 0

    async def version(self) -> tuple[str, list[str]]:
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def make_service(port: FakeVersionPort | None) -> SettingsService:
    def uow_factory() -> object:
        raise AssertionError("连接测试不得触碰数据库")

    def repository_factory(uow: object) -> object:
        raise AssertionError("连接测试不得触碰数据库")

    return SettingsService(
        uow_factory=uow_factory,
        repository_factory=repository_factory,
        clock_ms=lambda: 0,
        aria2_version=port,
    )


def admin_principal() -> Principal:
    return Principal(
        User(1, "admin", "admin", "ab" * 32, True, 1000, False, False, 1, 1, 1),
        CredentialKind.SESSION,
        "session",
    )


async def test_aria2_success_returns_version_and_features() -> None:
    port = FakeVersionPort(("1.37.0", ["HTTPS", "BitTorrent"]))
    result = await make_service(port).test_aria2(admin_principal())
    assert result == Aria2TestResult(
        connected=True, version="1.37.0", features=("HTTPS", "BitTorrent")
    )
    assert port.calls == 1


@pytest.mark.parametrize(
    "failure",
    [
        DomainError(ErrorCode.SUBMISSION_UNCERTAIN, "aria2 提交结果无法确认"),
        TimeoutError("timed out"),
        OSError("network unreachable"),
    ],
)
async def test_aria2_connection_failure_reports_disconnected_without_raising(
    failure: Exception,
) -> None:
    port = FakeVersionPort(failure)
    result = await make_service(port).test_aria2(admin_principal())
    assert result == Aria2TestResult(connected=False, version=None, features=())


@pytest.mark.parametrize("kind", [CredentialKind.API_TOKEN, CredentialKind.RPC_SECRET])
async def test_aria2_rejects_non_session_credentials(kind: CredentialKind) -> None:
    principal = Principal(
        User(1, "admin", "admin", "ab" * 32, True, 1000, False, False, 1, 1, 1),
        kind,
        None,
    )
    port = FakeVersionPort(("1.37.0", []))
    with pytest.raises(DomainError) as caught:
        await make_service(port).test_aria2(principal)
    assert caught.value.code is ErrorCode.PERMISSION_DENIED
    assert port.calls == 0


async def test_aria2_rejects_non_admin_sessions() -> None:
    principal = Principal(
        User(2, "user", "user", "cd" * 32, False, 1000, False, False, 1, 1, 1),
        CredentialKind.SESSION,
        "session",
    )
    port = FakeVersionPort(("1.37.0", []))
    with pytest.raises(DomainError) as caught:
        await make_service(port).test_aria2(principal)
    assert caught.value.code is ErrorCode.PERMISSION_DENIED
    assert port.calls == 0


async def test_aria2_without_port_is_invalid_state() -> None:
    with pytest.raises(DomainError) as caught:
        await make_service(None).test_aria2(admin_principal())
    assert caught.value.code is ErrorCode.INVALID_STATE
