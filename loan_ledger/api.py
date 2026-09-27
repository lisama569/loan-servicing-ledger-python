"""FastAPI HTTP layer exposing the servicing service.

Each domain error maps to a distinct HTTP status + machine-readable code so
clients can tell rejections apart:
  LOAN_NOT_FOUND 404 | INVALID_AMOUNT 422 | OVERPAYMENT 409 |
  LOAN_ALREADY_SETTLED 409 | VERSION_CONFLICT 409 | DUPLICATE_REQUEST 409
"""
from __future__ import annotations

import dataclasses

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import ErrorCode, LedgerError
from .service import LoanServicingService

_STATUS_BY_CODE = {
    ErrorCode.LOAN_NOT_FOUND: 404,
    ErrorCode.INVALID_AMOUNT: 422,
    ErrorCode.OVERPAYMENT: 409,
    ErrorCode.LOAN_ALREADY_SETTLED: 409,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.DUPLICATE_REQUEST: 409,
}


class CreateLoanRequest(BaseModel):
    loan_id: str
    principal_cents: int
    interest_cents: int = 0
    due_date: str = Field(description="ISO date, business timezone")
    daily_penalty_rate_ppm: int = 0


class RepaymentRequest(BaseModel):
    request_id: str = Field(description="idempotency key")
    amount_cents: int
    expected_version: int | None = None


def create_app(service: LoanServicingService) -> FastAPI:
    app = FastAPI(title="loan-servicing-ledger")
    app.state.service = service

    @app.exception_handler(LedgerError)
    async def ledger_error_handler(_: Request, exc: LedgerError) -> JSONResponse:
        return JSONResponse(
            status_code=_STATUS_BY_CODE[exc.code],
            content={"error": {"code": str(exc.code), "message": exc.message}})

    @app.post("/loans", status_code=201)
    def create_loan(req: CreateLoanRequest) -> dict:
        loan = service.create_loan(**req.model_dump())
        return service.get_loan_view(loan.loan_id)

    @app.post("/loans/{loan_id}/repayments", status_code=200)
    def post_repayment(loan_id: str, req: RepaymentRequest) -> dict:
        result = service.post_repayment(
            loan_id=loan_id, request_id=req.request_id,
            amount_cents=req.amount_cents, expected_version=req.expected_version)
        body = dataclasses.asdict(result)
        body["status"] = str(result.status)
        return body

    @app.get("/loans/{loan_id}")
    def get_loan(loan_id: str) -> dict:
        return service.get_loan_view(loan_id)

    @app.get("/loans/{loan_id}/events")
    def get_events(loan_id: str) -> list[dict]:
        return [dataclasses.asdict(e) for e in service._storage.events(loan_id)]

    @app.get("/health/conservation")
    def conservation() -> dict:
        return {"conservation_ok": service.check_conservation()}

    return app
