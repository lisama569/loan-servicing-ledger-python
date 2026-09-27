"""还款入账：分摊顺序、非法金额、超额、贷款不存在。"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import pytest

from loan_ledger.errors import ErrorCode, LedgerError
from loan_ledger.models import LoanStatus

from .conftest import assert_conservation_and_replayable, at

logger = logging.getLogger("loan_ledger.test")


def _open(svc, loan_id="L1", principal="1000.00", interest="50.00", due=date(2026, 9, 1)):
    return svc.open_loan(loan_id, Decimal(principal), Decimal(interest), due)


def test_allocation_order_penalty_then_interest_then_principal(service):
    """逾期后还款：先冲罚息，再利息，最后本金。"""
    _open(service)
    now = at(2026, 9, 11)  # 逾期 10 天，罚息 = 1000 * 0.0005 * 10 = 5.00
    r = service.repay("L1", Decimal("55.00"), "req-1", now=now)
    logger.info("输入: amount=55.00 逾期10天 | 分摊: %s | 判定: 罚息5.00->利息50.00->本金0", r.allocation)
    assert r.allocation.penalty == Decimal("5.00")
    assert r.allocation.interest == Decimal("50.00")
    assert r.allocation.principal == Decimal("0.00")
    assert r.status_after == LoanStatus.OVERDUE  # 本金未还，仍逾期
    assert_conservation_and_replayable(service, "L1")


def test_allocation_without_overdue(service):
    """未逾期还款：无罚息，直接 利息->本金。"""
    _open(service)
    r = service.repay("L1", Decimal("60.00"), "req-1", now=at(2026, 8, 20))
    logger.info("输入: amount=60.00 未逾期 | 分摊: %s", r.allocation)
    assert (r.allocation.penalty, r.allocation.interest, r.allocation.principal) == (
        Decimal("0.00"), Decimal("50.00"), Decimal("10.00"))
    assert r.status_after == LoanStatus.ACTIVE
    assert_conservation_and_replayable(service, "L1")


@pytest.mark.parametrize("bad", ["0", "-1", "-0.01"])
def test_non_positive_amount_rejected(service, bad):
    _open(service)
    with pytest.raises(LedgerError) as ei:
        service.repay("L1", Decimal(bad), f"bad-{bad}", now=at(2026, 8, 20))
    logger.info("输入: amount=%s | 判定: %s", bad, ei.value.code)
    assert ei.value.code == ErrorCode.INVALID_AMOUNT
    assert_conservation_and_replayable(service, "L1")


def test_overpayment_rejected_and_distinguishable(service):
    _open(service)
    now = at(2026, 8, 20)
    with pytest.raises(LedgerError) as ei:
        service.repay("L1", Decimal("1050.01"), "req-over", now=now)
    logger.info("输入: amount=1050.01 剩余应还=1050.00 | 判定: %s", ei.value.code)
    assert ei.value.code == ErrorCode.OVERPAYMENT
    # 与非法金额、贷款不存在是可区分的不同错误码
    with pytest.raises(LedgerError) as ei2:
        service.repay("L1", Decimal("-5"), "req-neg", now=now)
    assert ei2.value.code == ErrorCode.INVALID_AMOUNT != ei.value.code
    assert_conservation_and_replayable(service, "L1")


def test_exact_full_amount_accepted(service):
    """恰好等于剩余应还（边界值）必须被接受并结清。"""
    _open(service)
    r = service.repay("L1", Decimal("1050.00"), "req-full", now=at(2026, 8, 20))
    logger.info("输入: amount=1050.00 等于剩余应还 | 分摊: %s | 状态: %s", r.allocation, r.status_after)
    assert r.status_after == LoanStatus.SETTLED
    assert r.outstanding_after == Decimal("0")
    assert_conservation_and_replayable(service, "L1")


def test_loan_not_found(service):
    with pytest.raises(LedgerError) as ei:
        service.repay("GHOST", Decimal("1"), "req-x", now=at(2026, 8, 20))
    logger.info("输入: loan=GHOST | 判定: %s", ei.value.code)
    assert ei.value.code == ErrorCode.LOAN_NOT_FOUND


def test_repay_settled_loan_rejected(service):
    _open(service)
    service.repay("L1", Decimal("1050.00"), "req-full", now=at(2026, 8, 20))
    with pytest.raises(LedgerError) as ei:
        service.repay("L1", Decimal("1"), "req-after-settle", now=at(2026, 8, 21))
    logger.info("输入: 结清后再还款 | 判定: %s", ei.value.code)
    assert ei.value.code == ErrorCode.LOAN_ALREADY_SETTLED
    assert_conservation_and_replayable(service, "L1")
