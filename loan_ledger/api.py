"""FastAPI 路由：把服务暴露为 HTTP 接口，错误码映射为可区分的响应。"""

from __future__ import annotations

import os
import tempfile
from datetime import date
from decimal import Decimal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import ErrorCode, LedgerError
from .models import Allocation, LedgerEntry, Loan, RepaymentResult
from .service import LoanService
from .storage import LoanStore

_DB_PATH = os.environ.get(
    "LOAN_LEDGER_DB", os.path.join(tempfile.gettempdir(), "loan_ledger_db.json")
)
service = LoanService(LoanStore(_DB_PATH))

app = FastAPI(title="Loan Servicing Ledger")

_STATUS = {
    ErrorCode.LOAN_NOT_FOUND: 404,
    ErrorCode.INVALID_AMOUNT: 422,
    ErrorCode.OVERPAYMENT: 422,
    ErrorCode.LOAN_ALREADY_SETTLED: 409,
    ErrorCode.CONFLICT: 409,
    ErrorCode.VALIDATION: 422,
}


class OpenLoanIn(BaseModel):
    loan_id: str
    principal: Decimal = Field(gt=0)
    interest: Decimal = Field(ge=0)
    due_date: date


class RepayIn(BaseModel):
    amount: Decimal
    request_id: str = Field(min_length=1)
    expected_version: int | None = None  # 可选乐观锁；不一致返回 409 CONFLICT


def _loan_out(loan: Loan) -> dict:
    return {
        "loan_id": loan.loan_id,
        "principal": str(loan.principal),
        "interest": str(loan.interest),
        "due_date": loan.due_date.isoformat(),
        "paid_principal": str(loan.paid_principal),
        "paid_interest": str(loan.paid_interest),
        "paid_penalty": str(loan.paid_penalty),
        "outstanding": str(loan.outstanding),
        "status": loan.status.value,
        "version": loan.version,
    }


def _alloc_out(a: Allocation) -> dict:
    return {"penalty": str(a.penalty), "interest": str(a.interest), "principal": str(a.principal)}


def _result_out(r: RepaymentResult) -> dict:
    return {
        "loan_id": r.loan_id,
        "request_id": r.request_id,
        "accepted_amount": str(r.accepted_amount),
        "allocation": _alloc_out(r.allocation),
        "outstanding_after": str(r.outstanding_after),
        "penalty_outstanding_after": str(r.penalty_outstanding_after),
        "status_after": r.status_after.value,
        "overdue_days_after": r.overdue_days_after,
        "replayed": r.replayed,
    }


def _entry_out(e: LedgerEntry) -> dict:
    return {
        "seq": e.seq,
        "loan_id": e.loan_id,
        "kind": e.kind,
        "amount": str(e.amount),
        "allocation": None if e.allocation is None else _alloc_out(e.allocation),
        "business_date": e.business_date.isoformat(),
        "request_id": e.request_id,
        "note": e.note,
    }


@app.exception_handler(LedgerError)
async def _on_ledger_error(_: Request, exc: LedgerError) -> JSONResponse:
    return JSONResponse(
        status_code=_STATUS[exc.code],
        content={"error": {"code": exc.code.value, "message": exc.message}},
    )


@app.post("/loans", status_code=201)
def open_loan(body: OpenLoanIn) -> dict:
    loan = service.open_loan(body.loan_id, body.principal, body.interest, body.due_date)
    return _loan_out(loan)


@app.post("/loans/{loan_id}/repayments")
def repay(loan_id: str, body: RepayIn) -> dict:
    result = service.repay(
        loan_id, body.amount, body.request_id, expected_version=body.expected_version
    )
    return _result_out(result)


@app.get("/loans/{loan_id}")
def get_loan(loan_id: str) -> dict:
    return _loan_out(service.get_loan(loan_id))


@app.get("/loans/{loan_id}/ledger")
def get_ledger(loan_id: str) -> list[dict]:
    return [_entry_out(e) for e in service.ledger(loan_id)]
