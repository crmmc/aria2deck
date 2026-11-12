from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from .api.accounts import router as accounts_router
from .api.aria2_rpc import router as aria2_rpc_router
from .api.downloads import router as downloads_router
from .api.errors import install_error_handlers
from .api.files import router as files_router
from .api.health import router as health_router
from .api.packs import router as packs_router
from .api.public_shares import router as public_shares_router
from .api.request_body_limit import RequestBodyLimitMiddleware
from .api.settings import router as settings_router
from .api.shares import router as shares_router
from .api.static_site import register_static_site
from .api.system import router as system_router
from .api.ws import router as ws_router
from .application.accounts.revocation import InMemorySessionRevocationBus
from .application.accounts.service import AccountService
from .application.downloads.broadcast import TaskBroadcast
from .application.downloads.cleanup import DownloadUserCleanup
from .application.downloads.commands import DownloadCommandService
from .application.downloads.gateway import DownloadGatewayService
from .application.downloads.history import HistoryService
from .application.downloads.lifecycle import DownloadLifecycleService
from .application.downloads.projection import TaskProjector
from .application.downloads.queries import DownloadQueryService
from .application.downloads.recovery import DownloadRecoveryService
from .application.downloads.rpc_projection import VirtualGidSpace
from .application.downloads.rpc_service import Aria2RpcService
from .application.downloads.ws_service import WebSocketTaskService
from .application.library.cleanup import LibraryUserCleanup
from .application.library.commands import LibraryCommandService
from .application.library.deletion import ContentDeletionWorker
from .application.library.download import LibraryDownloadService
from .application.library.leases import ReadLeaseManager
from .application.library.queries import LibraryQueryService
from .application.packs.admission import PackAdmissionService
from .application.packs.cleanup import PackUserCleanup
from .application.packs.control import PackControlService
from .application.packs.dispatcher import PackDispatcher
from .application.packs.progress import PackProgressService
from .application.packs.source_cleanup import SourceCleanupWorker
from .application.ports import UnitOfWork
from .application.settings.download_limit import DownloadConnectionLimiter
from .application.settings.rate_limit import RateLimiter
from .application.settings.service import SettingsService
from .application.settings.trackers import TrackerService
from .application.shares.access import PublicShareService
from .application.shares.manage import ShareManageService
from .application.shares.tokens import ShareAccessTokenStore
from .application.system.connectivity import BackendConnectivity
from .application.system.health import HealthService
from .application.system.machine import MachineStatsService
from .application.system.supervisor import WorkerSupervisor
from .domain.errors import DomainError
from .domain.settings import SystemSettings
from .infrastructure.archive_writer import ArchiveWriter
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
from .infrastructure.sqlite.repositories.content import (
    DELETE_LEASE_OWNER,
    DELETE_LEASE_TTL_MS,
    ContentRepository,
)
from .infrastructure.sqlite.repositories.downloads import DownloadRepository
from .infrastructure.sqlite.repositories.packs import PackRepository
from .infrastructure.sqlite.repositories.quota import QuotaRepository
from .infrastructure.sqlite.repositories.settings import SettingsRepository
from .infrastructure.sqlite.repositories.shares import ShareRepository
from .infrastructure.sqlite.repositories.trackers import TrackerRepository
from .infrastructure.sqlite.unit_of_work import SQLiteUnitOfWork

