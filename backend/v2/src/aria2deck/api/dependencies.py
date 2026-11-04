from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer

from ..application.accounts.models import Principal
from ..application.accounts.service import AccountService
from ..application.downloads.commands import DownloadCommandService
from ..application.downloads.gateway import DownloadGatewayService
from ..application.downloads.history import HistoryService
from ..application.downloads.queries import DownloadQueryService
from ..application.library.commands import LibraryCommandService
from ..application.library.deletion import ContentDeletionWorker
from ..application.library.download import LibraryDownloadService
from ..application.library.queries import LibraryQueryService
from ..application.packs.admission import PackAdmissionService
from ..application.packs.control import PackControlService
from ..application.packs.progress import PackProgressService
from ..application.settings.rate_limit import RateLimiter
from ..application.settings.service import SettingsService
from ..application.settings.trackers import TrackerService
from ..application.shares.access import PublicShareService
from ..application.shares.manage import ShareManageService
from ..application.system.connectivity import BackendConnectivity
from ..domain.accounts import Permission, authorize_admin_session
from ..domain.errors import DomainError, ErrorCode

SESSION_COOKIE = "aria2deck_v2_session"
SESSION_COOKIE_SCHEME = APIKeyCookie(
    name=SESSION_COOKIE, scheme_name="SessionCookie", auto_error=False
)
API_TOKEN_SCHEME = HTTPBearer(
    scheme_name="ApiToken", bearerFormat="API Token", auto_error=False
)
OPTIONAL_SESSION_OPENAPI: dict[str, object] = {"security": [{}, {"SessionCookie": []}]}


def account_service(request: Request) -> AccountService:
    return cast(AccountService, request.app.state.account_service)


def download_queries(request: Request) -> DownloadQueryService:
    return cast(DownloadQueryService, request.app.state.download_queries)


def download_commands(request: Request) -> DownloadCommandService:
    return cast(DownloadCommandService, request.app.state.download_commands)


def download_history(request: Request) -> HistoryService:
    return cast(HistoryService, request.app.state.download_history)


def download_gateway(request: Request) -> DownloadGatewayService:
    return cast(DownloadGatewayService, request.app.state.download_gateway)


def library_queries(request: Request) -> LibraryQueryService:
    return cast(LibraryQueryService, request.app.state.library_queries)


def library_commands(request: Request) -> LibraryCommandService:
    return cast(LibraryCommandService, request.app.state.library_commands)


def library_download(request: Request) -> LibraryDownloadService:
    return cast(LibraryDownloadService, request.app.state.library_download)


def library_deletion(request: Request) -> ContentDeletionWorker:
    return cast(ContentDeletionWorker, request.app.state.library_deletion)


def pack_admission(request: Request) -> PackAdmissionService:
    return cast(PackAdmissionService, request.app.state.pack_admission)


def pack_progress(request: Request) -> PackProgressService:
    return cast(PackProgressService, request.app.state.pack_progress)


def pack_control(request: Request) -> PackControlService:
    return cast(PackControlService, request.app.state.pack_control)


def share_manage(request: Request) -> ShareManageService:
    return cast(ShareManageService, request.app.state.share_manage)


def public_shares(request: Request) -> PublicShareService:
    return cast(PublicShareService, request.app.state.public_shares)


def tracker_service(request: Request) -> TrackerService:
    return cast(TrackerService, request.app.state.tracker_service)


def settings_service(request: Request) -> SettingsService:
    return cast(SettingsService, request.app.state.settings_service)


def rate_limiter(request: Request) -> RateLimiter:
    return cast(RateLimiter, request.app.state.rate_limiter)


def client_ip(request: Request) -> str:
    return str(getattr(request.client, "host", "unknown"))


def _limit_authenticated(principal: Principal, limiter: RateLimiter) -> Principal:
    limiter.check_authenticated(user_id=principal.user.id)
    return principal


def connectivity(request: Request) -> BackendConnectivity:
    return cast(BackendConnectivity, request.app.state.connectivity)


async def optional_principal(
    request: Request,
    service: AccountService = Depends(account_service),
    limiter: RateLimiter = Depends(rate_limiter),
) -> Principal | None:
    authorization = request.headers.get("Authorization")
    if authorization is not None:
        scheme, separator, value = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not separator or not value.strip():
            raise DomainError(ErrorCode.UNAUTHENTICATED, "认证凭证无效")
        principal = await service.authenticate_api_token(value.strip())
        return _limit_authenticated(principal, limiter)
    raw_session_id = request.cookies.get(SESSION_COOKIE)
    if raw_session_id is not None:
        principal = await service.authenticate_session(raw_session_id)
        return _limit_authenticated(principal, limiter)
    return None


def _require_principal(principal: Principal | None) -> Principal:
    if principal is None:
        raise DomainError(ErrorCode.UNAUTHENTICATED, "请先登录")
    return principal


async def required_principal(
    principal: Principal | None = Depends(optional_principal),
    _authorization: Annotated[
        HTTPAuthorizationCredentials | None, Security(API_TOKEN_SCHEME)
    ] = None,
    _session: Annotated[str | None, Security(SESSION_COOKIE_SCHEME)] = None,
) -> Principal:
    return _require_principal(principal)


async def required_session_principal(
    principal: Principal | None = Depends(optional_principal),
    _session: Annotated[str | None, Security(SESSION_COOKIE_SCHEME)] = None,
) -> Principal:
    return _require_principal(principal)


def required_admin_principal(
    principal: Principal = Depends(required_principal),
) -> Principal:
    authorize_admin_session(principal.user.role, principal.kind, Permission.MANAGE_STORAGE)
    return principal
