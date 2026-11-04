from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from .api.accounts import router as accounts_router
from .api.downloads import router as downloads_router
from .api.errors import install_error_handlers
from .api.health import router as health_router
from .api.settings import router as settings_router
from .api.static_site import register_static_site
from .api.system import router as system_router
from .application.accounts.service import AccountService
from .application.downloads.cleanup import DownloadUserCleanup
from .application.downloads.commands import DownloadCommandService
from .application.downloads.gateway import DownloadGatewayService
from .application.downloads.history import HistoryService
from .application.downloads.lifecycle import DownloadLifecycleService
from .application.downloads.projection import TaskProjector
from .application.downloads.queries import DownloadQueryService
from .application.downloads.recovery import DownloadRecoveryService
from .application.library.cleanup import LibraryUserCleanup
from .application.packs.cleanup import PackUserCleanup
from .application.ports import UnitOfWork
from .application.settings.rate_limit import RateLimiter
from .application.settings.service import SettingsService
from .application.settings.trackers import TrackerService
from .application.system.connectivity import BackendConnectivity
from .application.system.health import HealthService
from .application.system.supervisor import WorkerSupervisor
from .domain.errors import DomainError
from .domain.settings import SystemSettings
from .infrastructure.aria2 import Aria2DownloadControl
from .infrastructure.aria2_submission import Aria2SubmissionClient
from .infrastructure.config import Settings
from .infrastructure.content_store import DownloadContentStore
from .infrastructure.http import SafeHttpClient
from .infrastructure.instance_lock import InstanceLock
from .infrastructure.observations import InMemoryDownloadObservations
from .infrastructure.pack_writer import PackWriterControl
from .infrastructure.security import CredentialManager
from .infrastructure.sqlite.bootstrap import initialize_database, validate_database
from .infrastructure.sqlite.engine import SQLiteDatabase
from .infrastructure.sqlite.repositories.accounts import AccountRepository
from .infrastructure.sqlite.repositories.content import ContentRepository
from .infrastructure.sqlite.repositories.downloads import DownloadRepository
from .infrastructure.sqlite.repositories.packs import PackRepository
from .infrastructure.sqlite.repositories.quota import QuotaRepository
from .infrastructure.sqlite.repositories.settings import SettingsRepository
from .infrastructure.sqlite.repositories.trackers import TrackerRepository
from .infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork


async def _database_is_ready(database: SQLiteDatabase) -> bool:
    try:
        await validate_database(database)
    except (DomainError, SQLAlchemyError):
        return False
    return True


def _clock_ms() -> int:
    return time.time_ns() // 1_000_000


def _directory_is_ready(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK)


