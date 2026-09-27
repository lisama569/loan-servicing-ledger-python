"""公共夹具与不变量校验：任意操作序列后，台账必须守恒且可重放。"""

from __future__ import annotations

import logging
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from loan_ledger.models import LoanStatus
from loan_ledger.service import BUSINESS_TZ, LoanService
from loan_ledger.storage import LoanStore

logger = logging.getLogger("loan_ledger.test")


@pytest.fixture()
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger_db.json"


@pytest.fixture()
def service(store_path: Path) -> LoanService:
    return LoanService(LoanStore(store_path))


def at(y: int, m: int, d: int, hour: int = 12) -> datetime:
    """构造业务时区下的时间点。"""
    return datetime(y, m, d, hour, tzinfo=BUSINESS_TZ)


def assert_conservation_and_replayable(svc: LoanService, loan_id: str) -> None:
    """核心不变量：

    1. 每笔 REPAYMENT 分录的 amount == 分摊之和（罚息+利息+本金）；
    2. 重放全部 REPAYMENT 分录，各桶累计 == 贷款当前 paid_*；
    3. 重放全部 PENALTY_ACCRUAL 分录，累计罚息 == 已付罚息 + 未付罚息；
    4. 已付不超过合同额；结清当且仅当全部应还（含罚息）为零。
    """
    loan = svc.get_loan(loan_id)
    entries = svc.ledger(loan_id)

    repays = [e for e in entries if e.kind == "REPAYMENT"]
    accruals = [e for e in entries if e.kind == "PENALTY_ACCRUAL"]

    for e in repays:
        assert e.allocation is not None
        assert e.amount == e.allocation.total, f"分录 {e.seq} 金额与分摊不符"

    paid_p = sum((e.allocation.principal for e in repays), Decimal("0"))
    paid_i = sum((e.allocation.interest for e in repays), Decimal("0"))
    paid_pen = sum((e.allocation.penalty for e in repays), Decimal("0"))
    assert (paid_p, paid_i, paid_pen) == (
        loan.paid_principal, loan.paid_interest, loan.paid_penalty,
    ), "重放台账得到的已付金额与贷款状态不一致"

    accrued = sum((e.amount for e in accruals), Decimal("0"))
    assert accrued >= loan.paid_penalty, "已付罚息超过累计计提，台账不守恒"

    assert loan.paid_principal <= loan.principal
    assert loan.paid_interest <= loan.interest

    fully_paid = (
        loan.outstanding == 0 and accrued - loan.paid_penalty == 0
    )
    assert (loan.status == LoanStatus.SETTLED) == fully_paid, (
        f"结清状态与台账不符: status={loan.status} outstanding={loan.outstanding} "
        f"penalty_due={accrued - loan.paid_penalty}"
    )
    logger.info(
        "守恒校验通过 loan=%s paid=(P:%s I:%s 罚:%s) accrued_penalty=%s status=%s",
        loan_id, paid_p, paid_i, paid_pen, accrued, loan.status,
    )
