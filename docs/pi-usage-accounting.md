# Pi 用量记录口径

项目层 `PiNebius.populate_context_post_run` 使用 `pi-events-v1` 采集器读取
`pi.txt`；固定 ERP-Bench 和 Pi 源码及校验值保持不变。采集器累加普通
assistant `message_end.message.usage` 与 `compaction_end.result.usage`。
不再叠加 session 文件中的同一压缩记录、流式 message update 或 `totalTokens`。

- 输入量为 `input + cacheRead + cacheWrite`，各字段只累计一次。
- 输出量为 `output`；`reasoning` 已在输出内，不重复相加。
- `n_cache_tokens` 延续缓存读取量的既有含义；缓存写入量另记在 metadata。
- `metadata.usage_accounting.status` 为 `complete` 或 `incomplete`。
  缺失、格式不正确、未结束或中止的调用保留已观测用量并标记不完整，不能把
  已记录小计作为完整模型开销。完整仅指当前固定版本事件日志中的用量覆盖，
  不是供应商账单核对。
- 固定版本的内部摘要重试只在最终结果保留最后一次尝试的用量；
  `summarization_retry_*` 事件不带之前尝试的用量。因此一旦出现该事件，
  记录 `summarization_retry_usage_missing` 并标记不完整，仍只累计可见用量，
  不猜测失败尝试的 tokens 或金额。
- Pi 在收到供应商用量之前将所有计数初始化为零。成功返回的全零用量无法
  区分真实零值与未报告用量，因此记录 `zero_usage_unconfirmed` 并保守标记
  不完整；非零输入、零输出的有效记录不会因此降级。
- `cost_usd` 仅累计事件中报告的正数费用，不使用 token 数推算美元；全零或缺失
  的价格记录保持 `null`。`cost_status` 单独说明费用记录的完整性。

Harbor result 的 metadata 通过 `summarize_run().metrics.usage_accounting`
保留到 RunView 的 `usage.accounting` 和旧控制台的 metrics 中。RunView 的
用量说明区分完整记录、不完整小计和未标记完整性的旧记录。旧记录显示
`unknown`，本次不重写原始日志、结果、历史摘要或验收成绩。

`scripts/experiments.py manifest` 与 `report` 导出的每个 `runs[].product`
也保留归一化的 `usage_accounting`，并将其 `cost_status` 同步为
`cost_completeness`。后者区分 `complete`、`incomplete`、`not_reported` 和
`unknown`；token 完整不代表费用完整。已有 token 和 `actual_cost` 数字保持
原值，缺失费用仍为 `null`，历史无标记或无法识别的标记为 `unknown`。
这些字段是兼容性新增，配置指纹与业务结果汇总的口径不变。成本比较需同时
检查费用完整性，不能把不完整小计或历史未知值当作完整费用。

固定事件回归覆盖普通调用、成功压缩、压缩重试、缓存读写、缺失/中止/截断、
全零默认值、有效零输出、重复读取，
以及 result → summary → RunView 的传播。导出回归另覆盖两种命令的完整、
不完整、历史未知及费用未报告记录，检查源记录不被修改，不调用模型或 ERP。
