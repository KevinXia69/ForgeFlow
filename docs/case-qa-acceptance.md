# 案例问答实施与验收报告

更新日期：2026-10-26（America/Los_Angeles）。本报告对应《ForgeFlow 下一阶段开发方案：中文问答取证与准确性》的 T01–T16，实施基线为 `c42063a95f6bb7104d1c37e3e0be83d843f3b112`。

代码、本地定向回归和 **38 次真实请求（第一轮 15＋第二轮 15＋中文界面 3＋末轮补充 5）**已完成。第二轮、界面及末轮最终回答覆盖了有限任务集所问的核心数量、成本、执行及交付边界，返回引用值均与案例原字段一致；末轮两个成本问使用明确标注的确定性回退。仍存在机械误拒、自由文本精度和额外陈述引用覆盖的限制，第一轮错误原样保留。本报告不表示已完成公网发布验证，也不宣称模型具有通用的 100% 准确率。

## 实施范围与证据

本轮补齐服务端共享事实提取与覆盖状态、同回合不可变证据及权限检查、有界只读工具、复合问题回答与事实校验、真实来源标签、中文业务资料和语言传递。两套前端均区分模型、确定性和本地回答，保留历史版本说明；模型空引用不会被本地引用填满，相关案例入口与具体字段依据分开。

- 原五问、预期事实及禁止推导固定在 [验收问题契约](../tests/fixtures/case_qa_acceptance.json)。这些是检查标准，不是改写后的模型输出。
- 历史案例为 [approved-case.json](../web/e2e/fixtures/approved-case.json)，文件 SHA-256 为 `8a4dad17cddc44f51feaa082d2d679f57a4a7f164d501de0195d7a2688e9576d`。数值/状态变体使用独立内存副本，测试检查源文件未被修改。
- [本轮运行时公开案例](evidence/2026-10-26-case-qa/runtime-public-case.json) 的内容指纹为 `15d33d33ed24d4a71bd3a5d2ae7416fd12151a04f7bb308d9778f128806f4691`。使用 UTF-8 的 `json.dumps(case, sort_keys=True, ensure_ascii=False, default=str)`、默认分隔符独立重算后，与全部 38 条响应一致。公开投影可含派生展示/能力字段；原 fixture 文件哈希与运行时内容指纹不是同一种标识。
- 原始记录分别保存为 [第一轮](evidence/2026-10-26-case-qa/round1.json)、[第二轮](evidence/2026-10-26-case-qa/round2.json)、[中文界面三问](evidence/2026-10-26-case-qa/ui.json)、[末轮补充](evidence/2026-10-26-case-qa/final.json)。[验收汇总](evidence/2026-10-26-case-qa/summary.json) 记录运行配置与本地验证。
- 独立语义复核分别见 [第一轮审查](evidence/2026-10-26-case-qa/manual-review-round1.json)、[第二轮审查](evidence/2026-10-26-case-qa/manual-review-round2.json)、[界面三问审查](evidence/2026-10-26-case-qa/manual-review-ui.json)、[末轮审查](evidence/2026-10-26-case-qa/manual-review-final.json)。审查对照问题、原始模型回答、最终响应及字段原值，不读取隐藏推理，不用机械检查的通过结果替代语义判断；审查由协作审查代理执行，不冒称人工签署。

真实采集使用隔离的本地工作台和历史合成案例，模型为 `nvidia/Nemotron-3.5-Super-VL`，运行路径为 **DSH CLI fallback**。SDK 的本地适配契约检查不能替代该 CLI 路径的真实结果，也不表示已完成 SDK 真模型端到端验收。本次问答采集没有执行 ERP 写入，没有重建正式评委基线。所有记录中的既有执行、回读和验收均属于历史案例。

## 本地验证结果