logger = logging.getLogger(__name__)


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
    content_store = DownloadContentStore(config.download_directory)
    download_queries = DownloadQueryService(
        uow_factory=uow_factory,
        repository_factory=download_repository,
        content_repository_factory=content_repository,
        pack_repository_factory=pack_repository,
        account_repository_factory=account_repository,
        projector=task_projector,
        machine_space=content_store,
        settings=lambda: settings_service.current(),
    )
    submission_backend = Aria2SubmissionClient(
        settings=lambda: settings_service.current(),
        credentials=credentials,
        internal_base_url=config.internal_base_url,
        download_directory=config.download_directory,
    )
    settings_service = SettingsService(
        uow_factory=uow_factory,
        repository_factory=settings_repository,
        clock_ms=_clock_ms,
        aria2_version=submission_backend,
    )
    download_cleanup = DownloadUserCleanup(
        uow_factory=uow_factory,
        repository_factory=download_repository,
        control=Aria2DownloadControl(settings=settings_service.current),
        clock_ms=_clock_ms,
    )
    pack_writer_control = PackWriterControl()
    pack_lifecycle_lock = asyncio.Lock()
    pack_cleanup = PackUserCleanup(
        uow_factory=uow_factory,
        repository_factory=pack_repository,
        control=pack_writer_control,
        prepared_store=content_store,
        clock_ms=_clock_ms,
        lock=pack_lifecycle_lock,
    )
    library_cleanup = LibraryUserCleanup(
        uow_factory=uow_factory, repository_factory=content_repository, clock_ms=_clock_ms
    )
    session_revocations = InMemorySessionRevocationBus()
    account_service = AccountService(
        uow_factory=uow_factory,
        repository_factory=account_repository,
        credentials=credentials,
        clock_ms=_clock_ms,
        session_ttl_seconds=config.session_ttl_seconds,
        cleanup_ports=(download_cleanup, pack_cleanup, library_cleanup),
        revocation_bus=session_revocations,
    )
    rate_limiter = RateLimiter(settings=settings_service.current, clock_ms=_clock_ms)
    download_connections = DownloadConnectionLimiter(settings=settings_service.current)
    safe_http = SafeHttpClient()
    tracker_service = TrackerService(
        uow_factory=uow_factory,
        repository_factory=tracker_repository,
        fetcher=safe_http,
        settings=settings_service.current,
        clock_ms=_clock_ms,
    )
    submission_lock = asyncio.Lock()
    download_commands = DownloadCommandService(
        uow_factory=uow_factory,
        repository_factory=download_repository,
        content_repository_factory=content_repository,
        quota_repository_factory=lambda uow: QuotaRepository(cast(AsyncConnection, uow.connection)),
        pack_repository_factory=pack_repository,
        backend=submission_backend,
        trackers=tracker_service,
        rate_limiter=rate_limiter,
        machine_space=content_store,
        settings=settings_service.current,
        clock_ms=_clock_ms,
        planned_gid=credentials.derive_download_gid,
        submission_lock=submission_lock,
    )
    download_history = HistoryService(
        uow_factory=uow_factory,
        repository_factory=download_repository,
        projector=task_projector,
        clock_ms=_clock_ms,
        retention_days=lambda: settings_service.current().history_retention_days,
    )
    connectivity = BackendConnectivity()
    machine_stats = MachineStatsService(content_store)
    task_broadcast = TaskBroadcast()
    download_lifecycle = DownloadLifecycleService(
        uow_factory=uow_factory,
        repository_factory=download_repository,
        content_repository_factory=content_repository,
        backend=submission_backend,
        content_store=content_store,
        observations=observations,
        connectivity=connectivity,
        settings=settings_service.current,
        clock_ms=_clock_ms,
        broadcast=task_broadcast,
        queries=download_queries,
    )
    download_recovery = DownloadRecoveryService(
        uow_factory=uow_factory,
        repository_factory=download_repository,
        backend=submission_backend,
        trackers=tracker_service,
        clock_ms=_clock_ms,
        lifecycle=download_lifecycle,
        quota_repository_factory=lambda uow: QuotaRepository(cast(AsyncConnection, uow.connection)),
        submission_lock=submission_lock,
    )
    download_gateway = DownloadGatewayService(
        uow_factory=uow_factory,
        repository_factory=download_repository,
        upstream=safe_http,
        machine_space=content_store,
        settings=settings_service.current,
        clock_ms=_clock_ms,
        verify_capability=lambda attempt_id, source_index, capability: (
            credentials.verify_download_fetch(
                attempt_id=attempt_id, source_index=source_index, capability=capability
            )
        ),
    )
    supervisor = WorkerSupervisor()
    virtual_gids = VirtualGidSpace()
    aria2_rpc_service = Aria2RpcService(
        queries=download_queries,
        commands=download_commands,
        history=download_history,
        gids=virtual_gids,
    )
    supervisor.register("download-polling", download_lifecycle.run_polling)
    supervisor.register("download-recovery-pending", download_recovery.run_worker)
    supervisor.register("download-listener", download_lifecycle.run_listener)
    supervisor.register("account-deletion", account_service.run_deletion_worker)
    supervisor.register("history-retention", download_history.run_worker)
    supervisor.register("tracker-refresh", tracker_service.run_worker)
    library_queries = LibraryQueryService(
        uow_factory=uow_factory,
        repository_factory=content_repository,
        quota_repository_factory=lambda uow: QuotaRepository(cast(AsyncConnection, uow.connection)),
        storage_presence=content_store,
    )
    library_commands = LibraryCommandService(
        uow_factory=uow_factory,
        repository_factory=content_repository,
        quota_repository_factory=lambda uow: QuotaRepository(cast(AsyncConnection, uow.connection)),
        clock_ms=_clock_ms,
        lease_owner=DELETE_LEASE_OWNER,
        lease_ttl_ms=DELETE_LEASE_TTL_MS,
    )
    read_leases = ReadLeaseManager(uow_factory=uow_factory, repository_factory=content_repository)
    library_download = LibraryDownloadService(
        uow_factory=uow_factory,
        repository_factory=content_repository,
        leases=read_leases,
        content_store=content_store,
    )
    library_deletion = ContentDeletionWorker(
        uow_factory=uow_factory,
        repository_factory=content_repository,
        content_store=content_store,
        clock_ms=_clock_ms,
        owner=DELETE_LEASE_OWNER,
        lease_ttl_ms=DELETE_LEASE_TTL_MS,
    )
    supervisor.register("content-deletion", library_deletion.run_worker)
    pack_admission = PackAdmissionService(
        uow_factory=uow_factory,
        repository_factory=pack_repository,
        download_repository_factory=download_repository,
        quota_repository_factory=lambda uow: QuotaRepository(cast(AsyncConnection, uow.connection)),
        machine_space=content_store,
        settings=settings_service.current,
        clock_ms=_clock_ms,
    )
    pack_progress = PackProgressService(
        uow_factory=uow_factory,
        repository_factory=pack_repository,
        content_repository_factory=content_repository,
    )
    pack_control = PackControlService(
        uow_factory=uow_factory,
        repository_factory=pack_repository,
        content_repository_factory=content_repository,
        quota_repository_factory=lambda uow: QuotaRepository(cast(AsyncConnection, uow.connection)),
        stop_writer=pack_writer_control.stop_and_wait,
        clear_writer=pack_writer_control.clear,
        prepared_store=content_store,
        clock_ms=_clock_ms,
        lease_owner=DELETE_LEASE_OWNER,
        lease_ttl_ms=DELETE_LEASE_TTL_MS,
        lock=pack_lifecycle_lock,
    )
    pack_dispatcher = PackDispatcher(
        uow_factory=uow_factory,
        repository_factory=pack_repository,
        quota_repository_factory=lambda uow: QuotaRepository(cast(AsyncConnection, uow.connection)),
        writer=ArchiveWriter(),
        content_store=content_store,
        writer_control=pack_writer_control,
        settings=settings_service.current,
        clock_ms=_clock_ms,
    )
    pack_source_cleanup = SourceCleanupWorker(
        uow_factory=uow_factory,
        repository_factory=pack_repository,
        content_repository_factory=content_repository,
        quota_repository_factory=lambda uow: QuotaRepository(cast(AsyncConnection, uow.connection)),
        clock_ms=_clock_ms,
        lease_owner=DELETE_LEASE_OWNER,
        lease_ttl_ms=DELETE_LEASE_TTL_MS,
    )
    supervisor.register("pack-dispatcher", pack_dispatcher.run_worker)
    supervisor.register("pack-source-cleanup", pack_source_cleanup.run_worker)
    share_tokens = ShareAccessTokenStore(clock_ms=_clock_ms)
    share_manage = ShareManageService(
        uow_factory=uow_factory,
        repository_factory=lambda uow: ShareRepository(cast(AsyncConnection, uow.connection)),
        clock_ms=_clock_ms,
    )
    public_shares = PublicShareService(
        uow_factory=uow_factory,
        repository_factory=lambda uow: ShareRepository(cast(AsyncConnection, uow.connection)),
        content_repository_factory=content_repository,
        tokens=share_tokens,
        leases=read_leases,
        content_paths=content_store,
        streamer=library_download,
        clock_ms=_clock_ms,
    )
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
            async with uow_factory() as uow:
                reset_leases = await content_repository(uow).reset_all_read_leases()
            logger.info("启动读租约复位 count=%s", reset_leases)
            app.state.readiness_check = health_service.is_ready
            app.state.readiness_details = health_service.errors
            await supervisor.start()
            yield
        finally:
            app.state.readiness_check = None
            app.state.readiness_details = None
            await supervisor.stop()
            await ws_service.shutdown()
            await database.close()
            instance_lock.release()

    @dataclass(frozen=True)
    class RpcCorsSettings:
        allow_all: bool
        allowed_origins: frozenset[str]

    app = FastAPI(title="Aria2Deck V2", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestBodyLimitMiddleware)
    app.state.account_service = account_service
    app.state.download_queries = download_queries
    app.state.download_commands = download_commands
    app.state.download_history = download_history
    app.state.download_recovery = download_recovery
    app.state.download_lifecycle = download_lifecycle
    app.state.download_gateway = download_gateway
    app.state.library_queries = library_queries
    app.state.library_commands = library_commands
    app.state.library_download = library_download
    app.state.library_deletion = library_deletion
    app.state.pack_admission = pack_admission
    app.state.pack_progress = pack_progress
    app.state.pack_control = pack_control
    app.state.pack_dispatcher = pack_dispatcher
    app.state.pack_source_cleanup = pack_source_cleanup
    app.state.share_manage = share_manage
    app.state.public_shares = public_shares
    app.state.tracker_service = tracker_service
    app.state.download_observations = observations
    app.state.settings_service = settings_service
    app.state.rate_limiter = rate_limiter
    app.state.download_connections = download_connections
    app.state.supervisor = supervisor
    app.state.connectivity = connectivity
    app.state.machine_stats = machine_stats
    app.state.cookie_secure = not config.debug
    app.state.aria2_rpc_service = aria2_rpc_service
    ws_allowed_origins = config.rpc_allowed_origins  # 与 RPC 同源配置复用
    ws_service = WebSocketTaskService(
        authenticator=account_service,
        broadcast=task_broadcast,
        allowed_origins=ws_allowed_origins,
    )
    session_revocations.subscribe(ws_service.on_revocation)
    app.state.rpc_cors = RpcCorsSettings(
        allow_all=config.rpc_allow_all_origins,
        allowed_origins=config.rpc_allowed_origins,
    )
    app.state.ws_service = ws_service
    app.state.session_revocations = session_revocations
    app.state.readiness_check = None
    app.state.readiness_details = None
    install_error_handlers(app)
    app.include_router(aria2_rpc_router)
    app.include_router(accounts_router)
    app.include_router(downloads_router)
    app.include_router(files_router)
    app.include_router(packs_router)
    app.include_router(shares_router)
    app.include_router(public_shares_router)
    app.include_router(settings_router)
    app.include_router(system_router)
    app.include_router(ws_router)
    app.include_router(health_router)
    register_static_site(app, static_dir=static_dir or config.static_directory)
    return app
