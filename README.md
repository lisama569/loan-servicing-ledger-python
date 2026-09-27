# Loan Servicing Ledger

本地模拟的贷款还款与逾期服务（FastAPI + 本地 JSON 存储，无外部依赖与真实凭据）。

## 业务规则

### 业务时区与逾期
- 业务时区固定为 **Asia/Shanghai**；应还日期、逾期天数、罚息的业务日期都按该时区计算。
- 到期日当天不算逾期；`逾期天数 = max(0, 业务日期 - 应还日期)`。
- 罚息公式：`罚息 = 逾期未还本金 × 0.0005（日利率万分之五）× 逾期天数`，金额统一保留两位小数（四舍五入）。
- 罚息按“目标总额补足”计提：每次 refresh/还款时计算当前应提总额，与台账中已计提的差额作为新增计提分录。**只增不减、天然幂等**，同一日期反复 refresh 不会重复计罚；结清（SETTLED）后不再计提。

### 还款分摊顺序
固定顺序，可解释、可重放：

1. **罚息**（未付的已计提罚息）
2. **利息**（合同利息 − 已付利息）
3. **本金**（合同本金 − 已付本金）

每笔还款在台账中追加一条 `REPAYMENT` 分录，记录分摊明细；`amount == 罚息 + 利息 + 本金` 恒成立。

### 状态机与恢复
- `ACTIVE → OVERDUE`：业务日期超过应还日期且仍有剩余应还。
- `OVERDUE → SETTLED`：剩余应还与未付罚息全部清零（部分还款后补缴可正确恢复）。
- 状态只由“剩余应还 + 业务日期”决定，refresh 幂等，不会来回抖动；结清后不会被重新计罚，也拒绝新的还款（`LOAN_ALREADY_SETTLED`）。

### 重复提交（幂等）
- 每笔还款必须携带 `request_id`，幂等键作用域为 `(loan_id, request_id)`。
- 同一键重复到达：**直接重放首次结果**（响应中 `replayed=true`），不产生新分录，重启后依然有效（索引随台账持久化）。
- 同一键但金额不一致：判定为键被复用，拒绝（`VALIDATION`）。

### 并发与冲突
- 同一存储上的入账在进程内锁串行化，多笔并发还款结果确定：总额守恒、版本号单调递增、无状态覆盖。
- 可选 `expected_version` 乐观锁：与当前版本不一致时返回 `CONFLICT`（HTTP 409），调用方可重试。
- 并发争抢剩余额度时，超出部分以 `OVERPAYMENT` 拒绝，不会出现负余额。

### 错误码（互相区分）
| 错误码 | HTTP | 含义 |
|---|---|---|
| `LOAN_NOT_FOUND` | 404 | 目标贷款不存在 |
| `INVALID_AMOUNT` | 422 | 金额为零、负数或非法 |
| `OVERPAYMENT` | 422 | 超出剩余应还（含罚息） |
| `LOAN_ALREADY_SETTLED` | 409 | 贷款已结清 |
| `CONFLICT` | 409 | 乐观锁版本冲突 |
| `VALIDATION` | 422 | 其他请求非法（如幂等键复用金额不一致） |

## HTTP 接口
- `POST /loans` 开仓：`{loan_id, principal, interest, due_date}`
- `POST /loans/{loan_id}/repayments` 还款：`{amount, request_id, expected_version?}`
- `GET /loans/{loan_id}` 查询（返回前按当前业务日期 refresh 罚息与状态）
- `GET /loans/{loan_id}/ledger` 查看台账分录（可重放）

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest           # 全部测试（日志打印输入、分摊明细与判定依据）
uv run python main.py   # 启动服务，默认 127.0.0.1:8000
```

存储文件默认为 `$LOAN_LEDGER_DB`（未设置时为临时目录下 `loan_ledger_db.json`），
删除该文件即可重置。示例：

```bash
curl -X POST localhost:8000/loans -H 'content-type: application/json' \
  -d '{"loan_id":"L1","principal":"1000","interest":"50","due_date":"2026-09-01"}'
curl -X POST localhost:8000/loans/L1/repayments -H 'content-type: application/json' \
  -d '{"amount":"105","request_id":"req-1"}'
curl localhost:8000/loans/L1/ledger
```

## 守恒与可重放
测试中的 `assert_conservation_and_replayable` 对任意操作序列校验：
每笔分录金额等于其分摊之和；重放全部 `REPAYMENT` 分录得到的各桶累计等于贷款当前
`paid_*`；累计计提罚息 ≥ 已付罚息；结清状态与台账完全对应。
`test_random_operation_sequence_conservation` 用随机操作序列（含重复、非法、超额、
refresh）验证该不变量恒成立。
