"""Loan servicing: repayment posting, allocation, overdue & recovery.

Rules (kept deterministic and explainable):

- Allocation order of every repayment: penalty -> interest -> principal.
- Penalty is *recomputed*, never accrued incrementally:
      penalty_due = outstanding_principal * daily_rate_ppm * overdue_days / 1e6
  Because it is derived from (remaining principal, overdue days) there is no
  double-counting, no oscillation, and a settled loan (principal == 0) can
  never accrue new penalty.
- Overdue days are counted in the configured business timezone:
      overdue_days = max(0, business_today - due_date).days
- Recovery: when a repayment brings the remaining total to zero the loan
  becomes SETTLED; while anything remains and today > due_date it is OVERDUE,
  otherwise ACTIVE. Partial payments simply shrink the penalty base.
- Idempotency: a request_id that arrives again with the identical payload
  returns the original result (duplicate=True) without re-posting; the same
  request_id with a *different* payload is rejected as DUPLICATE_REQUEST.
- Concurrency: mutations on one loan are serialized by a per-loan lock and
  guarded by an optimistic version; a stale expected_version is rejected as
  VERSION_CONFLICT, so concurrent postings never overwrite each other.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .errors import ErrorCode, LedgerError
from .models import Allocation, Loan, LoanStatus, RepaymentResult
from .storage import Storage, loan_from_dict, loan_to_dict


class LoanServicingService:
    def __init__(
        self,
        storage: Storage,
        business_tz: str = "Asia/Shanghai",
        today_fn: Callable[[], date] | None = None,
    ) -> None:
        self._storage = storage
        self._tz = ZoneInfo(business_tz)
        self._business_tz = business_tz
        # Injectable clock so tests can move "today" deterministically.
        self._today_fn = today_fn or (lambda: datetime.now(self._tz).date())

    # ---------------- time / overdue ----------------

    def business_today(self) -> date:
        return self._today_fn()

    def overdue_days(self, loan: Loan, today: date | None = None) -> int:
        today = today or self.business_today()
        due = date.fromisoformat(loan.due_date)
        return max(0, (today - due).days)

    def penalty_due_cents(self, loan: Loan, today: date | None = None) -> int:
        """Total penalty owed up to `today`, recomputed deterministically."""
        days = self.overdue_days(loan, today)
        if days <= 0 or loan.status == LoanStatus.SETTLED:
            return 0
        return loan.outstanding_principal() * loan.daily_penalty_rate_ppm * days // 1_000_000

    def outstanding_penalty_cents(self, loan: Loan, today: date | None = None) -> int:
        return max(0, self.penalty_due_cents(loan, today) - loan.paid_penalty_cents)

    def remaining_total_cents(self, loan: Loan, today: date | None = None) -> int:
        return (loan.outstanding_principal()
                + loan.outstanding_interest()
                + self.outstanding_penalty_cents(loan, today))

    # ---------------- commands ----------------

    def create_loan(self, loan_id: str, principal_cents: int, interest_cents: int,
                    due_date: str, daily_penalty_rate_ppm: int) -> Loan:
        if principal_cents <= 0 or interest_cents < 0:
            raise LedgerError(ErrorCode.INVALID_AMOUNT,
                              "principal must be positive and interest non-negative")
        if self._storage.get_loan(loan_id) is not None:
            raise LedgerError(ErrorCode.DUPLICATE_REQUEST,
                              f"loan {loan_id} already exists")
        loan = Loan(loan_id=loan_id, principal_cents=principal_cents,
                    interest_cents=interest_cents, due_date=due_date,
                    daily_penalty_rate_ppm=daily_penalty_rate_ppm)
        with self._storage.loan_lock(loan_id):
            self._storage.put_loan(loan)
            self._storage.append_event("loan_created", loan_id, loan_to_dict(loan))
            self._storage.commit()
        return loan

    def post_repayment(self, loan_id: str, request_id: str, amount_cents: int,
                       expected_version: int | None = None) -> RepaymentResult:
        # --- idempotency check (outside the loan lock is fine: map is locked) ---
        cached = self._storage.get_idempotent(request_id)
        if cached is not None:
            if cached["payload"] != {"loan_id": loan_id, "amount_cents": amount_cents}:
                raise LedgerError(
                    ErrorCode.DUPLICATE_REQUEST,
                    f"request_id {request_id} was already used with a different payload")
            result = self._result_from_dict(cached["result"])
            result.duplicate = True
            return result

        with self._storage.loan_lock(loan_id):
            # re-check inside the lock: a concurrent twin may have posted it
            cached = self._storage.get_idempotent(request_id)
            if cached is not None:
                if cached["payload"] != {"loan_id": loan_id, "amount_cents": amount_cents}:
                    raise LedgerError(
                        ErrorCode.DUPLICATE_REQUEST,
                        f"request_id {request_id} was already used with a different payload")
                result = self._result_from_dict(cached["result"])
                result.duplicate = True
                return result

            loan = self._storage.get_loan(loan_id)
            if loan is None:
                raise LedgerError(ErrorCode.LOAN_NOT_FOUND, f"loan {loan_id} not found")
            if amount_cents <= 0:
                raise LedgerError(ErrorCode.INVALID_AMOUNT,
                                  f"amount must be positive, got {amount_cents}")
            if loan.status == LoanStatus.SETTLED:
                raise LedgerError(ErrorCode.LOAN_ALREADY_SETTLED,
                                  f"loan {loan_id} is already settled")
            if expected_version is not None and expected_version != loan.version:
                raise LedgerError(
                    ErrorCode.VERSION_CONFLICT,
                    f"expected version {expected_version}, current {loan.version}")

            today = self.business_today()
            remaining = self.remaining_total_cents(loan, today)
            if amount_cents > remaining:
                raise LedgerError(
                    ErrorCode.OVERPAYMENT,
                    f"amount {amount_cents} exceeds remaining balance {remaining}")

            allocation = self._allocate(loan, amount_cents, today)
            loan.paid_penalty_cents += allocation.penalty_cents
            loan.paid_interest_cents += allocation.interest_cents
            loan.paid_principal_cents += allocation.principal_cents
            loan.status = self._derive_status(loan, today)
            loan.version += 1

            result = RepaymentResult(
                loan_id=loan_id, request_id=request_id, allocation=allocation,
                status=loan.status, version=loan.version)

            self._storage.put_loan(loan)
            self._storage.append_event("repayment_posted", loan_id, {
                "request_id": request_id,
                "amount_cents": amount_cents,
                "business_date": today.isoformat(),
                "allocation": dataclasses.asdict(allocation),
                "paid_principal_cents": loan.paid_principal_cents,
                "paid_interest_cents": loan.paid_interest_cents,
                "paid_penalty_cents": loan.paid_penalty_cents,
                "status": str(loan.status),
                "version": loan.version,
            })
            self._storage.put_idempotent(request_id, {
                "payload": {"loan_id": loan_id, "amount_cents": amount_cents},
                "result": self._result_to_dict(result),
            })
            self._storage.commit()
            return result

    # ---------------- queries ----------------

    def get_loan_view(self, loan_id: str) -> dict:
        loan = self._storage.get_loan(loan_id)
        if loan is None:
            raise LedgerError(ErrorCode.LOAN_NOT_FOUND, f"loan {loan_id} not found")
        today = self.business_today()
        # Status is derived as of today (SETTLED is terminal and never re-derived).
        status = (loan.status if loan.status == LoanStatus.SETTLED
                  else self._derive_status(loan, today))
        return {
            "loan_id": loan.loan_id,
            "status": str(status),
            "version": loan.version,
            "business_date": today.isoformat(),
            "overdue_days": self.overdue_days(loan, today),
            "outstanding_principal_cents": loan.outstanding_principal(),
            "outstanding_interest_cents": loan.outstanding_interest(),
            "outstanding_penalty_cents": self.outstanding_penalty_cents(loan, today),
            "remaining_total_cents": self.remaining_total_cents(loan, today),
            "paid_principal_cents": loan.paid_principal_cents,
            "paid_interest_cents": loan.paid_interest_cents,
            "paid_penalty_cents": loan.paid_penalty_cents,
        }

    # ---------------- replay / conservation ----------------

    def replay(self) -> dict[str, Loan]:
        """Rebuild every loan purely from the event log (determinism check)."""
        loans: dict[str, Loan] = {}
        for ev in self._storage.events():
            if ev.kind == "loan_created":
                loans[ev.loan_id] = loan_from_dict(ev.payload)
            elif ev.kind == "repayment_posted":
                loan = loans[ev.loan_id]
                loan.paid_principal_cents = ev.payload["paid_principal_cents"]
                loan.paid_interest_cents = ev.payload["paid_interest_cents"]
                loan.paid_penalty_cents = ev.payload["paid_penalty_cents"]
                loan.status = LoanStatus(ev.payload["status"])
                loan.version = ev.payload["version"]
        return loans

    def check_conservation(self) -> bool:
        """For every loan: sum(repayment allocations) == sum(paid buckets)."""
        for loan_id in {e.loan_id for e in self._storage.events()}:
            events = self._storage.events(loan_id)
            repaid = sum(e.payload["amount_cents"]
                         for e in events if e.kind == "repayment_posted")
            loan = self._storage.get_loan(loan_id)
            paid = (loan.paid_principal_cents + loan.paid_interest_cents
                    + loan.paid_penalty_cents)
            if repaid != paid:
                return False
        return True

    # ---------------- internals ----------------

    def _allocate(self, loan: Loan, amount_cents: int, today: date) -> Allocation:
        """Deterministic waterfall: penalty -> interest -> principal."""
        alloc = Allocation()
        left = amount_cents
        take = min(left, self.outstanding_penalty_cents(loan, today))
        alloc.penalty_cents, left = take, left - take
        take = min(left, loan.outstanding_interest())
        alloc.interest_cents, left = take, left - take
        take = min(left, loan.outstanding_principal())
        alloc.principal_cents, left = take, left - take
        return alloc

    def _derive_status(self, loan: Loan, today: date) -> LoanStatus:
        if self.remaining_total_cents(loan, today) == 0:
            return LoanStatus.SETTLED
        if self.overdue_days(loan, today) > 0:
            return LoanStatus.OVERDUE
        return LoanStatus.ACTIVE

    @staticmethod
    def _result_to_dict(r: RepaymentResult) -> dict:
        return {
            "loan_id": r.loan_id, "request_id": r.request_id,
            "allocation": dataclasses.asdict(r.allocation),
            "status": str(r.status), "version": r.version,
        }

    @staticmethod
    def _result_from_dict(d: dict) -> RepaymentResult:
        return RepaymentResult(
            loan_id=d["loan_id"], request_id=d["request_id"],
            allocation=Allocation(**d["allocation"]),
            status=LoanStatus(d["status"]), version=d["version"])
