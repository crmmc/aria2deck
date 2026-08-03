from __future__ import annotations

from app.domain.errors import (
    BadRequestError,
    ConflictError,
    DomainError,
    ForbiddenError,
    GoneError,
    InternalDomainError,
    NotFoundError,
    TooManyRequestsError,
    UnauthorizedError,
)

__all__ = [
    "BadRequestError",
    "ConflictError",
    "DomainError",
    "ForbiddenError",
    "GoneError",
    "InternalDomainError",
    "InternalServiceError",
    "NotFoundError",
    "ServiceError",
    "TooManyRequestsError",
    "UnauthorizedError",
]

ServiceError = DomainError
InternalServiceError = InternalDomainError
