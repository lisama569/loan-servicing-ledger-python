"""Local JSON-file storage simulating a database. No external services.

Design notes:
- All state lives in memory and is mirrored to a JSON snapshot file so the
  service can be restarted locally without losing data.
- An append-only event log is kept alongside the snapshot; the loan state
  must always equal a pure replay of the events (tested in the test-suite).
- A global RLock plus per-loan locks give deterministic behaviour when many
  repayments hit the same loan concurrently.
"""
from __future__ import annotations

import dataclasses
import json
import threading
from pathlib import Path

from .models import LedgerEvent, Loan, LoanStatus


def loan_to_dict(loan: Loan) -> dict:
    d = dataclasses.asdict(loan)
    d["status"] = str(loan.status)
    return d


def loan_from_dict(d: dict) -> Loan:
    d = dict(d)
    d["status"] = LoanStatus(d["status"])
    return Loan(**d)


class Storage:
    """Thread-safe in-memory store with JSON snapshot persistence."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.RLock()
        self._loan_locks: dict[str, threading.Lock] = {}
        self._loans: dict[str, Loan] = {}
        self._events: list[LedgerEvent] = []
        self._idempotency: dict[str, dict] = {}  # request_id -> {payload, result}
        if self._path and self._path.exists():
            self._load()

    # ---------------- persistence ----------------

    def _load(self) -> None:
        data = json.loads(self._path.read_text(encoding="utf-8"))
        self._loans = {k: loan_from_dict(v) for k, v in data["loans"].items()}
        self._events = [LedgerEvent(**e) for e in data["events"]]
        self._idempotency = data["idempotency"]

    def _save_locked(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({
            "loans": {k: loan_to_dict(v) for k, v in self._loans.items()},
            "events": [dataclasses.asdict(e) for e in self._events],
            "idempotency": self._idempotency,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)  # atomic rename

    # ---------------- loans ----------------

    def get_loan(self, loan_id: str) -> Loan | None:
        with self._lock:
            return self._loans.get(loan_id)

    def put_loan(self, loan: Loan) -> None:
        with self._lock:
            self._loans[loan.loan_id] = loan

    def loan_lock(self, loan_id: str) -> threading.Lock:
        """Per-loan mutex: serializes concurrent repayments on one loan."""
        with self._lock:
            return self._loan_locks.setdefault(loan_id, threading.Lock())

    def list_loans(self) -> list[Loan]:
        with self._lock:
            return list(self._loans.values())

    # ---------------- events ----------------

    def append_event(self, kind: str, loan_id: str, payload: dict) -> LedgerEvent:
        with self._lock:
            ev = LedgerEvent(seq=len(self._events) + 1, kind=kind,
                             loan_id=loan_id, payload=payload)
            self._events.append(ev)
            return ev

    def events(self, loan_id: str | None = None) -> list[LedgerEvent]:
        with self._lock:
            if loan_id is None:
                return list(self._events)
            return [e for e in self._events if e.loan_id == loan_id]

    # ---------------- idempotency ----------------

    def get_idempotent(self, request_id: str) -> dict | None:
        with self._lock:
            return self._idempotency.get(request_id)

    def put_idempotent(self, request_id: str, record: dict) -> None:
        with self._lock:
            self._idempotency[request_id] = record

    # ---------------- commit ----------------

    def commit(self) -> None:
        """Persist the snapshot (called once per successful mutation)."""
        with self._lock:
            self._save_locked()
