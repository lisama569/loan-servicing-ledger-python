"""本地 JSON 文件存储：模拟持久化，进程内带锁保证并发安全。"""

from __future__ import annotations

import json
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path

from .models import Allocation, LedgerEntry, Loan, LoanStatus


def _loan_to_dict(loan: Loan) -> dict:
    return {
        "loan_id": loan.loan_id,
        "principal": str(loan.principal),
        "interest": str(loan.interest),
        "due_date": loan.due_date.isoformat(),
        "paid_principal": str(loan.paid_principal),
        "paid_interest": str(loan.paid_interest),
        "paid_penalty": str(loan.paid_penalty),
        "status": loan.status.value,
        "version": loan.version,
    }


def _loan_from_dict(d: dict) -> Loan:
    return Loan(
        loan_id=d["loan_id"],
        principal=Decimal(d["principal"]),
        interest=Decimal(d["interest"]),
        due_date=date.fromisoformat(d["due_date"]),
        paid_principal=Decimal(d["paid_principal"]),
        paid_interest=Decimal(d["paid_interest"]),
        paid_penalty=Decimal(d["paid_penalty"]),
        status=LoanStatus(d["status"]),
        version=d["version"],
    )


def _entry_to_dict(e: LedgerEntry) -> dict:
    return {
        "seq": e.seq,
        "loan_id": e.loan_id,
        "kind": e.kind,
        "amount": str(e.amount),
        "allocation": (
            None
            if e.allocation is None
            else {
                "penalty": str(e.allocation.penalty),
                "interest": str(e.allocation.interest),
                "principal": str(e.allocation.principal),
            }
        ),
        "business_date": e.business_date.isoformat(),
        "request_id": e.request_id,
        "note": e.note,
    }


class LoanStore:
    """贷款 + 台账的本地存储。

    - 数据落盘到单个 JSON 文件，便于本地验证与重放；
    - 进程内用锁串行化同一笔贷款的读写，保证并发入账结果确定。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._loans: dict[str, Loan] = {}
        self._entries: list[LedgerEntry] = []
        self._request_index: dict[tuple[str, str], LedgerEntry] = {}  # (loan_id, request_id) -> 分录
        self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        self._loans = {d["loan_id"]: _loan_from_dict(d) for d in raw.get("loans", [])}
        self._entries = [self._entry_from_dict(d) for d in raw.get("entries", [])]
        self._request_index = {
            (e.loan_id, e.request_id): e
            for e in self._entries
            if e.kind == "REPAYMENT" and e.request_id
        }

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "loans": [_loan_to_dict(l) for l in self._loans.values()],
            "entries": [_entry_to_dict(e) for e in self._entries],
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)  # 原子替换，避免半截文件

    @staticmethod
    def _entry_from_dict(d: dict) -> LedgerEntry:
        alloc = d.get("allocation")
        return LedgerEntry(
            seq=d["seq"],
            loan_id=d["loan_id"],
            kind=d["kind"],
            amount=Decimal(d["amount"]),
            allocation=None
            if alloc is None
            else Allocation(
                penalty=Decimal(alloc["penalty"]),
                interest=Decimal(alloc["interest"]),
                principal=Decimal(alloc["principal"]),
            ),
            business_date=date.fromisoformat(d["business_date"]),
            request_id=d.get("request_id"),
            note=d.get("note", ""),
        )

    # ---- 贷款读写 ----
    def get_loan(self, loan_id: str) -> Loan | None:
        with self._lock:
            return self._loans.get(loan_id)

    def save_loan(self, loan: Loan) -> None:
        with self._lock:
            self._loans[loan.loan_id] = loan
            self._flush()

    def add_loan(self, loan: Loan) -> None:
        with self._lock:
            if loan.loan_id in self._loans:
                raise ValueError(f"loan already exists: {loan.loan_id}")
            self._loans[loan.loan_id] = loan
            self._flush()

    # ---- 台账 ----
    def append_entry(self, entry: LedgerEntry) -> LedgerEntry:
        with self._lock:
            entry.seq = len(self._entries) + 1
            self._entries.append(entry)
            if entry.kind == "REPAYMENT" and entry.request_id:
                self._request_index[(entry.loan_id, entry.request_id)] = entry
            self._flush()
            return entry

    def entries(self, loan_id: str | None = None) -> list[LedgerEntry]:
        with self._lock:
            if loan_id is None:
                return list(self._entries)
            return [e for e in self._entries if e.loan_id == loan_id]

    def find_by_request(self, loan_id: str, request_id: str) -> LedgerEntry | None:
        with self._lock:
            return self._request_index.get((loan_id, request_id))

    # ---- 并发控制 ----
    @property
    def lock(self) -> threading.RLock:
        return self._lock
