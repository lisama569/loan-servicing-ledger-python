"""HTTP-layer tests: each rejection maps to a distinct status + code."""
from __future__ import annotations

import logging
from datetime import date

import pytest
from fastapi.testclient import TestClient

from loan_ledger.api import create_app
from loan_ledger.service import LoanServicingService
from loan_ledger.storage import Storage

log = logging.getLogger("ledger-test")


@pytest.fixture
def client() -> TestClient:
    svc = LoanServicingService(Storage(), today_fn=lambda: date(2026, 9, 27))
    c = TestClient(create_app(svc))
    r = c.post("/loans", json={"loan_id": "L1", "principal_cents": 100_000,
                               "interest_cents": 5_000, "due_date": "2026-09-20",
                               "daily_penalty_rate_ppm": 100})
    assert r.status_code == 201
    return c


def test_error_codes_are_distinguishable(client):
    cases = [
        ({"request_id": "e1", "amount_cents": 0}, 422, "INVALID_AMOUNT"),
        ({"request_id": "e2", "amount_cents": 10**9}, 409, "OVERPAYMENT"),
    ]
    for body, status, code in cases:
        r = client.post("/loans/L1/repayments", json=body)
        log.info("INPUT %s -> HTTP %d code=%s", body, r.status_code,
                 r.json()["error"]["code"])
        assert r.status_code == status
        assert r.json()["error"]["code"] == code
    r = client.post("/loans/GHOST/repayments",
                    json={"request_id": "e3", "amount_cents": 1})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "LOAN_NOT_FOUND"


def test_repayment_and_duplicate_over_http(client):
    body = {"request_id": "r1", "amount_cents": 3_000}
    r1 = client.post("/loans/L1/repayments", json=body)
    r2 = client.post("/loans/L1/repayments", json=body)
    assert r1.status_code == r2.status_code == 200
    assert r2.json()["duplicate"] is True
    assert r1.json()["allocation"] == r2.json()["allocation"]
    log.info("duplicate retry over HTTP returns cached allocation: %s",
             r2.json()["allocation"])
    assert client.get("/health/conservation").json()["conservation_ok"]


def test_version_conflict_over_http(client):
    v = client.get("/loans/L1").json()["version"]
    ok = client.post("/loans/L1/repayments",
                     json={"request_id": "r1", "amount_cents": 100,
                           "expected_version": v})
    assert ok.status_code == 200
    stale = client.post("/loans/L1/repayments",
                        json={"request_id": "r2", "amount_cents": 100,
                              "expected_version": v})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"
    log.info("stale expected_version=%d -> 409 VERSION_CONFLICT", v)
