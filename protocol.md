# 额度与商户凭证

时刻为带偏移的本地 ISO 8601。额度期、授权期限和离线领用期限均按半开区间处理。金额为整数分。额度有效性依据交付或离线成交时刻，线上冻结最晚到额度期末，未完成订单应在届时重新取得出资承诺。

`grants` 中 household 为家庭，beneficiary 为空表示共享，category 为空表示品类不限。授权 `mandates` 带 beneficiary、delegate、from、until 和可选的 revoked_at。发放批不跨期拼用。出资优先用居民匹配的专用额度、家庭共享，再用明确确认的自费。同一种额度按较早到期后按编号分摊。

`terminals` 的 lease 只用于一个 terminal、一个 beneficiary、一个 mandate 和一个品类，带明确来源 allocations，各项金额单位为分。凭证 `receipts` 的序号从 1 起，直到 `closures` 给出的 last_seq。接收身份为 (lease, seq)，传输编号 message 单独使用。闭账只能由登记终端递交，核销记录引用同一个序号，管理员保留审批时刻和依据。凭证里的 occurred_at 指已完成的门店成交，不是消息入站时间。

`carts` 的 customer 是操作人，items 按商品行给品类和金额。线上部分签收按行及实际确认金额办理，退款应逐行追溯原分摊。商户财务的一张结算可列若干笔可报销凭证，审批和调整必须能定位原行。

交换样本为 UTF-8 NDJSON，每行独立。文件中的行顺序保留这组资料的原始提供顺序。
