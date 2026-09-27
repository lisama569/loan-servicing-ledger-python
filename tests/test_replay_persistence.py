"""台账可重放与本地持久化：任意操作序列后，重放分录可重建状态。"""

from __future__ import annotations

import logging
import random
from datetime import date, timedelta
from decimal import Decimal

from loan_ledger.errors import LedgerError
from loan_ledger.models import LoanStatus
from loan_ledger.service import LoanService
from loan_ledger.storage import LoanStore

from .conftest import assert_conservation_and_replayable, at

logger = logging.getLogger("loan_ledger.test")


def test_persistence_roundtrip(service, store_path):
    """落盘后用新 Store 重新加载，状态与台账完全一致。"""
    service.open_loan("L1", Decimal("1000.00"), Decimal("50.00"), date(2026, 9, 1))
    service.repay("L1", Decimal("105.00"), "req-1", now=at(2026, 9, 11))

    svc2 = LoanService(LoanStore(store_path))  # 模拟重启
    loan = svc2.get_loan("L1")
    assert loan.paid_principal == Decimal("50.00")
    assert loan.paid_interest == Decimal("50.00")
    # 重启后幂等索引仍然有效：重试不重复入账
    r = svc2.repay("L1", Decimal("105.00"), "req-1", now=at(2026, 9, 11))
    assert r.replayed is True
    assert len([e for e in svc2.ledger("L1") if e.kind == "REPAYMENT"]) == 1
    logger.info("重启后重放 req-1 -> replayed=True，台账未重复")
    assert_conservation_and_replayable(svc2, "L1")


def test_random_operation_sequence_conservation(service):
    """随机操作序列（还款/重复/非法/超额/refresh）后，台账始终守恒且可重放。"""
    rng = random.Random(20260927)
    service.open_loan("L1", Decimal("5000.00"), Decimal("100.00"), date(2026, 9, 1))
    day = date(2026, 8, 25)
    req_n = 0
    sent: dict[str, Decimal] = {}  # request_id -> 首次金额，用于构造合法重试
    for step in range(60):
        op = rng.choice(["repay", "dup", "bad", "over", "refresh"])
        day += timedelta(days=rng.choice([0, 1, 2]))
        now = at(day.year, day.month, day.day)
        try:
            if op == "repay":
                req_n += 1
                amt = money2(Decimal(rng.randint(1, 30000)) / 100)
                sent[f"req-{req_n}"] = amt
                r = service.repay("L1", amt, f"req-{req_n}", now=now)
                logger.info("step=%d repay %s -> 分摊 %s 状态 %s", step, amt, r.allocation, r.status_after)
            elif op == "dup" and sent:
                rid = rng.choice(list(sent))
                r = service.repay("L1", sent[rid], rid, now=now)  # 同键同额重试
                assert r.replayed
            elif op == "bad":
                service.repay("L1", Decimal("0"), f"req-bad-{step}", now=now)
            elif op == "over":
                service.repay("L1", Decimal("999999.99"), f"req-over-{step}", now=now)
            else:
                service.refresh_status("L1", now=now)
        except LedgerError as e:
            logger.info("step=%d op=%s 被拒绝: %s", step, op, e.code)
        assert_conservation_and_replayable(service, "L1")
    loan = service.get_loan("L1")
    logger.info("随机序列结束: status=%s outstanding=%s 分录数=%d",
                loan.status, loan.outstanding, len(service.ledger("L1")))


def money2(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"))
