"""End-to-end service tests.

Every test logs its inputs, the allocation waterfall and the decision
rationale so failures are explainable from the log alone.
Run with:  uv run pytest -v -s
"""
from __future__ import annotations

import logging
import threading
from datetime import date, timedelta

import pytest

from loan_ledger.errors import ErrorCode, LedgerError
from loan_ledger.models import LoanStatus
from loan_ledger.service import LoanServicingService
from loan_ledger.storage import Storage

log = logging.getLogger("ledger-test")


@pytest.fixture(autouse=True)
def _log_to_stdout(caplog):
    with caplog.at_level(logging.INFO, logger="ledger-test"):
        yield


class MutableClock:
    def __init__(self, today: date) -> None:
        self.today = today

    def __call__(self) -> date:
        return self.today

    def advance(self, days: int) -> None:
        self.today += timedelta(days=days)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(date(2026, 9, 27))


@pytest.fixture
def svc(clock) -> LoanServicingService:
    return LoanServicingService(Storage(), business_tz="Asia/Shanghai", today_fn=clock)


def make_loan(svc, loan_id="L1", principal=100_000, interest=5_000,
              due="2026-09-20", rate_ppm=100):
    svc.create_loan(loan_id, principal, interest, due, rate_ppm)
    log.info("created loan %s principal=%d interest=%d due=%s rate=%dppm",
             loan_id, principal, interest, due, rate_ppm)
    return svc.get_loan_view(loan_id)


def pay(svc, loan_id, req, amount, expected_version=None):
    view_before = svc.get_loan_view(loan_id)
    log.info("INPUT repay loan=%s req=%s amount=%d expected_version=%s | "
             "before: outstanding(p=%d i=%d pen=%d) status=%s",
             loan_id, req, amount, expected_version,
             view_before["outstanding_principal_cents"],
             view_before["outstanding_interest_cents"],
             view_before["outstanding_penalty_cents"], view_before["status"])
    r = svc.post_repayment(loan_id, req, amount, expected_version)
    a = r.allocation
    log.info("ALLOCATION penalty=%d interest=%d principal=%d (waterfall: "
             "penalty->interest->principal) -> status=%s version=%d dup=%s",
             a.penalty_cents, a.interest_cents, a.principal_cents,
             r.status, r.version, r.duplicate)
    return r


# ---------- allocation & validation ----------

def test_allocation_waterfall_order(svc):
    make_loan(svc)  # 7 days overdue on 2026-09-27 -> penalty = 100000*100*7/1e6 = 70
    r = pay(svc, "L1", "r1", 3_000)
    assert (r.allocation.penalty_cents, r.allocation.interest_cents,
            r.allocation.principal_cents) == (70, 2_930, 0)
    log.info("RATIONALE: 70 penalty due first, remainder 2930 to interest, "
             "principal untouched")


def test_reject_zero_and_negative_amount(svc):
    make_loan(svc)
    for bad in (0, -1, -100_000):
        with pytest.raises(LedgerError) as ei:
            pay(svc, "L1", f"bad{bad}", bad)
        assert ei.value.code == ErrorCode.INVALID_AMOUNT
        log.info("REJECTED amount=%d code=%s (distinct from overpayment)",
                 bad, ei.value.code)


def test_reject_unknown_loan(svc):
    with pytest.raises(LedgerError) as ei:
        svc.post_repayment("GHOST", "r1", 100)
    assert ei.value.code == ErrorCode.LOAN_NOT_FOUND
    log.info("REJECTED unknown loan code=%s", ei.value.code)


def test_reject_overpayment(svc):
    make_loan(svc)
    remaining = svc.get_loan_view("L1")["remaining_total_cents"]
    with pytest.raises(LedgerError) as ei:
        pay(svc, "L1", "r-over", remaining + 1)
    assert ei.value.code == ErrorCode.OVERPAYMENT
    log.info("REJECTED amount=%d > remaining=%d code=%s",
             remaining + 1, remaining, ei.value.code)


