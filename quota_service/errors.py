"""Errors raised by the community quota and merchant voucher service."""
from __future__ import annotations

from typing import Any, Optional


class QuotaServiceError(Exception):
    """Base class for all predictable service errors."""

    code = "quota_error"
    http_status = 400

    def __init__(
        self,
        message: str,
        *,
        details: Optional[dict[str, Any]] = None,
        code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code:
            self.code = code

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": True,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class ValidationError(QuotaServiceError):
    code = "validation_error"
    http_status = 422


class NotFoundError(QuotaServiceError):
    code = "not_found"
    http_status = 404


class ConflictError(QuotaServiceError):
    code = "conflict"
    http_status = 409


class AuthorizationError(QuotaServiceError):
    code = "authorization_denied"
    http_status = 403


class ConfirmationRequiredError(QuotaServiceError):
    code = "confirmation_required"
    http_status = 409


class FundingError(QuotaServiceError):
    code = "funding_error"
    http_status = 409


class InsufficientQuotaError(FundingError):
    code = "insufficient_quota"


class CommitmentExpiredError(FundingError):
    code = "commitment_expired"
    http_status = 409
