from __future__ import annotations

from fastapi import HTTPException, status

from app.domain.errors import (
    BadGatewayError,
    BadRequestError,
    ConflictError,
    DomainError,
    ForbiddenError,
    GoneError,
    InternalDomainError,
    NotFoundError,
    PayloadTooLargeError,
    TooManyRequestsError,
    UnauthorizedError,
)


_HTTP_STATUS_BY_ERROR_TYPE: dict[type[DomainError], int] = {
    BadRequestError: status.HTTP_400_BAD_REQUEST,
    UnauthorizedError: status.HTTP_401_UNAUTHORIZED,
    ForbiddenError: status.HTTP_403_FORBIDDEN,
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    GoneError: status.HTTP_410_GONE,
    TooManyRequestsError: status.HTTP_429_TOO_MANY_REQUESTS,
    PayloadTooLargeError: status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    BadGatewayError: status.HTTP_502_BAD_GATEWAY,
    InternalDomainError: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def http_status_for_error(exc: DomainError) -> int:
    return _HTTP_STATUS_BY_ERROR_TYPE.get(
        type(exc), status.HTTP_500_INTERNAL_SERVER_ERROR
    )


def raise_http(exc: DomainError) -> None:
    raise HTTPException(status_code=http_status_for_error(exc), detail=exc.detail) from exc
