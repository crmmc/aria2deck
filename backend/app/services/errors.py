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


ServiceError = DomainError
InternalServiceError = InternalDomainError