def test_reject_after_settled(svc, clock):
    make_loan(svc)
    remaining = svc.get_loan_view("L1")["remaining_total_cents"]
    pay(svc, "L1", "r-full", remaining)
    assert svc.get_loan_view("L1")["status"] == LoanStatus.SETTLED
    with pytest.raises(LedgerError) as ei:
        svc.post_repayment("L1", "r-late", 1)
    assert ei.value.code == ErrorCode.LOAN_ALREADY_SETTLED
    log.info("REJECTED post-settlement repayment code=%s", ei.value.code)


# ---------- idempotency / duplicates ----------

def test_duplicate_retry_not_posted_twice(svc):
    make_loan(svc)
    r1 = pay(svc, "L1", "req-9", 3_000)
    r2 = pay(svc, "L1", "req-9", 3_000)  # retry of the same request
    assert r2.duplicate is True
    assert r2.allocation == r1.allocation
    view = svc.get_loan_view("L1")
    assert view["paid_interest_cents"] == r1.allocation.interest_cents
    log.info("RATIONALE: same request_id + same payload -> cached result, "
             "ledger unchanged (paid_interest=%d)", view["paid_interest_cents"])
    assert svc.check_conservation()


def test_same_request_id_different_payload_rejected(svc):
    make_loan(svc)
    pay(svc, "L1", "req-x", 1_000)
    with pytest.raises(LedgerError) as ei:
        svc.post_repayment("L1", "req-x", 2_000)
    assert ei.value.code == ErrorCode.DUPLICATE_REQUEST
    log.info("REJECTED reused request_id with different amount code=%s",
             ei.value.code)


# ---------- overdue & recovery ----------

def test_partial_repayment_then_recovery(svc, clock):
    make_loan(svc)  # overdue 7 days at start
    assert svc.get_loan_view("L1")["status"] == LoanStatus.OVERDUE

    clock.advance(3)  # 10 days overdue now
    pen_before = svc.get_loan_view("L1")["outstanding_penalty_cents"]
    log.info("overdue_days=10 penalty_due=%d (recomputed on remaining "
             "principal, never accrued twice)", pen_before)
    assert pen_before == 100_000 * 100 * 10 // 1_000_000

    pay(svc, "L1", "p1", 50_000)  # partial: penalty+interest+some principal
    v = svc.get_loan_view("L1")
    assert v["status"] == LoanStatus.OVERDUE  # still overdue, not oscillating
    log.info("after partial pay: status=%s remaining=%d", v["status"],
             v["remaining_total_cents"])

    # penalty base shrank -> recomputed penalty is smaller, no double count
    v = svc.get_loan_view("L1")
    due_now = v["outstanding_principal_cents"] * 100 * 10 // 1_000_000
    pen_after = v["outstanding_penalty_cents"]
    assert pen_after == max(0, due_now - v["paid_penalty_cents"])
    log.info("penalty recomputed on shrunk principal: due=%d paid=%d "
             "outstanding=%d", due_now, v["paid_penalty_cents"], pen_after)

    # catch up fully -> SETTLED
    pay(svc, "L1", "p2", v["remaining_total_cents"])
    assert svc.get_loan_view("L1")["status"] == LoanStatus.SETTLED

    # time passes: a settled loan must never accrue new penalty
    clock.advance(30)
    v = svc.get_loan_view("L1")
    assert v["status"] == LoanStatus.SETTLED
    assert v["outstanding_penalty_cents"] == 0
    assert v["remaining_total_cents"] == 0
    log.info("RATIONALE: settled loan has zero principal base -> penalty "
             "stays 0 after +30 days, status never flips back")
    assert svc.check_conservation()


def test_no_penalty_before_due_date(svc):
    make_loan(svc, due="2026-12-31")
    v = svc.get_loan_view("L1")
    assert v["overdue_days"] == 0
    assert v["outstanding_penalty_cents"] == 0
    assert v["status"] == LoanStatus.ACTIVE
    log.info("RATIONALE: today < due_date -> no overdue, no penalty")


# ---------- concurrency ----------

