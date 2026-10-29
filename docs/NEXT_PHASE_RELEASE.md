# 新增支出利润率修复：发布与恢复手册

本次发布把利润率独立检查、产品指标与展示统一到 `new_spend_coverage_v2`。后端与两套前端应一起更新。可选保留优化默认关闭。开发计划见 [NEXT_PHASE_DEVELOPMENT_PLAN.md](NEXT_PHASE_DEVELOPMENT_PLAN.md)，当前能力见 [CURRENT_CAPABILITIES.md](CURRENT_CAPABILITIES.md)。

## 发布内容与来源

业务代码提交：`74a990a`。锁定 ERP-Bench 提交：`ceba3880af555129b5278e056a0c20f2fb5a0ba9`。证据和媒体通过后续提交追加；验收报告记录实际源码文件哈希，不能用文档提交时间冒充 ERP 执行版本。

新指标 schema 为 2，manifest 增加 `margin_basis`，因此新计划身份与旧计划不同。默认目标仍为 `repair_plan`／`preserve_quantities`。成本和产能候选保持独立目标；原始验收不改变评分标准。

## 升级前检查

1. 暂停接受新的准备和批准请求，保留读取与受控恢复能力。备份当前应用、来源包、计划 store、产品 cases、approval 文件、journal 和原始验收回执。备份包含私密授权材料，应按原来的权限保存，不上传为公开证据。
2. 盘点 store 中每个计划的状态及其来源包。对已完成、尚未执行、部分执行／结果未知分别处理。不要仅凭页面状态推断某一步没有发生；journal 与 ERP 回读决定恢复行为。
3. 准备新来源包，在副本中验证其文件清单和哈希。不得把新源码覆盖到旧计划绑定的 bundle 路径；保留旧包以供审计和只读历史处理。停用旧执行入口的新任务接收能力。
4. 部署新后端和前端，在只读状态检查身份、案例列表和历史回执，再开放新计划准备。已有有效未完成执行不能由新计划抢占其执行范围。

## 旧记录的具体处理

下列命令在受信任的运维进程中运行；`PLAN_STORE`、`CURRENT_BUNDLE`、`PLAN_ID` 由运维选择，不能来自模型工具调用。当前 bundle 指新版本完整恢复包，原始 bundle 留在原路径。

```sh
python -m forgeflow.recovery.compatibility review \
  --store "$PLAN_STORE" --bundle "$CURRENT_BUNDLE" --plan-id "$PLAN_ID"
```

复核结果的含义：

| 状态 | 操作 |
| --- | --- |
| `completed_history` | 保留原始成功回执和哈希；新说明作为追加文档保存，不重新执行 |
| `current_plan` | 已使用新口径，继续原来的版本、审批和来源约束 |
| `requires_reprepare` | 尚无已开始步骤；由原操作者退役旧比较集，再新建案例、读取新快照并重新批准新计划 |
| `requires_recovery_authorization` | 原 journal 已开始；复核记录绑定当前来源、原始计划、完整步骤、journal 前缀及剩余步骤，需独立恢复授权 |
| `recovery_blocked` | 原计划不能通过正确口径复核；停止剩余写入，保留原记录并交人工处理 |

未执行的旧产品计划退役：

```sh
python -m forgeflow.recovery.compatibility retire \
  --store "$PLAN_STORE" --bundle "$CURRENT_BUNDLE" --plan-id "$PLAN_ID" \
  --approver "$ORIGINAL_APPROVER"
```

该操作保存追加退役记录，保留旧批准文件，防止历史计划退役当前新版本。完成后保留旧案例只读，再通过产品流程新建案例，获取新快照和新计划身份并重新批准。当前没有旧案例原地迁移的 UI 入口，不能对旧案例直接调用 reprepare 冒充迁移；根版本退役不会改写旧 ProductCase 文件。不能改旧 manifest 中的口径标识，也不能把旧批准复制给新计划。

部分执行的恢复授权：

```sh
umask 077
python -m forgeflow.recovery.compatibility authorize \
  --store "$PLAN_STORE" --bundle "$CURRENT_BUNDLE" --plan-id "$PLAN_ID" \
  --review-id "$REVIEW_ID" --approver "$ORIGINAL_APPROVER" > "$PRIVATE_RECOVERY_APPROVAL"
```

授权默认 15 分钟有效，只允许精确复核的原计划继续；来源、journal、剩余步骤或操作者变化会使其失效。运维恢复进程将此 JSON 解析为 `Approval`，使用原 `ExecutionGrant` 调用 `recovery.service.apply(connection, plan_id, store=..., bundle=..., grant=original_grant, require_grant=True, legacy_authorization=approval)`。产品计划还会重新核对原产品版本授权。该接口不暴露给模型或普通产品 API。原部署路径的本地恢复 CLI 支持 `apply --grant ... --legacy-authorization ...`；自定义 store／连接的部署使用受信任运维适配器，不将凭证拼进模型命令。

恢复先核对原编译步骤并回读已开始步骤，再执行剩余动作。不得重新 prepare 后重放全部步骤。复核授权在执行前消费；恢复再次中断时，重新复核当前 journal 并签发新授权，不能修改或复用过期 token。

## 回滚

应用回滚不等于执行数据回滚。新计划不能交给旧代码执行；旧已知错误口径也不能重新开放。保留新版本恢复进程处理其已绑定计划，旧界面仅用于只读历史查看。若不能同时保证来源、版本与剩余步骤绑定，维持只读，列出待处理计划。不得删除 active-plan、journal 或 approval-used 标记来解除阻塞。

## 可选保留优化

`fixed-totals-retention-v1` 是运维配置，不是请求体可任意提交的选项。直接执行器使用 `--retention-profile fixed-totals-retention-v1`；隔离本地 launcher 可用 `FORGEFLOW_RETENTION_PROFILE=fixed-totals-retention-v1`。不设置即关闭。已运行的执行器只在启动时读取该配置；修改后应先 stop 再 resume，并在新准备中确认 manifest 的实际 profile，仅对运行中实例重复 resume 不会切换配置。

开启只影响之后准备的计划。配置写入输入摘要和 manifest；原计划与优化审计共同绑定新计划身份。保留目标先证明原修复值和费用最优，再固定报价采购、工位制造及库存总量优化分配。默认预算 2 秒，未证明改善即回退。成本／产能目标跳过。页面显示 improved／unchanged／fallback／skipped 的实际状态，不把启用配置等同于已改进。

关闭配置同样只影响新准备，不能修改已有审计、批准或计划身份。测试覆盖真实 CP-SAT 4→6 件保留改善、超时／验证失败回退、无改善、配置篡改、候选与追加门槛版本绑定。

## 验收和材料

本轮新的隔离验收位于 [evidence/2026-10-28-next-phase](evidence/2026-10-28-next-phase)。A、B 默认流程、丢响应恢复、实际旧提交产物的离线升级演练分别保存，不能相互替代。原始 reward、公共案例、独立金额核算、业务表摘要与来源哈希按运行保留。

后端完整回归、前端构建／smoke／一致性与 first-look 结果由该目录的验证记录汇总。UI fixture 的截图明确标识用途，真实 ERP 证明来自沙箱记录。历史演示素材保留，新材料清单见 [RECORDING_CORRECTIONS.md](RECORDING_CORRECTIONS.md)。
