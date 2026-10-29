# P2 保留优化复核

复核日期：2026-10-28（本地时区）。范围：`fixed-totals-retention-v1`、候选目标反例、来源与审批绑定。基线是 `74a990a51545d7a619b6b39179bd357d803c7396`；来源清单修复及回归已提交于最终代码 `bf880f2f1a8881809a6da2d89bcd3ba6724f4f81`。复核的 retention、service、bundle 及三份相关测试文件已逐一核对，与该提交字节相同。未修改 ERP-Bench，上游仍为 `ceba3880af555129b5278e056a0c20f2fb5a0ba9`。

结论：在限定模型范围内实现了真实的保留量优化；默认关闭，不应描述为全局最少 ERP 修改。复核发现并修复一个可选模块来源清单缺口，修复后相关 118 项测试通过。

| 核查项 | 代码与证据 | 结论 |
| --- | --- | --- |
| 原求解最优性 | `retention.py::_proven_optima` 要求 repair 模型及 primary、spend secondary 最后一次求解均为 `OPTIMAL`。新增测试分别将真实 trace 的这两阶段改为 `FEASIBLE`，均跳过且不启动 refinement。 | 只有可证明的原修复/费用最优解进入实验。 |
| 固定目标与总量 | `_joint_model` 固定原 repair expression、精确分币费用、每个 offer 采购量、每个工位制造量、各品项库存使用量；仍使用上游可行性和故障路径排除约束。提取后再次比较固定值，独立重建并校验 change set。 | 保留优化不能用更高费用、更多制造或改变修复目标换取结果。 |
| 真正改变分配 | 两交期真实 CP-SAT 例子：早需求 4、晚需求 6、库存 2、原晚 MO 6。原保留 4；优化后库存分给早需求，保留 6，新建量明确断言为 2；修复值 6、费用 56、总量不变。另有三组不同数量验证。 | 不是重排展示，也不是写死样例。 |
| 时间与材料 | 原 MO 只能流向允许交期；材料必须早于实际开工到达。把旧 MO 开工提前到材料到达前时，实际保留量仍为 0。一次 CP-SAT 求解，默认预算 2 秒，扣除建模时间，无重试。 | 预算指建模和该次求解；不承诺整个 prepare（原校验、映射等）的墙钟上限。 |
| 回退与证据 | 无改善、预算耗尽、未证最优、材料/映射或独立校验失败，保留原 `plan` 内容。audit 深复制完整 `original_plan` 并记录匹配的 SHA-256；禁止对已 refinement 的输入重复嵌套。 | 回退不伪报改进；原计划无效则拒绝，不把错误原计划包装成成功。 |
| 绑定与审批 | profile 进入准备输入摘要、manifest 和新的 plan identity；apply 重验 profile、原计划 audit、全部 artifact hashes。测试覆盖修订、候选选择、重启、旧身份拒绝及重复审批不重写。 | 即使业务 plan 相同，新的 audit/profile 仍形成新身份，不能借旧审批执行。 |
| 打包 | recovery bundle 包含 retention 和必要 recovery 模块；通用 solver/schema 来自上游哈希白名单，不包含 task initializer、reference solution 或 verifier。隔离 Python `-I` 子进程只加载解包 bundle，仍完成真实 4→6。 | 运行时依赖不靠开发 checkout 偶然可导入。 |

复核发现的来源绑定问题：打包时有 `retention.py`，但原 `verify_sources` 的必需集合没有条件要求这个可选文件。只从临时 provenance 删除其哈希条目、保留文件字节，开启 profile 的普通及候选 prepare 原先都成功。修复新增 `service._require_retention_sources`，在两种 prepare 读取 ERP 前、persist 前，以及 apply 校验身份后要求该条目。回归同时证明：遗漏条目时 prepare 不读取 ERP；自洽的新 plan ID、manifest 和 provenance 仍不能绕过 apply；默认未启用 profile 的 bundle 保持兼容。已完成的 `B/` 验收及其绑定 bundle 保留原样，明确属于修复前基线，不将其重新标记成修复后验收。

工作包 B 的三候选反例使用同一虚构快照和 pinned 真实求解器。在 `test_three_profiles_are_real_distinct_choices_and_capacity_selection_is_bound` 中，直接读取持久化 change set 的 projected totals，分别断言保留量 `6/0/0`、新建量 `4/10/10`；费用为 `62/50/90`，标准工时为 `30/30/10`，全部最终 MO 排除故障工位 `WC-A`。这证明费用/产能目标改变了取舍，而非默认修复目标的另一种名称。

验证命令（项目根目录，使用已安装依赖的 Python）：

```sh
PYTHONPATH=src:tests python -m pytest -q tests/test_retention_integration.py tests/test_recovery_service.py tests/test_product_candidates.py tests/test_product_capacity_candidates.py tests/test_planning_retention.py
python -m ruff check src/forgeflow/recovery/service.py tests/test_retention_integration.py tests/test_planning_retention.py tests/test_product_capacity_candidates.py
```

结果：`118 passed, 1 warning in 17.54s`；Ruff 全通过。唯一 warning 是既有 Starlette/httpx 弃用提示。测试使用虚构业务数据；真实原始验收另见各沙箱 evidence，不把这些回归测试计入 benchmark 成绩。

最终提交的独立 B 沙箱验收见 `B-final/report.json` 与 `B-final/provenance.json`：默认 `preserve_quantities` 通过原始验收 95/95，规划费用 133,394.07，修复值 184；新支出覆盖 168 件、收入 651,142.80，另有库存覆盖 60 件，独立核算毛利率 79.51385318%。准备、选择和审批前重启均未改变 8 张业务表；执行 23 项业务动作、确认 34 步，重复审批与执行后重启未产生重复。118 个源文件/脚本哈希逐一等于 `bf880f2`，46 个 bundle 文件哈希匹配，运行期间 source 未变。该 HTTP 验收关闭可选 retention，证明默认路径兼容；4→6 的改进与来源缺口拒绝由上面的真实求解和集成回归证明，不能互相替代。旧 `B/` 证据仍属于 `74a990a`，没有覆盖或重置。

限制：固定 offer、工位与库存总量；只支持当前单制造成品模型，未覆盖任意多层制造、跨工位总量变化或全部 ERP 文档编辑数最优。若 solver 保留最优值无法被独立映射实现，回退到原方案。不同生产环境的延迟与求解规模不由该合成测试推断。
