# 社区到家额度与离线商户报销

这是一个仅依赖 Python 标准库的额度与凭证服务。核心是 `quota_service.QuotaService`，
另提供：

- 线上下单端接口：`OnlineOrderChannel` / `/v1/online/...`
- 终端补传接口：`TerminalChannel` / `/v1/terminal/...`
- 经办审批接口：`OperatorChannel` / `/v1/admin/...`
- JSON/HTTP 适配、命令行和可执行样本场景

金额一律为整数分；所有时间为带偏移的 ISO 8601；额度期、授权期和领用期均为半开区间
`[from, until)`。

## 运行检查

```bash
python3 -m unittest discover -s tests -v
python3 -m quota_service scenario --samples samples
```

加载样本并启动 HTTP 服务：

```bash
QUOTA_SAMPLES=samples python3 app.py --port 8080
# 或
python3 -m quota_service serve --samples samples --port 8080
```

健康检查：

```bash
curl http://127.0.0.1:8080/health
```

## Python 核心用法

```python
from quota_service import QuotaService, OnlineOrderChannel, TerminalChannel, OperatorChannel

svc = QuotaService()
svc.load_ndjson_dir("samples")

online = OnlineOrderChannel(svc)
quote = online.quote("c-a", "2026-11-29T10:00:00+08:00")
order = online.confirm(
    "c-a",
    at="2026-11-29T10:05:00+08:00",
    actor="help-a",
    accept_self_pay=True,
)

delivery = online.confirm_delivery(
    order["order_id"],
    [{"line": "a1", "confirmed_amount": 3100}],  # a2 缺货/不合规即不列或确认 0
    at="2026-11-29T13:35:00+08:00",
    signed_at="2026-11-29T13:30:00+08:00",
    actor="help-a",
)

operator = OperatorChannel(svc)
operator.verify_delivery(delivery["delivery_id"], "op-li")
settlement = operator.issue_settlement(
    "set-1", "op-li", "2026-12-01T10:00:00+08:00",
    merchant="mx", items=["lease1#1", "lease1#2"],
)
```

## 已实现的业务约束

### 额度与出资

- 家庭共享额度：`beneficiary` 为空。
- 居民专用额度：`beneficiary` 精确匹配，且商品品类必须匹配额度品类；`category`
  为空表示品类不限。
- 绝不会使用另一个居民的专用额度补本居民或共享余额的不足。
- 出资顺序：居民匹配专用额度 → 家庭共享额度 → 明确确认的自费。
- 同层额度按 `(until, grant)` 排序，即先到期、再按编号分摊；发放批不跨期拼用。
- 报价、订单行和交付结果均给出逐商品出资、额度编号和到期日。
- 自费必须显式确认；改篮导致自费增加时，新报价标记
  `requires_resident_acceptance=True`，未经居民接受不能确认，居民也可拒绝报价。

### 授权与代办

- 受益人本人办理不需要 mandate；其他 `customer/actor` 必须有指定受益人的有效授权。
- 授权在 `[from, until)` 内有效；若有 `revoked_at`，则 `t < revoked_at` 才有效。
- 撤销后，代办人的下单、改篮确认、交付确认等仍需代办确认的操作会抛出
  `AuthorizationError` 并停止。
- 已在撤销前签收的购买，可由经办人通过 `operator=...` 按签收证据审核，不要求授权在
  审批时刻仍有效。

### 线上冻结、交付与退款

- 确认订单只创建额度 hold，不直接核销；未交付/取消/缺货/拆出不合规商品时释放对应
  hold，居民权利不会在货未送成时被消掉。
- 部分签收按行和实际确认金额结算；每行保留原始分摊和 `hold_id/charge_id`，退款逐行
  追溯。
- 整行撤销精确退回该行原额度/自费承担额；部分金额默认按原分摊比例精确到分退回，也可
  选择 `refund_policy="waterfall"` 按出资层级冲减。
- 退款发生时若仍在额度期内，退回额度可恢复使用；若已过期，退款记为
  `refunded_expired` / `reusable=False` 和 `unusable_reason=grant_period_ended`，
  不增加新一期额度。
- 额度期末未完成的 hold 可由 `expire_due_commitments(at)` 按期边界标记失效；
  `recommit_order(...)` 用当前有效额度重新取得出资承诺。

### 终端离线额度与闭账

- 终端领用记录固定一个 terminal、lease、beneficiary、mandate 和 category，并带明确
  `allocations`。
- lease hold 在其有效期及闭账/裁决前占用同家庭额度，线上报价不能用走。
- lease 到期后，未完成闭账或缺号未处理时不释放领用额度，不会把全部额度直接返给家庭。
- 收据身份是 `(lease, seq)`；`message` 仅作传输编号。相同 `(lease, seq)` 且业务字段
  相同的重传幂等接收，并保留多个 transport message；业务字段不同则记录 conflict，不覆盖
  原凭证。
- 凭证有效性按 `occurred_at` 判断：lease 期间、mandate 当时有效、品类和额度期合规。
- 撤销生效后的同授权交易标记为 `disputed` / `disputed_authorization`，不能进入正常
  结算。
- closure 只能由登记终端递交；`1..last_seq` 中缺号必须补齐收据，或由经办人逐笔
  `void_sequence(...)` 后，closure 才是 `complete`，相关收据才能结算。
- 作废决定永久保留；之后收到同序号抵触凭证时，凭证状态为
  `conflicts_with_void`，服务保留 conflict 记录和原 void 决定。

### 结算、调整与查询

- 商户可 `apply_settlement(...)`，经办人 `issue_settlement(...)` 后结算成立。
- 一张结算可列多个已核对交付或离线凭证；结算快照保存原行、原凭证、出资分摊和判断依据。
- 已结算交付/凭证后来发生作废或退款时，不改写旧结算，而是生成负项 adjustment，并自动
  带入该商户下一张结算。
- `resident_view(beneficiary, household=None, at=None)` 返回当前可用额度、等待补传/闭账
  的 lease 和争议凭证。
- `merchant_view(merchant, at=None)` 只返回该商户自己的订单、终端凭证、可报销金额、
  缺号/闭账凭证、结算和调整；不会披露其他商户或家庭成员的购买。
- `admin_view()` 返回完整来源、冲突、待审核项和财务记录。

## HTTP 适配

通用动作端点：

```http
POST /v1/actions/{action.with.dots}
Content-Type: application/json
```

例如：

```bash
curl -X POST http://127.0.0.1:8080/v1/actions/view.merchant \
  -H 'Content-Type: application/json' \
  -d '{"merchant":"mx","at":"2026-12-01T10:00:00+08:00"}'
```

常用 REST 路由：

- `POST /v1/online/carts`
- `POST /v1/online/carts/{cart_id}/quote`
- `POST /v1/online/carts/{cart_id}/confirm`
- `POST /v1/online/orders/{order_id}/delivery`
- `POST /v1/terminal/receipts`
- `POST /v1/terminal/closures`
- `POST /v1/admin/sequences/void`
- `POST /v1/admin/receipts/verify`
- `POST /v1/admin/settlements`
- `GET /v1/views/resident/{beneficiary}?household=hh8&at=...`
- `GET /v1/views/merchant/{merchant}?at=...`
- `GET /v1/admin`

## 数据来源

`samples/*.ndjson` 是可更新的交换来源。代码按
`grants → mandates → terminals → receipts → closures → carts` 的依赖顺序加载；
也可以随时调用对应的 `upsert_*` / `submit_*` 方法更新来源。