def test_concurrent_repayments_deterministic(svc):
    make_loan(svc, principal=1_000_000, interest=0, due="2026-12-31")
    n_threads, per = 8, 25
    amount = 1_000
    barrier = threading.Barrier(n_threads)
    errors: list[Exception] = []

    def worker(tid: int) -> None:
        try:
            for i in range(per):
                barrier.wait(timeout=10) if i == 0 else None
                svc.post_repayment("L1", f"t{tid}-r{i}", amount)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    total = n_threads * per * amount
    view = svc.get_loan_view("L1")
    log.info("INPUT: %d threads x %d repayments x %d cents on one loan",
             n_threads, per, amount)
    log.info("RESULT: paid_principal=%d expected=%d version=%d",
             view["paid_principal_cents"], total, view["version"])
    assert view["paid_principal_cents"] == total
    assert view["version"] == n_threads * per  # every posting bumped version once
    assert svc.check_conservation()
    log.info("RATIONALE: per-loan lock serializes postings -> deterministic "
             "sum, no lost updates")


def test_version_conflict_detectable(svc):
    make_loan(svc)
    v0 = svc.get_loan_view("L1")["version"]
    pay(svc, "L1", "r1", 1_000, expected_version=v0)  # ok, bumps to v0+1
    with pytest.raises(LedgerError) as ei:
        svc.post_repayment("L1", "r2", 1_000, expected_version=v0)  # stale
    assert ei.value.code == ErrorCode.VERSION_CONFLICT
    log.info("REJECTED stale expected_version=%d code=%s (distinct from "
             "overpayment/invalid)", v0, ei.value.code)


def test_concurrent_duplicate_retries_posted_once(svc):
    make_loan(svc, principal=1_000_000, interest=0, due="2026-12-31")
    results, errors = [], []

    def retry():
        try:
            results.append(svc.post_repayment("L1", "shared-req", 5_000))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=retry) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert svc.get_loan_view("L1")["paid_principal_cents"] == 5_000
    assert sum(1 for r in results if not r.duplicate) == 1
    log.info("RATIONALE: 10 concurrent retries of one request_id -> exactly "
             "one posting, nine duplicate=True responses")
    assert svc.check_conservation()


# ---------- conservation & replay ----------

def test_conservation_and_replay_after_random_sequence(svc, clock):
    make_loan(svc, "A", principal=80_000, interest=2_000, due="2026-09-25", rate_ppm=50)
    make_loan(svc, "B", principal=50_000, interest=1_000, due="2026-12-31", rate_ppm=0)
    ops = [("A", "a1", 10_000), ("B", "b1", 5_000), ("A", "a2", 3_000),
           ("A", "a1", 10_000),  # duplicate retry, must be a no-op
           ("B", "b2", 46_000)]  # settles B exactly
    for loan_id, req, amount in ops:
        pay(svc, loan_id, req, amount)
        clock.advance(1)

    assert svc.check_conservation()
    replayed = svc.replay()
    for loan_id in ("A", "B"):
        live = svc.get_loan_view(loan_id)
        rep = replayed[loan_id]
        log.info("REPLAY loan=%s live(paid=%d/%d/%d) replayed(paid=%d/%d/%d)",
                 loan_id, live["paid_principal_cents"], live["paid_interest_cents"],
                 live["paid_penalty_cents"], rep.paid_principal_cents,
                 rep.paid_interest_cents, rep.paid_penalty_cents)
        assert (rep.paid_principal_cents, rep.paid_interest_cents,
                rep.paid_penalty_cents) == (
                    live["paid_principal_cents"], live["paid_interest_cents"],
                    live["paid_penalty_cents"])
    log.info("RATIONALE: event log replay reproduces live state and sum of "
             "allocations equals sum of paid buckets for every loan")


def test_persistence_roundtrip(tmp_path, clock):
    path = tmp_path / "ledger.json"
    svc1 = LoanServicingService(Storage(path), today_fn=clock)
    make_loan(svc1)
    pay(svc1, "L1", "r1", 3_000)

    svc2 = LoanServicingService(Storage(path), today_fn=clock)
    v1, v2 = svc1.get_loan_view("L1"), svc2.get_loan_view("L1")
    assert v1 == v2
    r = svc2.post_repayment("L1", "r1", 3_000)  # idempotency survives restart
    assert r.duplicate is True
    log.info("RATIONALE: JSON snapshot reload preserves state and "
             "idempotency records across restarts")
