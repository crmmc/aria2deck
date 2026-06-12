from __future__ import annotations


class DomainError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


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
    pass


class PayloadTooLargeError(DomainError):
    pass


class BadGatewayError(DomainError):
    pass


class InternalDomainError(DomainError):
    pass
