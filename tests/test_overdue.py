"""逾期计算、罚息计提与状态恢复。"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from loan_ledger.models import LoanStatus
from loan_ledger.service import accrued_penalty, overdue_days

from .conftest import assert_conservation_and_replayable, at

logger = logging.getLogger("loan_ledger.test")


def _open(svc, loan_id="L1", principal="1000.00", interest="50.00", due=date(2026, 9, 1)):
    return svc.open_loan(loan_id, Decimal(principal), Decimal(interest), due)


def test_overdue_days_and_penalty_formula(service):
    """逾期天数与罚息公式：未还本金 * 0.0005 * 天数。"""
    loan = _open(service)
    assert overdue_days(loan, date(2026, 9, 1)) == 0   # 到期日当天不算逾期
    assert overdue_days(loan, date(2026, 9, 11)) == 10
    p = accrued_penalty(loan, date(2026, 9, 11))
    logger.info("判定依据: 1000 * 0.0005 * 10 = %s", p)
    assert p == Decimal("5.00")


def test_business_timezone_boundary(service):
    """业务时区边界：UTC 深夜对应上海次日，逾期天数以业务时区为准。"""
    from datetime import datetime, timezone
    _open(service)
    # 2026-09-01 16:30 UTC == 2026-09-02 00:30 Asia/Shanghai -> 已逾期 1 天
    now = datetime(2026, 9, 1, 16, 30, tzinfo=timezone.utc)
    loan = service.refresh_status("L1", now=now)
    logger.info("输入: now=%s (UTC) -> 业务日期 2026-09-02 | 状态: %s", now, loan.status)
    assert loan.status == LoanStatus.OVERDUE


def test_no_double_accrual_on_repeated_refresh(service):
    """同一日期反复 refresh 不重复计罚。"""
    _open(service)
    now = at(2026, 9, 11)
    service.refresh_status("L1", now=now)
    service.refresh_status("L1", now=now)
    service.refresh_status("L1", now=now)
    accruals = [e for e in service.ledger("L1") if e.kind == "PENALTY_ACCRUAL"]
    total = sum((e.amount for e in accruals), Decimal("0"))
    logger.info("3 次同日 refresh 后计提分录 %d 条，累计 %s", len(accruals), total)
    assert total == Decimal("5.00")
    assert_conservation_and_replayable(service, "L1")


def test_accrual_grows_monotonically_with_time(service):
    """时间推进，罚息只增不减、按增量补提。"""
    _open(service)
    service.refresh_status("L1", now=at(2026, 9, 6))   # 5 天 -> 2.50
    service.refresh_status("L1", now=at(2026, 9, 11))  # 10 天 -> 5.00，补提 2.50
    accruals = [e.amount for e in service.ledger("L1") if e.kind == "PENALTY_ACCRUAL"]
    logger.info("分段计提: %s", accruals)
    assert accruals == [Decimal("2.50"), Decimal("2.50")]
    assert_conservation_and_replayable(service, "L1")


def test_partial_repayment_recovers_status(service):
    """部分还款 -> 补缴后状态正确恢复，无抖动、无重复计罚。"""
    _open(service)
    day10 = at(2026, 9, 11)  # 逾期 10 天，罚息 5.00

    # 第一次：只够罚息+利息，本金未动 -> 仍逾期
    r1 = service.repay("L1", Decimal("55.00"), "req-1", now=day10)
    assert r1.status_after == LoanStatus.OVERDUE
    logger.info("部分还款 55.00 -> 分摊 %s，状态 %s", r1.allocation, r1.status_after)

    # 同一天再次 refresh：罚息不得重复计提
    service.refresh_status("L1", now=day10)
    accruals = [e for e in service.ledger("L1") if e.kind == "PENALTY_ACCRUAL"]
    assert sum((e.amount for e in accruals), Decimal("0")) == Decimal("5.00")

    # 补缴全部本金 -> 结清
    r2 = service.repay("L1", Decimal("1000.00"), "req-2", now=day10)
    logger.info("补缴 1000.00 -> 分摊 %s，状态 %s", r2.allocation, r2.status_after)
    assert r2.allocation.principal == Decimal("1000.00")
    assert r2.status_after == LoanStatus.SETTLED

    # 结清后时间推进：不得重新计罚、状态不得回退
    service.refresh_status("L1", now=at(2026, 12, 31))
    loan = service.get_loan("L1")
    accruals_after = [e for e in service.ledger("L1") if e.kind == "PENALTY_ACCRUAL"]
    assert loan.status == LoanStatus.SETTLED
    assert sum((e.amount for e in accruals_after), Decimal("0")) == Decimal("5.00")
    assert_conservation_and_replayable(service, "L1")


def test_status_not_flapping_between_refreshes(service):
    """到期日前后多次 refresh，状态只随日期单向变化，不抖动。"""
    _open(service)
    assert service.refresh_status("L1", now=at(2026, 8, 31)).status == LoanStatus.ACTIVE
    assert service.refresh_status("L1", now=at(2026, 9, 1)).status == LoanStatus.ACTIVE
    assert service.refresh_status("L1", now=at(2026, 9, 2)).status == LoanStatus.OVERDUE
    assert service.refresh_status("L1", now=at(2026, 9, 2)).status == LoanStatus.OVERDUE
    assert_conservation_and_replayable(service, "L1")