def create_app(*, static_dir: Path | None = None, settings: Settings | None = None) -> FastAPI:
    config = settings or Settings.from_env()
    database = SQLiteDatabase(config.database_path)
    credentials = CredentialManager(pepper=config.credential_pepper, debug=config.debug)

    def uow_factory() -> SQLiteUnitOfWork:
        return SQLiteUnitOfWork(database)

    def account_repository(uow: UnitOfWork) -> AccountRepository:
        return AccountRepository(cast(AsyncConnection, uow.connection))

    def settings_repository(uow: UnitOfWork) -> SettingsRepository:
        return SettingsRepository(cast(AsyncConnection, uow.connection), credentials)

    def download_repository(uow: UnitOfWork) -> DownloadRepository:
        return DownloadRepository(cast(AsyncConnection, uow.connection))

    def tracker_repository(uow: UnitOfWork) -> TrackerRepository:
        return TrackerRepository(cast(AsyncConnection, uow.connection))

    def content_repository(uow: UnitOfWork) -> ContentRepository:
        return ContentRepository(cast(AsyncConnection, uow.connection))

    def pack_repository(uow: UnitOfWork) -> PackRepository:
        return PackRepository(cast(AsyncConnection, uow.connection))

    observations = InMemoryDownloadObservations()
    task_projector = TaskProjector(observations=observations, clock_ms=_clock_ms)
    download_queries = DownloadQueryService(
        uow_factory=uow_factory,
        repository_factory=download_repository,
        account_repository_factory=account_repository,
        projector=task_projector,
    )
    settings_service = SettingsService(
        uow_factory=uow_factory,
        repository_factory=settings_repository,
        clock_ms=_clock_ms,
    )
    download_cleanup = DownloadUserCleanup(
        uow_factory=uow_factory,
        repository_factory=download_repository,
        control=Aria2DownloadControl(settings=settings_service.current),
        clock_ms=_clock_ms,
    )
    pack_cleanup = PackUserCleanup(
        uow_factory=uow_factory,
        repository_factory=pack_repository,
        control=PackWriterControl(),
        clock_ms=_clock_ms,
    )
    library_cleanup = LibraryUserCleanup(
        uow_factory=uow_factory, repository_factory=content_repository, clock_ms=_clock_ms
    )
    account_service = AccountService(
        uow_factory=uow_factory,
        repository_factory=account_repository,
        credentials=credentials,
        clock_ms=_clock_ms,
        session_ttl_seconds=config.session_ttl_seconds,
        cleanup_ports=(download_cleanup, pack_cleanup, library_cleanup),
    )
    rate_limiter = RateLimiter(settings=settings_service.current, clock_ms=_clock_ms)
    safe_http = SafeHttpClient()
    tracker_service = TrackerService(
        uow_factory=uow_factory,
        repository_factory=tracker_repository,
        fetcher=safe_http,
        settings=settings_service.current,
        clock_ms=_clock_ms,
    )
    submission_backend = Aria2SubmissionClient(
        settings=settings_service.current,
        credentials=credentials,
        internal_base_url=config.internal_base_url,
        download_directory=config.download_directory,
    )
    download_commands = DownloadCommandService(
        uow_factory=uow_factory,
        repository_factory=download_repository,
        content_repository_factory=content_repository,
        quota_repository_factory=lambda uow: QuotaRepository(cast(AsyncConnection, uow.connection)),
        backend=submission_backend,
        trackers=tracker_service,
        rate_limiter=rate_limiter,
        clock_ms=_clock_ms,
        planned_gid=credentials.derive_download_gid,
    )
    download_history = HistoryService(
        uow_factory=uow_factory,
        repository_factory=download_repository,
        projector=task_projector,
        clock_ms=_clock_ms,
        retention_days=lambda: settings_service.current().history_retention_days,
    )
    connectivity = BackendConnectivity()
    download_lifecycle = DownloadLifecycleService(
        uow_factory=uow_factory,
        repository_factory=download_repository,
        content_repository_factory=content_repository,
        backend=submission_backend,
        content_store=DownloadContentStore(config.download_directory),
        observations=observations,
        connectivity=connectivity,
        clock_ms=_clock_ms,
    )
    download_recovery = DownloadRecoveryService(
        uow_factory=uow_factory,
        repository_factory=download_repository,
        backend=submission_backend,
        trackers=tracker_service,
        clock_ms=_clock_ms,
        lifecycle=download_lifecycle,
        quota_repository_factory=lambda uow: QuotaRepository(cast(AsyncConnection, uow.connection)),
    )
    download_gateway = DownloadGatewayService(
        uow_factory=uow_factory,
        repository_factory=download_repository,
        upstream=safe_http,
        settings=settings_service.current,
        clock_ms=_clock_ms,
        verify_capability=lambda attempt_id, source_index, capability: (
            credentials.verify_download_fetch(
                attempt_id=attempt_id, source_index=source_index, capability=capability
            )
        ),
    )
    supervisor = WorkerSupervisor()
    supervisor.register("download-polling", download_lifecycle.run_polling)
    supervisor.register("download-listener", download_lifecycle.run_listener)
    supervisor.register("account-deletion", account_service.run_deletion_worker)
    supervisor.register("history-retention", download_history.run_worker)
    supervisor.register("tracker-refresh", tracker_service.run_worker)
    health_service = HealthService(
        database_check=partial(_database_is_ready, database),
        directory_check=partial(_directory_is_ready, config.download_directory),
        supervisor=supervisor,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        config.ensure_directories()
        instance_lock = InstanceLock(config.instance_lock_path)
        instance_lock.acquire()
        try:
            await initialize_database(
                database,
                initial_settings=SystemSettings(
                    aria2_rpc_url=config.aria2_rpc_url,
                    aria2_rpc_secret=config.aria2_rpc_secret,
                ),
                credentials=credentials,
            )
            await account_service.ensure_initial_admin(
                password=config.initial_admin_password, required=not config.debug
            )
            await settings_service.initialize()
            await download_recovery.recover()
            app.state.readiness_check = health_service.is_ready
            app.state.readiness_details = health_service.errors
            await supervisor.start()
            yield
        finally:
            app.state.readiness_check = None
            app.state.readiness_details = None
            await supervisor.stop()
            await database.close()
            instance_lock.release()

    app = FastAPI(title="Aria2Deck V2", version="0.1.0", lifespan=lifespan)
    app.state.account_service = account_service
    app.state.download_queries = download_queries
    app.state.download_commands = download_commands
    app.state.download_history = download_history
    app.state.download_recovery = download_recovery
    app.state.download_lifecycle = download_lifecycle
    app.state.download_gateway = download_gateway
    app.state.tracker_service = tracker_service
    app.state.download_observations = observations
    app.state.settings_service = settings_service
    app.state.rate_limiter = rate_limiter
    app.state.supervisor = supervisor
    app.state.connectivity = connectivity
    app.state.cookie_secure = not config.debug
    app.state.readiness_check = None
    app.state.readiness_details = None
    install_error_handlers(app)
    app.include_router(accounts_router)
    app.include_router(downloads_router)
    app.include_router(settings_router)
    app.include_router(system_router)
    app.include_router(health_router)
    register_static_site(app, static_dir=static_dir or config.static_directory)
    return app
