"""Error codes and exceptions for the loan servicing ledger.

Every rejection carries a distinct, machine-readable reason code so that
callers can tell apart invalid amounts, overpayment, missing loans,
duplicate submissions and version conflicts.
"""
from __future__ import annotations

import enum


class ErrorCode(enum.StrEnum):
    LOAN_NOT_FOUND = "LOAN_NOT_FOUND"
    INVALID_AMOUNT = "INVALID_AMOUNT"          # zero or negative amount
    OVERPAYMENT = "OVERPAYMENT"                # exceeds remaining balance
    LOAN_ALREADY_SETTLED = "LOAN_ALREADY_SETTLED"
    VERSION_CONFLICT = "VERSION_CONFLICT"      # optimistic-concurrency mismatch
    DUPLICATE_REQUEST = "DUPLICATE_REQUEST"    # same request_id, different payload


class LedgerError(Exception):
    """Domain error with a distinct, explainable reason code."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
