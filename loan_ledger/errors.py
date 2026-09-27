"""业务错误类型：每种拒绝原因都有独立的错误码，便于区分。"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    LOAN_NOT_FOUND = "LOAN_NOT_FOUND"
    INVALID_AMOUNT = "INVALID_AMOUNT"          # 金额为零或负数
    OVERPAYMENT = "OVERPAYMENT"                # 超出剩余应还
    LOAN_ALREADY_SETTLED = "LOAN_ALREADY_SETTLED"
    CONFLICT = "CONFLICT"                      # 并发冲突，需重试
    VALIDATION = "VALIDATION"                  # 请求字段非法


class LedgerError(Exception):
    """带错误码的业务异常。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