| 检查 | 已执行结果 | 范围与限制 |
|---|---|---|
| 最终 Python 定向回归 | 28 个文件，**435 passed，0 failed**，23.99 s | 覆盖事实、回答、API、权限、版本、工具预算、取消/重连、中文检索及回归；包含 CLI 取消与进程回收新增 6 例。测试中的模型返回用替身，业务执行使用 FakeProductExecutor 或临时记录；不是 435 次真实模型调用 |
| MCP 来源路径补充回归 | 新增防止记录内容覆盖主机生成 `source_field` 的 1 例后，工具套件 **19 passed** | 已被最终 435 项覆盖，不能再相加宣称新的不重复总数 |
| Node 契约与组件渲染 | **42 passed，0 failed** | 包括历史版本、两套字段/语言测试及 21 项 React SSR 检查；SSR 使用真实组件逻辑，但 ChatSurface、Markdown 和活动面板是无副作用替身，不等同浏览器点击或布局测试 |
| 生产构建 | `web`、`web-public` 均通过，含 TypeScript 类型检查 | Vite 仍提示已有大 chunk；这不是性能 SLA 验证 |
| diff 检查 | 通过 | 未运行 CI，未以测试数推导模型准确率 |

Python 最终日志为本轮运行目录的 `local-python-tests-release.txt`，Node 日志为 `local-node-tests.txt`；稳定结果随[验收汇总](evidence/2026-10-26-case-qa/summary.json)归档。Python 存在 1 条 Starlette/TestClient 依赖弃用提示，未影响本轮结果。较早的 221/423 项及 SDK/StreamHub 29 项检查已被上述更完整回归覆盖，不另行相加。

审计期间还修复了以下前端边界：本地答案延迟到需要降级时才计算；`execution.writes` 缺失显示未记录而不崩溃；选择冲突即使有历史 execution 也不展示旧计划作为新答案；授权、案例不存在、回合冲突等服务器拒绝直接报错，不用旧案例伪造本地成功。本地降级仅接受网络失败或 405/501 不支持接口，普通 ask 失败不会被误作流式兼容分支重复发送。

CLI 取消路径也补齐了排队与启动前检查、静默/已有输出子进程的终止和回收、超时回收，以及分片中文输出只产生一次最终事件。取消后不再发送已缓冲的最终回答；这些本地子进程检查不能证明供应商已终止其远端模型计算。

## T01–T16 验收对应

下表区分“方案中的必需真实任务集”和“额外验证”。本阶段没有要求 T01–T16 的每一种故障变体都再调用一次真实模型；缺失、权限、取消、篡改等路径主要由可重复的本地语义/接口检查验收。可选扩展不是本阶段遗漏，公网项属于后续发布检查。

