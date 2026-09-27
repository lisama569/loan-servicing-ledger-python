# loan-servicing-ledger-python

本地模拟的贷款还款与逾期服务：还款入账与分摊、逾期罚息计算、状态恢复、
幂等去重与并发控制。数据存储为本地 JSON 文件（`data/ledger.json`），
不依赖任何外部服务或真实凭据。

## 运行与本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -v -s     # 运行全部测试，-s 可看到每笔输入/分摊/判定日志
uv run python main.py   # 启动 HTTP 服务（127.0.0.1:8000）
```

HTTP 接口示例：

```bash
# 建贷
curl -X POST localhost:8000/loans -H 'content-type: application/json' -d '{
  "loan_id":"L1","principal_cents":100000,"interest_cents":5000,
  "due_date":"2026-09-20","daily_penalty_rate_ppm":100}'
# 还款（request_id 为幂等键，expected_version 可选）
curl -X POST localhost:8000/loans/L1/repayments -H 'content-type: application/json' -d '{
  "request_id":"req-1","amount_cents":3000,"expected_version":0}'
# 查询台账视图 / 事件流水 / 守恒校验
curl localhost:8000/loans/L1
curl localhost:8000/loans/L1/events
curl localhost:8000/health/conservation
```

## 金额表示

所有金额均为**整数分**（cents），避免浮点误差，保证任意操作序列后台账守恒。

## 分摊顺序（waterfall）

每笔还款按固定顺序分摊，先到先扣：

1. **罚息**（截至业务日重算出的未付罚息）
2. **利息**（剩余应还利息）
3. **本金**（剩余本金）

分摊明细随结果返回（`allocation`），并写入事件流水，可逐笔解释。

## 逾期与罚息规则

- **业务时区**：默认 `Asia/Shanghai`（可配置）。应还日期与"今天"均在业务
  时区内解释，`逾期天数 = max(0, 业务今天 - 应还日期)`。
- **罚息确定性重算，不做增量累加**：
  `应计罚息 = 剩余本金 × 日罚息率(ppm) × 逾期天数 ÷ 1_000_000`。
  因为罚息永远由（剩余本金, 逾期天数）现算，天然满足：
  - 不会重复计罚（没有累加器可以漂移）；
  - 部分还款后罚息基数随剩余本金缩小；
  - 结清后剩余本金为 0，罚息恒为 0，不会被重新计罚；
  - 状态不会来回抖动：状态由当前余额与日期唯一决定。
- **状态机**：`ACTIVE → OVERDUE → SETTLED`。有剩余且已过期为 `OVERDUE`；
  剩余总额归零为 `SETTLED`（终态）；补缴后自动恢复。查询视图中的状态按
  业务日实时推导（`SETTLED` 除外，终态不再回退）。

## 重复提交（幂等）约定

- 每个还款请求必须携带全局唯一的 `request_id`。
- 相同 `request_id` + 相同负载（loan_id、金额）重试：**返回首次结果**，
  响应中 `duplicate=true`，台账不变，不会重复入账。
- 相同 `request_id` + 不同负载：拒绝，`409 DUPLICATE_REQUEST`。

## 并发控制

- 同一贷款的写操作由**按贷款加锁**串行化，多笔并发还款的结果确定，
  不会金额错乱或互相覆盖。
- 可选乐观并发：请求携带 `expected_version`，与当前版本不一致时拒绝
  `409 VERSION_CONFLICT`，调用方可重读后重试。

## 错误码（可互相区分）

| 错误码 | HTTP | 含义 |
|---|---|---|
| `LOAN_NOT_FOUND` | 404 | 目标贷款不存在 |
| `INVALID_AMOUNT` | 422 | 金额为零或负数（建贷参数非法同码） |
| `OVERPAYMENT` | 409 | 金额超出剩余应还总额 |
| `LOAN_ALREADY_SETTLED` | 409 | 贷款已结清 |
| `VERSION_CONFLICT` | 409 | 乐观锁版本冲突 |
| `DUPLICATE_REQUEST` | 409 | 同一 request_id 携带不同负载 |

## 守恒与可重放

- 所有变动写入**追加式事件流水**（`loan_created` / `repayment_posted`），
  贷款状态等于事件流的纯重放（`LoanServicingService.replay()`，测试验证）。
- 守恒不变式：对每笔贷款，`Σ(还款事件金额) == 已还本金 + 已还利息 + 已还罚息`
  （`check_conservation()` / `GET /health/conservation`）。
- 测试覆盖：重复提交、并发入账（多线程 + 并发重试）、部分还款后恢复、
  超额与非法金额、结清后拒绝、随机操作序列后的守恒与重放、重启后持久化。
  单测日志打印每笔输入、分摊明细与判定依据（`pytest -s` 可见）。
