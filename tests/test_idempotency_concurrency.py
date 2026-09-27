"""重复提交（幂等）与并发入账（确定性、冲突可识别）。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest

from loan_ledger.errors import ErrorCode, LedgerError
from loan_ledger.models import LoanStatus

from .conftest import assert_conservation_and_replayable, at

logger = logging.getLogger("loan_ledger.test")


def _open(svc, loan_id="L1", principal="1000.00", interest="50.00", due=date(2026, 9, 1)):
    return svc.open_loan(loan_id, Decimal(principal), Decimal(interest), due)


def test_duplicate_request_replayed_not_reposted(service):
    """同一 request_id 重试 N 次：只入账一次，后续为重放。"""
    _open(service)
    now = at(2026, 8, 20)
    first = service.repay("L1", Decimal("100.00"), "req-dup", now=now)
    for i in range(5):
        r = service.repay("L1", Decimal("100.00"), "req-dup", now=now)
        logger.info("第 %d 次重试 -> replayed=%s", i + 2, r.replayed)
        assert r.replayed is True
        assert r.allocation == first.allocation
    repays = [e for e in service.ledger("L1") if e.kind == "REPAYMENT"]
    assert len(repays) == 1, "重复提交不得产生重复分录"
    assert service.get_loan("L1").paid_interest == Decimal("50.00")
    assert_conservation_and_replayable(service, "L1")


def test_same_request_id_different_amount_rejected(service):
    """幂等键被复用但金额不一致：拒绝且原因可区分，不产生新分录。"""
    _open(service)
    now = at(2026, 8, 20)
    service.repay("L1", Decimal("100.00"), "req-m", now=now)
    with pytest.raises(LedgerError) as ei:
        service.repay("L1", Decimal("200.00"), "req-m", now=now)
    logger.info("同键不同额判定: %s (%s)", ei.value.code, ei.value.message)
    assert ei.value.code == ErrorCode.VALIDATION
    repays = [e for e in service.ledger("L1") if e.kind == "REPAYMENT"]
    assert len(repays) == 1
    assert_conservation_and_replayable(service, "L1")


def test_same_request_id_different_loans_are_independent(service):
    """幂等键按 (loan_id, request_id) 作用域隔离。"""
    _open(service, "L1")
    _open(service, "L2")
    now = at(2026, 8, 20)
    r1 = service.repay("L1", Decimal("10.00"), "req-shared", now=now)
    r2 = service.repay("L2", Decimal("10.00"), "req-shared", now=now)
    assert not r1.replayed and not r2.replayed
    assert_conservation_and_replayable(service, "L1")
    assert_conservation_and_replayable(service, "L2")


def test_concurrent_duplicate_requests_single_posting(service):
    """并发重复提交同一请求：全部成功但只有一笔入账。"""
    _open(service)
    now = at(2026, 8, 20)
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(
            lambda _: service.repay("L1", Decimal("100.00"), "req-race", now=now), range(16)))
    assert sum(1 for r in results if not r.replayed) == 1
    repays = [e for e in service.ledger("L1") if e.kind == "REPAYMENT"]
    assert len(repays) == 1
    logger.info("16 并发重复提交 -> 入账 1 笔，重放 %d 笔", sum(1 for r in results if r.replayed))
    assert_conservation_and_replayable(service, "L1")


def test_concurrent_distinct_repayments_deterministic_total(service):
    """多笔不同还款并发作用于同一贷款：总额确定、无金额错乱。"""
    _open(service, principal="10000.00", interest="0.00")
    now = at(2026, 8, 20)
    amounts = [Decimal("100.00")] * 50
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(
            lambda i: service.repay("L1", amounts[i], f"req-c-{i}", now=now), range(50)))
    accepted = sum((r.accepted_amount for r in results), Decimal("0"))
    loan = service.get_loan("L1")
    logger.info("50 笔并发各 100.00 -> 入账总额 %s，剩余 %s", accepted, loan.outstanding)
    assert accepted == Decimal("5000.00")
    assert loan.paid_principal == Decimal("5000.00")
    assert loan.outstanding == Decimal("5000.00")
    # 版本号单调递增且与入账笔数一致：没有状态互相覆盖
    assert loan.version == 50
    assert_conservation_and_replayable(service, "L1")


def test_optimistic_version_conflict_distinguishable(service):
    """expected_version 不匹配 -> CONFLICT，可与其他拒绝原因区分。"""
    _open(service)
    now = at(2026, 8, 20)
    service.repay("L1", Decimal("10.00"), "req-v1", now=now)
    loan = service.get_loan("L1")
    with pytest.raises(LedgerError) as ei:
        service.repay("L1", Decimal("10.00"), "req-v2", now=now,
                      expected_version=loan.version + 1)
    logger.info("版本冲突判定: %s (%s)", ei.value.code, ei.value.message)
    assert ei.value.code == ErrorCode.CONFLICT
    # 用正确版本重试则成功
    r = service.repay("L1", Decimal("10.00"), "req-v2", now=now,
                      expected_version=loan.version)
    assert not r.replayed
    assert_conservation_and_replayable(service, "L1")


def test_concurrent_overpayment_only_fits_once(service):
    """并发争抢剩余额度：超出部分必须有一笔被 OVERPAYMENT 拒绝，总额守恒。"""
    _open(service, principal="150.00", interest="0.00")
    now = at(2026, 8, 20)

    def pay(i):
        try:
            return service.repay("L1", Decimal("100.00"), f"req-o-{i}", now=now)
        except LedgerError as e:
            return e

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(pay, range(2)))
    ok = [r for r in results if not isinstance(r, LedgerError)]
    rejected = [r for r in results if isinstance(r, LedgerError)]
    logger.info("两笔 100 并发争抢 150 额度 -> 成功 %d 笔，拒绝 %d 笔", len(ok), len(rejected))
    assert len(ok) == 1 and len(rejected) == 1
    assert rejected[0].code == ErrorCode.OVERPAYMENT
    assert service.get_loan("L1").paid_principal == Decimal("100.00")
    assert_conservation_and_replayable(service, "L1")