| 用例 | 本地证据与结论 | 已有真实任务证据／后续类别 |
|---|---|---|
| T01 原始数量复合问 | [test_answer_evidence.py](../tests/test_answer_evidence.py)：大案例摘要省略单据后仍取得取消/替代各 3 件、单位与角色；前端兜底有同样依据 | **必需任务已采集**：两轮各含两次独立原问及同会话首问；新 UI 数量页。第二轮未出现用差额代替数量 |
| T02 明细追问 | 原字段逐项核对 4 张 MO、5 张 PO；明细问不能只给汇总，范围表达不自动判缺失 | **必需任务已采集**：两轮独立明细问及同会话追问；第一轮范围误拒和第二轮量词歧义分别保留 |
| T03 成本 | [test_case_qa_acceptance_matrix.py](../tests/test_case_qa_acceptance_matrix.py)、[test_case_planned_cost.py](../tests/test_case_planned_cost.py)：四金额各归原字段，保留成本、显式零与未知、正差额不是节省 | **必需任务已采集**：原成本问、四金额改写问、新 UI 成本页及末轮两种成本问。末轮最终事实完整，仍有保守误拒，明确标为 deterministic |
| T04 状态与范围 | 历史时间、5 单/50 台、执行状态、客户交付分开；失效供给不等于当前未恢复缺口 | **必需任务已采集**：两轮各两次状态原问，均区分已记录执行与实际客户交付 |
| T05 执行与验收 | 两条动作与对应单据、51/51、原始验收范围核对；[test_api_evidence_scope.py](../tests/test_api_evidence_scope.py) 区分严格与模拟验收 | **必需任务已采集**：原执行问、交付改写问、新 UI 执行页；末轮交付复查为 model，准确区分四种证据 |
| T06 单位与层级 | 组件变为 99 kg 仍保留物料/单位；汇总按产品、角色和单位分组，不生成跨物料总缺口 | **必需改写任务已采集**：“先列保留，再取消/替代，不同物料不相加”。更复杂 kg/件真模型组合属于可选扩展 |
| T07 真正缺失 | 缺失字段、空数组、显式 0、截断、暂不可核对分别断言，原记录保持不变 | 本地验收完成；真实缺失字段变体可扩展，不将其写作未完成的必需真实请求 |
| T08 截断与分页 | 字符预算、行预算、超大单行、累计工具字节；[test_evidence_snapshot_tools.py](../tests/test_evidence_snapshot_tools.py) 验证分页、原始索引和不可覆盖的 `source_field` | 本地验收完成；**末轮真实 MCP 两次成功读取 5＋4＝9 条**，原字段、分页完整性及版本匹配。其他真实读取失败组合可扩展，不能用 mock 充当模型取证 |
| T09 执行未知与待批准 | [test_case_qa_unknown.py](../tests/test_case_qa_unknown.py)：未知不写成 0 次；无明细与空明细有别；pending/冲突不暴露旧计划为已确认方案；不重复 apply | 本地验收完成；更多未知/待批准浏览器组合为可选扩展 |
| T10 失败、超时与取消 | [test_api_stream_turns.py](../tests/test_api_stream_turns.py)、[test_dsh_sdk.py](../tests/test_dsh_sdk.py)、[test_dsh_harness.py](../tests/test_dsh_harness.py)、[test_stream_hub.py](../tests/test_stream_hub.py)：排队取消不新发请求、CLI 子进程回收、租约失效、重放不重执行、不重复最终事件；SSR resume 失败不伪装成功 | 本地验收完成；上游已发出的请求能否被供应商终止并未证明，不能由本地取消结果推出。更多真实供应商错误组合为可选扩展 |
| T11 版本变化 | 内容不可变、同 ID 改内容生成新指纹、篡改检测、权限撤销使旧版失效；[历史版本测试](../web/e2e/case-answer-context.test.mjs)及 SSR 固定旧正文/字段/版本 | 全部 38 条响应的指纹与上下文已核对；第一轮引用错绑另列，第二轮、UI 及末轮字段值匹配。多次切版的真实浏览器巡检可扩展；不影响已完成的本地版本验收 |
| T12 用户范围与内容注入 | [test_case_qa_content_boundaries.py](../tests/test_case_qa_content_boundaries.py)：恶意来源文字不能选 uid/path/scope 或获得写工具；嵌套私有字段不出现在投影/检索/事实中；双用户 API 与撤权回归 | 本地能力/过滤边界验收完成。未宣称任意真实模型都能抵抗所有内容注入；更广泛真实对抗语料属于扩展 |
| T13 引用与降级 | [test_case_answer_sources.py](../tests/test_case_answer_sources.py)、双端 [SSR 测试](../web/e2e/case-answer-render.test.mjs)：模型空引用仍空、三种来源、相关入口分区、合法字段与版本、旧消息 unknown、真实活动不伪造 | 两轮保留模型与确定性模式；UI 三问均记录 model。第二轮及 UI 引用值全一致；第一轮两个派生计数错绑仍保留为失败记录 |
| T14 中文与英文 | JSON/stream 传 lang、同 turn 语言冲突、英文单号不改变显式中文；`web` 中英与 `web-public` 英文契约均通过 | 中文真实任务及 UI 已完成；**末轮英文等价问为 model，状态、订单数/产品量及交付边界正确**。当前 `web-public` 不被描述为已有中文界面 |
| T15 通用中文检索 | [test_chinese_business_knowledge.py](../tests/test_chinese_business_knowledge.py)、[test_knowledge_locale.py](../tests/test_knowledge_locale.py)：5 份中文文档、正确引用行号、无关问题无依据、中英文范围分离 | 文档检索与案例事实问答分别完成本地验收；额外真实文档问答 UI 巡检可扩展。通用文档不能证明实时案例单据事实 |
| T16 数值与状态变化 | 取消/替代 8/9、前端 17/19/0、金额/单号、订单数/79 台、失败状态/7/8 规则等变体随源字段改变；源 fixture 哈希不变 | 本地验收完成；真任务集有 3 个顺序/同义改写，未把固定演示数字视为模型常识。更多真实数值变体可扩展 |

