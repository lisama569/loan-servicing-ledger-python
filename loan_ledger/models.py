"""Core data types. Money is always integer cents to keep math exact."""
from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field


class LoanStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    OVERDUE = "OVERDUE"
    SETTLED = "SETTLED"


@dataclass
class Loan:
    loan_id: str
    principal_cents: int          # original principal
    interest_cents: int           # total interest due
    due_date: str                 # ISO date, business timezone
    daily_penalty_rate_ppm: int   # penalty per day, parts per million of outstanding principal
    paid_principal_cents: int = 0
    paid_interest_cents: int = 0
    paid_penalty_cents: int = 0
    status: LoanStatus = LoanStatus.ACTIVE
    version: int = 0              # optimistic concurrency token

    def outstanding_principal(self) -> int:
        return self.principal_cents - self.paid_principal_cents

    def outstanding_interest(self) -> int:
        return self.interest_cents - self.paid_interest_cents


@dataclass
class Allocation:
    """How one repayment is split across buckets (penalty -> interest -> principal)."""
    penalty_cents: int = 0
    interest_cents: int = 0
    principal_cents: int = 0

    @property
    def total_cents(self) -> int:
        return self.penalty_cents + self.interest_cents + self.principal_cents


@dataclass
class LedgerEvent:
    """Append-only event; the loan state is a pure replay of these events."""
    seq: int
    kind: str                     # "loan_created" | "repayment_posted"
    loan_id: str
    payload: dict = field(default_factory=dict)


@dataclass
class RepaymentResult:
    loan_id: str
    request_id: str
    allocation: Allocation
    status: LoanStatus
    version: int
    duplicate: bool = False       # True when returned from idempotency cache
