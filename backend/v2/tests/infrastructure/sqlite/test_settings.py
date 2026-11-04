from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import delete, text, update

from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.domain.settings import SystemSettings
from aria2deck.infrastructure.security import CredentialManager
from aria2deck.infrastructure.sqlite.bootstrap import initialize_database
from aria2deck.infrastructure.sqlite.engine import SQLiteDatabase
from aria2deck.infrastructure.sqlite.repositories.settings import SettingsRepository
from aria2deck.infrastructure.sqlite.schema import app_settings
from aria2deck.infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

RPC_VALUE = "test-rpc-value"


def repository(uow: SQLiteUnitOfWork) -> SettingsRepository:
    return SettingsRepository(uow.connection, CredentialManager(pepper=None, debug=True))


async def test_settings_repository_encrypts_and_restores_all_values(
    database: SQLiteDatabase,
) -> None:
    expected = replace(
        SystemSettings(),
        site_title="V2",
        aria2_rpc_secret=RPC_VALUE,
        hidden_file_extensions=(".tmp",),
        tracker_fixed_list=("udp://tracker.example",),
        download_connection_limits={"http": 4},
        updated_at_ms=123,
    )
    async with SQLiteUnitOfWork(database) as uow:
        await repository(uow).save_settings(expected)
    async with SQLiteUnitOfWork(database) as uow:
        assert await repository(uow).get_settings() == expected
    async with database.connection() as connection:
        ciphertext = (
            await connection.execute(text("SELECT aria2_rpc_secret_ciphertext FROM app_settings"))
        ).scalar_one()
    assert RPC_VALUE not in ciphertext


async def test_settings_repository_rejects_missing_singleton(database: SQLiteDatabase) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(delete(app_settings))
        with pytest.raises(DomainError) as caught:
            await repository(uow).get_settings()
        assert caught.value.code is ErrorCode.INVALID_STATE
        with pytest.raises(DomainError) as caught:
            await repository(uow).save_settings(SystemSettings())
        assert caught.value.code is ErrorCode.INVALID_STATE


@pytest.mark.parametrize(
    "column,value",
    [
        ("hidden_file_extensions_json", "{}"),
        ("tracker_fixed_list_json", '["valid", 1]'),
        ("download_connection_limits_json", "[]"),
        ("download_connection_limits_json", '{"http": "4"}'),
    ],
)
async def test_settings_repository_rejects_corrupt_json_shapes(
    database: SQLiteDatabase, column: str, value: str
) -> None:
    async with SQLiteUnitOfWork(database) as uow:
        await uow.connection.execute(update(app_settings).values(**{column: value}))
        with pytest.raises(DomainError) as caught:
            await repository(uow).get_settings()
        assert caught.value.code is ErrorCode.INVALID_STATE


async def test_new_database_initial_settings_require_an_encryptor(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "new.db")
    try:
        with pytest.raises(DomainError) as caught:
            await initialize_database(database, initial_settings=SystemSettings())
        assert caught.value.code is ErrorCode.CONFIGURATION_INVALID
    finally:
        await database.close()


async def test_new_database_initial_settings_are_seeded_atomically(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "seeded.db")
    expected = SystemSettings(
        aria2_rpc_url="http://aria2:6800/jsonrpc", aria2_rpc_secret=RPC_VALUE
    )
    credentials = CredentialManager(pepper=None, debug=True)
    try:
        await initialize_database(
            database, initial_settings=expected, credentials=credentials
        )
        async with SQLiteUnitOfWork(database) as uow:
            actual = await SettingsRepository(uow.connection, credentials).get_settings()
        assert actual == expected
    finally:
        await database.close()