## 真实问答与失败记录

每轮 15 条由原五问各 2 次独立请求（10 条）、原同会话数量问与明细追问（2 条）、顺序或同义改写（3 条）组成。界面三问是另一次实际采集，不借用独立请求的输出冒充截图中的回答。原始失败及前后轮记录分别保存，没有覆盖。

| 批次 | 已完成结果 | 必须保留的限制 |
|---|---|---|
| 第一轮 15 条 | 所有问题均已保存原始模型回答和最终响应，并完成独立语义复核 | **不是通过批次**：`independent-1-q4` 把新建采购与既有采购增加额 0 混同且被放行；`independent-2-q1`、`paraphrase-2` 被误拒；两个确定性响应把 `plan.actions` 数组引用成派生值 2；还有覆盖状态混用、计划/实绩歧义及两条最终未满足三句话 |
| 第二轮 15 条 | 最终 13 条 model、2 条 deterministic；未发现必答核心数量/成本/交付结论错误或子问题遗漏；全部字段引用原值匹配 | `paraphrase-2` 的正确成本回答仍被误拒；`independent-1-q2` 用“1件、共3 Units”指单据数有歧义，降级有合理性。`paraphrase-3` 把 `succeeded` 引成 `succeed`，且“还有8天”未明确历史口径；部分额外陈述虽真实，却未附对应字段引用。不能据此称自由文本逐字、所有额外断言均通过 |
| 中文界面 3 条 | 记录均为 model；同一运行时快照；数量、成本、执行页的 77/10/31 项引用全部与原字段一致 | 选定的 PPT 结论可用；成本回答的“沉淀成本”不如“沉没成本”准确，80.0 margin 缺百分号且不是实际利润。不将这类附加表述作为成果 |
| 末轮补充 5 条 | 3 条 model、2 条 deterministic；最终必答事实完整；10/10/21/28/77 项引用均匹配；真实 MCP 两页完整取得 9 条记录 | 两个成本原文核心事实正确，仍被保守降级；原成本问原文没有显式说明保留安排计入，最终回退已补充。误拒限制未消失，不把该批次描述为模型全部通过 |

同框统计：**38 次真实请求＝15＋15＋3＋5**，均保留原始模型回答；最终来源共 **31 条 model、7 条 deterministic**，这是来源分布而非准确率。只有末轮 `tool-pagination` 发生实际 MCP 读取，合计 **2 次成功调用**。主机准备事实、相关案例入口、字段引用与重复累积的活动 trace 不计作模型工具调用。

以上语义审查同时区分原始模型回答和最终输出。正确的引用不能抵消错误正文，事实正确的回答也可能被机械规则误拒。“原始模型正确”“最终回答正确”“引用绑定正确”“额外陈述引用完整”“语言与格式遵从”是不同指标，不合并成一个泛化准确率。

可用于本轮 PPT 的明确结论为：取消与替代计划各 3 件，并保留 4 张制造单、5 张采购单；方案规划成本较原安排**增加 160.05 美元**，不是节省；记录中两项采购单据变更及适用规则 51/51 通过、回读已验证，**仍不能证明订单已交付或客户已签收**。不把 80.0 margin、规划收益或旧执行记录包装为本轮新增财务收益或 ERP 操作。

## 末轮 5 项真实补充复查

五项均已执行并独立复核原文、最终回答、字段原值及快照。没有发现最终必答核心事实错误或子问题遗漏；两项成本回退符合方案允许的确定性路径，来源明确、事实完整，不为追求模型模式继续反复重试。此结论只限于本批输入与配置，不覆盖早期失败，也不推导通用准确率。

