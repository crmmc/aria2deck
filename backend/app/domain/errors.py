from __future__ import annotations


class DomainError(Exception):
    def __init__(self, detail: str, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.headers = headers


class BadRequestError(DomainError):
    pass


class UnauthorizedError(DomainError):
    pass


class ForbiddenError(DomainError):
    pass


class NotFoundError(DomainError):
    pass


class ConflictError(DomainError):
    pass


class GoneError(DomainError):
    pass


class TooManyRequestsError(DomainError):
    def __init__(self, detail: str, *, retry_after: int | None = None) -> None:
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
        super().__init__(detail, headers=headers)


class PayloadTooLargeError(DomainError):
    pass


class BadGatewayError(DomainError):
    pass


class InternalDomainError(DomainError):
    pass
