from __future__ import annotations

import pytest
from fastapi import HTTPException

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
from app.http.errors import raise_http


def test_domain_error_is_http_decoupled() -> None:
    exc = BadRequestError("bad")

    assert isinstance(exc, DomainError)
    assert exc.detail == "bad"
    assert not hasattr(exc, "status_code")


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (BadRequestError("bad"), 400),
        (UnauthorizedError("unauthorized"), 401),
        (ForbiddenError("forbidden"), 403),
        (NotFoundError("missing"), 404),
        (ConflictError("conflict"), 409),
        (GoneError("gone"), 410),
        (TooManyRequestsError("limited"), 429),
        (InternalDomainError("internal"), 500),
    ],
)
def test_raise_http_maps_domain_errors_to_existing_http_statuses(
    error: DomainError, status_code: int
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        raise_http(error)

    assert exc_info.value.status_code == status_code
    assert exc_info.value.detail == error.detail