| 项目／记录 ID | 最终来源 | 核对结果与限制 |
|---|---|---|
| 原成本问题 `retry-cost` | deterministic | 最终三句话给出四金额、增加而非节省、保留安排计入及真实成本/完整费用未知。原文采购 3,464.94＋制造 1,330.0、基线和差额均正确，但对保留安排的范围解释不够显式；没有发现数值矛盾，仍触发保守拒绝 |
| 四金额与费用 `retry-four-costs` | deterministic | 4,634.89／4,794.94／1,432.32／160.05 含义正确；取消费/税费未知。原文也正确区分既有采购增量 0 与新建采购金额，语义复核认定为误拒；最终没有遗漏必答事实 |
| 客户交付 `retry-delivery` | model | 未记录客户收货；51/51 是原始任务验收，7 步回读不是交付证明；两条单据各 3 Units，不等于 ERP 事务数；替代编号明确为计划标识 |
| 英文等价 `english-status` | model | 英文输出 `succeeded`／`verified`、5 张销售订单、50 台产品正确；历史执行与实际客户交付明确分开。额外出现替代计划编号，未显式补充正式 ERP 单号提示，但未冒称正式编号 |
| 真实 MCP 分页 `tool-pagination` | model | 两次 `get_case` 均完成；offset 0 取 5 条、offset 5 取 4 条，total 9；末页 complete=true、next_offset=null。九个源索引无遗漏或重复，编号/物料/数量/单位逐项匹配；同物料合计 40／31／22／76，未跨物料相加；取消/替代各 3 Units |

两页 MCP 的 case_id、plan_id、version 均一致，`content_revision` 同为 `bc1650000f204575933dedb6c1a6b7d79ecd446ec4d5a31aaa7f4d7fb637ff6a`。每行主机生成的 `source_field` 对应 `plan.costs.lines.0` 至 `.8`，去除该路径注记后的内容与源记录完整相等。该指纹针对工具可见证据包，与整份公开案例的 `evidence_usage.snapshot_id` 指纹范围不同，二者不要求数值相同。分页活动的页大小与偏移支持每页 5 条的陈述；最终活动记录没有单独暴露原始参数对象，不额外声称验证了未记录的参数元数据。

输入、原始输出、最终模式、证据指纹和实际活动保存于[末轮原始记录](evidence/2026-10-26-case-qa/final.json)，独立结论和逐字段核对见[末轮审查](evidence/2026-10-26-case-qa/manual-review-final.json)。两条成本的 `model_evidence_mismatch` 状态如实保留；语义审查没有读取机械规则日志，因此不把内部标签匹配的具体原因冒称为独立验证结论。

## 后续发布检查与复跑

公网登录、隔离、问答和引用检查属于后续发布步骤，应使用单独 QA 工作区；本报告不以重置正式评委环境或重新批准真实 ERP 写入为条件。更广泛的真实模型对抗输入、异构单位大案例、多状态浏览器巡检以及供应商异常组合是扩展验证，不能倒写为本轮已经覆盖。

必要本地检查可分别重跑，不启动 CI：

```sh
python -m pytest -q tests/test_answer_evidence.py tests/test_case_qa_acceptance_matrix.py tests/test_case_qa_content_boundaries.py tests/test_case_answer_sources.py tests/test_evidence_snapshot_tools.py
node --test web/e2e/case-answer-context.test.mjs web/e2e/case-answer-evidence.test.mjs web-public/e2e/case-answer-evidence.test.mjs web/e2e/case-answer-render.test.mjs web-public/e2e/case-answer-render.test.mjs
npm --prefix web run build
npm --prefix web-public run build
```

有限任务集的事实覆盖目标不转化为通用成功率。降级应继续明确标注；若发现关键事实错误、引用错绑或权限问题，应保存失败证据并重跑受影响问题，不能通过修改历史案例或隐藏失败使验收看起来通过。
