# 下一阶段开发与验收证据

最终业务代码为 `bf880f2f1a8881809a6da2d89bcd3ba6724f4f81`，ERP-Bench 固定为 `ceba3880af555129b5278e056a0c20f2fb5a0ba9`。后续提交只追加验证工具、文档和公开证据。交付见 [PR #2](https://github.com/KevinXia69/ForgeFlow/pull/2)，运维步骤见 [发布与恢复手册](../../NEXT_PHASE_RELEASE.md)。本轮没有合并主分支或更新公开部署。

## 最终默认路径

| 指标 | A-final | B-final |
| --- | --- | --- |
| 全案规划需求 | 5 单、50 件 | 12 单、228 件 |
| 直接关联需求／失效供给 | 13 件／3 件 | 112 件／92 件 |
| 新增支出覆盖数量／现货数量 | 43／7 | 168／60 |
| 新增支出覆盖收入 | 20623.23 | 651142.80 |
| 计划采购与制造费用 | 4794.94 | 133394.07 |
| 新增支出利润率 | 76.74981077% | 79.51385318% |
| 原修复目标值 | 6 | 184 |
| 原始规则与得分 | 40 条约束＋11 条其他适用规则；100 分 | 75 条约束＋20 条其他适用规则；100 分 |
| 业务动作／已确认执行步骤 | 2／7 | 23／34 |

金额均为合成任务的 USD 规划值。采购和制造费用包含保留的单据；客户预算继续使用完整订单销售金额。动作和步骤计数不等于数据库写入次数。两次验收均由明确授权的 HTTP 驱动执行批准，不能称为真人浏览器批准或自主 agent 基准运行。三种目标配置经业务等价去重后，A 有一个、B 有两个不同候选。

- [A 最终报告](A-final/report.json)、[来源核对](A-final/provenance.json)、[原始 reward 字节](A-final/original-reward.raw.json)、[公开执行回执](A-final/executed.json)、[中英问答](A-final/answers.json)。
- [B 最终报告](B-final/report.json)、[来源核对](B-final/provenance.json)、[原始 reward 字节](B-final/original-reward.raw.json)、[公开执行回执](B-final/executed.json)、[中英问答](B-final/answers.json)。

两场景均逐项核对 118 个执行源文件／脚本与最终提交、46 个来源包文件及原始 reward 哈希。审批前、准备后和重启前后的八张业务表摘要证明准备阶段没有业务写入；重复批准及执行后重启保留同一回执和业务表状态。来源包、原计划及私密授权材料保存在私有运行目录，公开报告已去除操作者范围哈希等私密字段。

## 按开发方案核对

| 要求 | 实现与可运行证明 |
| --- | --- |
| 精确分母、完整计划费用、零值边界、严格门槛 | [margin](../../../tests/test_margin.py)、[独立 change-set 检查](../../../tests/test_recovery_change_set.py)、[追加门槛](../../../tests/test_product_constraints.py) |
| 默认、候选、指标 builder/projector、主卡与问答一致 | [decision metrics](../../../tests/test_decision_metrics.py)、[产品投影](../../../tests/test_margin_product_projection.py)、[版本与展示边界](../../../tests/test_margin_presentation.py)、A/B 最终中英问答与回执 |
| 历史可读、旧批准不能继续新执行、部分执行受控恢复 | [14 项兼容回归](../../../tests/test_margin_compatibility.py)、[实际旧提交产物的升级演练](legacy-upgrade-final/report.json)、[来源核对](legacy-upgrade-final/source-verification.json) |
| 三目标实际行为与故障排除 | [三候选回归](../../../tests/test_product_capacity_candidates.py)：真实锁定求解器的持久化投影保留量 6／0／0，新建量 4／10／10，费用 62／50／90，均排除故障工位 |
| 范围、日期、动作与步骤、历史文档入口 | [影响范围回归](../../../tests/test_impact_scope_summary.py)、[截图清单](../../media/next-phase/screenshots-manifest.json)、当前中英能力文档与指南 |
| 默认真实 ERP 验收、重放与重启 | A-final、B-final 报告中的业务表摘要、原始规则、批准重放与两次 stop/resume 验证 |
| 真实 ERP 丢响应后恢复 | [recovery-final](recovery-final/report.json)、[来源核对](recovery-final/source-verification.json)、[v2 指标](recovery-final/margin-v2.json)：一次授权、一次 apply、七步骤，重复 reconciliation 复用同一原始验收回执 |
| 固定原目标与费用后的保留改进 | [真实 CP-SAT 测试](../../../tests/test_planning_retention.py)、[产品与来源绑定集成](../../../tests/test_retention_integration.py)：旧量 4→6、新建量 4→2，原修复值 6、费用 56 不变；含无改善、未证明最优、超时、校验失败回退和批准重放 |
| 全量回归与前端检查 | [后端记录](validation/backend.json)：3062 通过、40 条环境／历史素材条件跳过、28 子测试通过；[前端记录](validation/frontend.json)：两包构建／类型检查、纯逻辑测试、smoke、一致性及实际布局验证 |
| 独立审查与部署说明 | [后端复核](review-backend.md)、[保留优化复核](review-retention.md)、[发布与恢复手册](../../NEXT_PHASE_RELEASE.md) |

Ruff 全部通过。40 条后端跳过项的逐项原因在 [完整日志](validation/backend-bf880f2.txt) 中，包括 Linux 专属所有权／peer credential 检查、缺少历史运行素材和未构建的可选 Pi smoke 副本。前端 first-look 的 11 项检查来自首轮 `74a990a`；最终 CSS 另有双包构建及 A/B × 中英 × 1440／390 的八组真实布局验证，不把这些不同范围合并宣称为同一次运行。

## 恢复证据的边界

`recovery-final` 在真实 ERP 完整 apply 返回后丢弃响应，再回读原执行结果，证明丢响应恢复与幂等性。`legacy-upgrade-final` 使用实际旧提交 `c42063a` 生成的 manifest、批准与 journal，在合成 ERP 中覆盖六类状态，包括写到一半的 journal 恢复、错误分母阻断、未执行退役重算和回滚只读。后者不是一次真实 ERP 跨版本部分执行迁移；两份证据不能互相替代。

旧 ProductCase 没有原地 UI 迁移。运维退役旧根计划后保留旧案例，只读审计，再新建案例获取新快照和新批准。P2 默认关闭；本轮证明了真实求解器和产品集成，没有宣称开启实验后的真实 ERP 执行成绩或全局最少写入。

## 历史与本轮范围

`A/`、`B/`、`recovery/`、`legacy-upgrade/` 保留首轮 `74a990a` 证据；对应 `*-final/` 为最终业务代码结果。A/B 首轮公开报告删除了操作者范围哈希，完整原报告在私有运行目录保留，追加 `publication-provenance.json` 记录脱敏前后哈希；不得把其原始来源证明误认为当前公开投影的字节哈希。

用户明确取消本轮 PPT 和录屏工作；新草稿未交付，历史 PPT／视频和此前原始验收资料均未修改。保留的 [18 张界面截图](../../media/next-phase/README.md) 用于核对字段和布局，其中 A-final 与旧 A/B 布局用例明确分别标注来源。
