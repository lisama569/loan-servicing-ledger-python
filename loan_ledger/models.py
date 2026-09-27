"""数据模型：贷款台账、还款请求、入账结果与分摊明细。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


class LoanStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"          # 正常
    OVERDUE = "OVERDUE"        # 逾期
    SETTLED = "SETTLED"        # 已结清


@dataclass
class Loan:
    loan_id: str
    principal: Decimal                 # 合同本金
    interest: Decimal                  # 到期应付利息
    due_date: date                     # 应还日期（业务时区下的日期）
    paid_principal: Decimal = Decimal("0")
    paid_interest: Decimal = Decimal("0")
    paid_penalty: Decimal = Decimal("0")
    status: LoanStatus = LoanStatus.ACTIVE
    version: int = 0                   # 乐观锁版本号

    @property
    def outstanding(self) -> Decimal:
        """剩余应还（不含罚息，罚息另算）。"""
        return (self.principal - self.paid_principal) + (self.interest - self.paid_interest)


@dataclass
class Allocation:
    """一笔还款的分摊明细。"""

    penalty: Decimal
    interest: Decimal
    principal: Decimal

    @property
    def total(self) -> Decimal:
        return self.penalty + self.interest + self.principal


@dataclass
class LedgerEntry:
    """台账分录：每次状态变更（还款入账/罚息计提）追加一条，可重放。"""

    seq: int
    loan_id: str
    kind: str                          # "REPAYMENT" | "PENALTY_ACCRUAL"
    amount: Decimal
    allocation: Allocation | None
    business_date: date
    request_id: str | None = None      # 幂等键
    note: str = ""


@dataclass
class RepaymentResult:
    loan_id: str
    request_id: str
    accepted_amount: Decimal
    allocation: Allocation
    outstanding_after: Decimal
    penalty_outstanding_after: Decimal
    status_after: LoanStatus
    overdue_days_after: int
    replayed: bool = False             # True 表示命中幂等重放，未重复入账
