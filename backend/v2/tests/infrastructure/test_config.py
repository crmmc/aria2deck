from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aria2deck.bootstrap import create_app
from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.settings import SystemSettings
from aria2deck.infrastructure.config import Settings
from aria2deck.infrastructure.security import CredentialManager
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.settings import SettingsRepository
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

RPC_SECRET = "rpc-" + "test-" * 5


def environment(tmp_path: Path) -> dict[str, str]:
    return {
        "ARIA2DECK_DATABASE_PATH": str(tmp_path / "data" / "v2.db"),
        "ARIA2DECK_DOWNLOAD_DIR": str(tmp_path / "downloads"),
        "ARIA2DECK_STATIC_DIR": str(tmp_path / "static"),
        "ARIA2DECK_DEBUG": "true",
    }


def test_debug_configuration_is_independent_and_creates_only_v2_directories(tmp_path: Path) -> None:
    settings = Settings.from_env(environment(tmp_path))
    assert settings.database_path == tmp_path / "data" / "v2.db"
    assert settings.database_path.name == "v2.db"
    assert settings.debug
    settings.ensure_directories()
    assert settings.database_path.parent.is_dir()
    assert settings.download_directory.is_dir()
    assert not settings.static_directory.exists()


def test_production_configuration_requires_long_distinct_secrets(tmp_path: Path) -> None:
    env = environment(tmp_path) | {
        "ARIA2DECK_DEBUG": "false",
        "ARIA2DECK_SHARE_JWT_SECRET": "test-share-key-" + "s" * 24,
        "ARIA2DECK_CREDENTIAL_PEPPER": "test-pepper-key-" + "p" * 24,
        "ARIA2DECK_INITIAL_ADMIN_PASSWORD": "test-initial-admin-password",
    }
    settings = Settings.from_env(env)
    assert not settings.debug
    assert "test-share-key-" not in repr(settings)
    assert "test-pepper-key-" not in repr(settings)
    assert "test-initial" not in repr(settings)


@pytest.mark.parametrize("mode", ["missing", "short", "same"])
def test_production_configuration_fails_closed(tmp_path: Path, mode: str) -> None:
    env = environment(tmp_path) | {"ARIA2DECK_DEBUG": "false"}
    if mode != "missing":
        env["ARIA2DECK_SHARE_JWT_SECRET"] = (
            "short" if mode == "short" else "same-test-value-" + "x" * 20
        )
        env["ARIA2DECK_CREDENTIAL_PEPPER"] = (
            "also-short" if mode == "short" else env["ARIA2DECK_SHARE_JWT_SECRET"]
        )
    else:
        env["SHARE_JWT_SECRET"] = "legacy-secret-must-not-be-used" + "s" * 16
        env["CREDENTIAL_PEPPER"] = "legacy-pepper-must-not-be-used" + "p" * 16
    with pytest.raises(DomainError) as caught:
        Settings.from_env(env)
    assert caught.value.code is ErrorCode.CONFIGURATION_INVALID


def test_empty_database_path_is_rejected(tmp_path: Path) -> None:
    env = environment(tmp_path) | {"ARIA2DECK_DATABASE_PATH": " "}
    with pytest.raises(DomainError):
        Settings.from_env(env)


def test_default_runtime_paths_are_absolute_and_under_the_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    settings = Settings.from_env({"ARIA2DECK_DEBUG": "true"})

    expected_root = home / ".local" / "share" / "aria2deck"
    assert settings.database_path == expected_root / "v2.db"
    assert settings.download_directory == expected_root / "downloads"
    assert settings.database_path.is_absolute()
    assert settings.download_directory.is_absolute()


def test_configured_data_directory_supplies_defaults_and_ignores_legacy_aliases(
    tmp_path: Path,
) -> None:
    data_directory = tmp_path / "persistent"
    settings = Settings.from_env(
        {
            "ARIA2DECK_DEBUG": "true",
            "ARIA2DECK_DATA_DIR": str(data_directory),
            "ARIA2DECK_V2_DATABASE_PATH": str(tmp_path / "legacy.db"),
            "ARIA2C_DOWNLOAD_DIR": str(tmp_path / "legacy-downloads"),
        }
    )

    assert settings.database_path == data_directory / "v2.db"
    assert settings.download_directory == data_directory / "downloads"
    assert settings.initial_admin_password is None


def test_aria2_environment_is_read_without_exposing_the_secret(tmp_path: Path) -> None:
    settings = Settings.from_env(
        environment(tmp_path)
        | {
            "ARIA2DECK_ARIA2_RPC_URL": "http://aria2:6800/jsonrpc",
            "ARIA2DECK_ARIA2_RPC_SECRET": RPC_SECRET,
        }
    )

    assert settings.aria2_rpc_url == "http://aria2:6800/jsonrpc"
    assert settings.aria2_rpc_secret == RPC_SECRET
    assert RPC_SECRET not in repr(settings)


@pytest.mark.parametrize(
    "name,value",
    [
        ("ARIA2DECK_ARIA2_RPC_URL", "ftp://aria2/jsonrpc"),
        ("ARIA2DECK_ARIA2_RPC_URL", ""),
        ("ARIA2DECK_ARIA2_RPC_SECRET", ""),
        ("ARIA2DECK_ARIA2_RPC_SECRET", "x" * 4097),
        ("ARIA2DECK_DATA_DIR", " "),
    ],
)
def test_invalid_aria2_and_data_directory_configuration_fails_closed(
    tmp_path: Path, name: str, value: str
) -> None:
    with pytest.raises(DomainError) as caught:
        Settings.from_env(environment(tmp_path) | {name: value})
    assert caught.value.code is ErrorCode.CONFIGURATION_INVALID


def test_compose_aria2_configuration_seeds_and_is_read_from_a_new_database(
    tmp_path: Path,
) -> None:
    settings = Settings.from_env(
        environment(tmp_path)
        | {
            "ARIA2DECK_ARIA2_RPC_URL": "http://aria2:6800/jsonrpc",
            "ARIA2DECK_ARIA2_RPC_SECRET": RPC_SECRET,
        }
    )

    with TestClient(create_app(settings=settings)):
        pass

    async def read_persisted_settings() -> SystemSettings:
        database = SQLiteDatabase(settings.database_path)
        try:
            async with SQLiteUnitOfWork(database) as uow:
                repository = SettingsRepository(
                    uow.connection,
                    CredentialManager(
                        pepper=settings.credential_pepper, debug=settings.debug
                    ),
                )
                return await repository.get_settings()
        finally:
            await database.close()

    persisted = asyncio.run(read_persisted_settings())
    assert persisted.aria2_rpc_url == "http://aria2:6800/jsonrpc"
    assert persisted.aria2_rpc_secret == RPC_SECRET


@pytest.mark.parametrize(
    "value",
    ["http://127.0.0.1:bad", "ftp://localhost", "http://user@localhost", "http://localhost/?query=1"],
)
def test_internal_gateway_url_must_be_a_plain_http_url(value: str) -> None:
    with pytest.raises(DomainError) as caught:
        Settings.from_env({"ARIA2DECK_DEBUG": "true", "ARIA2DECK_INTERNAL_BASE_URL": value})
    assert caught.value.code is ErrorCode.CONFIGURATION_INVALID


def test_internal_gateway_url_is_configurable() -> None:
    settings = Settings.from_env(
        {"ARIA2DECK_DEBUG": "true", "ARIA2DECK_INTERNAL_BASE_URL": "http://app:8001/"}
    )
    assert settings.internal_base_url == "http://app:8001"
