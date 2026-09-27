"""还款入账与逾期服务：分摊、罚息、状态恢复、幂等与并发控制。

业务规则（与 README 一致）：
- 业务时区固定为 Asia/Shanghai，应还日期/逾期天数都按该时区的日期计算；
- 分摊顺序固定为：罚息 -> 利息 -> 本金；
- 罚息 = 逾期未还本金 * 日利率(万分之五) * 逾期天数，按 refresh 时点计提、只增不减；
- 结清后不再计提罚息，也不接受新的还款。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from .errors import ErrorCode, LedgerError
from .models import Allocation, LedgerEntry, Loan, LoanStatus, RepaymentResult
from .storage import LoanStore

logger = logging.getLogger("loan_ledger.service")

BUSINESS_TZ = ZoneInfo("Asia/Shanghai")  # 约定的业务时区
PENALTY_DAILY_RATE = Decimal("0.0005")   # 罚息日利率：万分之五，按逾期未还本金计
CENT = Decimal("0.01")


def money(v: Decimal) -> Decimal:
    """金额统一保留两位小数（四舍五入）。"""
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


def business_today(now: datetime | None = None) -> date:
    """业务时区下的当前日期。"""
    now = now or datetime.now(tz=BUSINESS_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=BUSINESS_TZ)
    return now.astimezone(BUSINESS_TZ).date()


def overdue_days(loan: Loan, on: date) -> int:
    """截至 on 的逾期天数；未到期或已结清为 0。"""
    if loan.status == LoanStatus.SETTLED or on <= loan.due_date:
        return 0
    return (on - loan.due_date).days


def accrued_penalty(loan: Loan, on: date) -> Decimal:
    """截至 on 的罚息目标总额：逾期未还本金 * 日利率 * 逾期天数。"""
    days = overdue_days(loan, on)
    if days <= 0:
        return Decimal("0")
    unpaid_principal = loan.principal - loan.paid_principal
    return money(unpaid_principal * PENALTY_DAILY_RATE * days)


class LoanService:
    def __init__(self, store: LoanStore) -> None:
        self._store = store

    # ---- 开仓 ----
    def open_loan(
        self, loan_id: str, principal: Decimal, interest: Decimal, due_date: date
    ) -> Loan:
        principal, interest = money(principal), money(interest)
        if principal <= 0 or interest < 0:
            raise LedgerError(ErrorCode.VALIDATION, "principal 必须为正，interest 不能为负")
        loan = Loan(loan_id=loan_id, principal=principal, interest=interest, due_date=due_date)
        self._store.add_loan(loan)
        logger.info("open_loan loan=%s principal=%s interest=%s due=%s",
                    loan_id, principal, interest, due_date)
        return loan

    # ---- 内部：罚息计提（只增不减，结清后不再计提） ----
    def _accrued_total(self, loan_id: str) -> Decimal:
        return sum(
            (e.amount for e in self._store.entries(loan_id) if e.kind == "PENALTY_ACCRUAL"),
            Decimal("0"),
        )

    def _refresh_locked(self, loan: Loan, today: date) -> None:
        """在锁内重算罚息与状态。罚息按目标总额补足，天然幂等、不重复计罚。"""
        if loan.status == LoanStatus.SETTLED:
            return
        target = accrued_penalty(loan, today)
        accrued = self._accrued_total(loan.loan_id)
        if target > accrued:
            delta = target - accrued
            self._store.append_entry(LedgerEntry(
                seq=0, loan_id=loan.loan_id, kind="PENALTY_ACCRUAL", amount=delta,
                allocation=None, business_date=today,
                note=f"计提罚息至 {today}，目标 {target}，已提 {accrued}，补提 {delta}",
            ))
            logger.info("penalty accrual loan=%s target=%s accrued=%s delta=%s",
                        loan.loan_id, target, accrued, delta)
        # 状态恢复：只由剩余应还与日期决定，避免抖动
        loan.status = (
            LoanStatus.OVERDUE if today > loan.due_date and loan.outstanding > 0
            else LoanStatus.ACTIVE
        )
        self._store.save_loan(loan)

    def refresh_status(self, loan_id: str, now: datetime | None = None) -> Loan:
        today = business_today(now)
        with self._store.lock:
            loan = self._require_loan(loan_id)
            self._refresh_locked(loan, today)
            return loan

    # ---- 还款入账 ----
    def repay(
        self,
        loan_id: str,
        amount: Decimal,
        request_id: str,
        now: datetime | None = None,
        expected_version: int | None = None,
    ) -> RepaymentResult:
        """还款入账：幂等、按 罚息->利息->本金 顺序分摊。

        - 同一 (loan_id, request_id) 重复提交：直接重放首次结果，不重复入账；
        - expected_version 与当前版本不一致：报 CONFLICT，调用方可重试。
        """
        today = business_today(now)
        with self._store.lock:  # 串行化同一存储上的所有入账，并发结果确定
            loan = self._require_loan(loan_id)

            # 1) 金额合法性（先于幂等判断，保证拒绝原因可区分）
            if amount is None or not amount.is_finite() or amount <= 0:
                raise LedgerError(ErrorCode.INVALID_AMOUNT, f"还款金额必须为正数: {amount}")
            amount = money(amount)

            # 2) 幂等：request_id 已入账则重放；金额不一致说明键被复用，拒绝
            dup = self._store.find_by_request(loan_id, request_id)
            if dup is not None:
                if dup.amount != amount:
                    raise LedgerError(
                        ErrorCode.VALIDATION,
                        f"request_id={request_id} 已入账金额 {dup.amount}，与本次 {amount} 不一致",
                    )
                logger.info("repay replay loan=%s request=%s amount=%s",
                            loan_id, request_id, dup.amount)
                return self._result_from(loan, request_id, dup.amount,
                                         dup.allocation or Allocation(0, 0, 0),
                                         today, replayed=True)

            # 3) 乐观锁：版本冲突可区分地返回
            if expected_version is not None and expected_version != loan.version:
                raise LedgerError(
                    ErrorCode.CONFLICT,
                    f"版本冲突: expected={expected_version} actual={loan.version}",
                )

            # 4) 结清后拒绝新还款
            if loan.status == LoanStatus.SETTLED:
                raise LedgerError(ErrorCode.LOAN_ALREADY_SETTLED, "贷款已结清，拒绝还款")

            # 5) 先计提罚息，再校验超额
            self._refresh_locked(loan, today)
            penalty_due = self._accrued_total(loan_id) - loan.paid_penalty
            total_due = penalty_due + loan.outstanding
            if amount > total_due:
                raise LedgerError(
                    ErrorCode.OVERPAYMENT,
                    f"还款金额 {amount} 超出剩余应还 {total_due}",
                )

            # 6) 分摊：罚息 -> 利息 -> 本金
            alloc = self._allocate(loan, amount, penalty_due)
            loan.paid_penalty += alloc.penalty
            loan.paid_interest += alloc.interest
            loan.paid_principal += alloc.principal
            loan.version += 1
            loan.status = (
                LoanStatus.SETTLED if loan.outstanding == 0 and
                self._accrued_total(loan_id) - loan.paid_penalty == 0
                else LoanStatus.OVERDUE if today > loan.due_date
                else LoanStatus.ACTIVE
            )
            self._store.save_loan(loan)
            self._store.append_entry(LedgerEntry(
                seq=0, loan_id=loan_id, kind="REPAYMENT", amount=amount,
                allocation=alloc, business_date=today, request_id=request_id,
                note=f"分摊 罚息={alloc.penalty} 利息={alloc.interest} 本金={alloc.principal}",
            ))
            logger.info(
                "repay loan=%s request=%s amount=%s alloc=(penalty=%s interest=%s principal=%s) "
                "outstanding=%s status=%s",
                loan_id, request_id, amount, alloc.penalty, alloc.interest, alloc.principal,
                loan.outstanding, loan.status,
            )
            return self._result_from(loan, request_id, amount, alloc, today)

    @staticmethod
    def _allocate(loan: Loan, amount: Decimal, penalty_due: Decimal) -> Allocation:
        """按 罚息->利息->本金 顺序分摊，各桶最多分到剩余应还。"""
        remaining = amount
        to_penalty = min(remaining, penalty_due)
        remaining -= to_penalty
        to_interest = min(remaining, loan.interest - loan.paid_interest)
        remaining -= to_interest
        to_principal = min(remaining, loan.principal - loan.paid_principal)
        remaining -= to_principal
        assert remaining == 0, "分摊后仍有剩余，说明超额校验失效"
        return Allocation(penalty=to_penalty, interest=to_interest, principal=to_principal)

    def _result_from(self, loan: Loan, request_id: str, amount: Decimal,
                     alloc: Allocation, today: date, replayed: bool = False) -> RepaymentResult:
        return RepaymentResult(
            loan_id=loan.loan_id,
            request_id=request_id,
            accepted_amount=amount,
            allocation=alloc,
            outstanding_after=loan.outstanding,
            penalty_outstanding_after=self._accrued_total(loan.loan_id) - loan.paid_penalty,
            status_after=loan.status,
            overdue_days_after=overdue_days(loan, today),
            replayed=replayed,
        )

    # ---- 查询 ----
    def _require_loan(self, loan_id: str) -> Loan:
        loan = self._store.get_loan(loan_id)
        if loan is None:
            raise LedgerError(ErrorCode.LOAN_NOT_FOUND, f"贷款不存在: {loan_id}")
        return loan

    def get_loan(self, loan_id: str) -> Loan:
        return self.refresh_status(loan_id)

    def ledger(self, loan_id: str | None = None) -> list[LedgerEntry]:
        return self._store.entries(loan_id)
