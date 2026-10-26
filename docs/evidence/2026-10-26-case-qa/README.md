# 中文案例问答验收证据

本目录保存 2026-10-26 本地开发期间的真实模型输出、最终 API 响应、独立语义复核及界面素材。完整实施说明见 [验收报告](../../case-qa-acceptance.md)，接口定义见 [证据契约](../../case-answer-evidence-contract.md)。

| 文件 | 内容 |
|---|---|
| [summary.json](summary.json) | 38 次真实请求、本地检查、模型与后端、范围与限制 |
| [round1.json](round1.json) / [round2.json](round2.json) | 两轮各 15 条，包含原五问各两次独立请求、同会话追问及三个改写问题 |
| [ui.json](ui.json) | 实际中文界面三问；与两轮独立请求分别采集 |
| [final.json](final.json) | 最终冻结实现上的成本、交付、英文和真实 MCP 分页复验，共 5 条 |
| [失败账本](failure-ledger.md) | 哪些错误已修、哪些表达仍会误拒，以及旧记录为何保留 |
| manual-review-*.json | 对照问题、原始回答、最终回答和源字段的独立审查；由协作审查代理完成，不是人工签署 |
| [browser-review.json](browser-review.json) | 实际点击引用、查看覆盖状态、跳转案例、刷新历史的记录 |
| ui-*.png | 工作台原始截图；本地、历史合成案例、本轮无 ERP 写入 |
| [runtime-public-case.json](runtime-public-case.json) | 可独立核对引用的公开案例投影 |
| [provenance.json](provenance.json) / [final-code-hashes.json](final-code-hashes.json) | 历史来源、运行方式与末轮实现文件哈希 |
| local-*-tests.txt | 本地定向检查日志，未运行 CI |

`raw_model_answer` 是供应商运行路径返回的正文，`response.answer` 是校验后用户看到的最终正文。两者可能不同；确定性降级有独立 `source_mode` 和原因。记录保留正文而不公开隐藏推理、会话凭据或私有运行目录。会话日志哈希用于本地归档比对，不表示原始日志全文公开。

第一轮包含关键错误，不能算通过批次。第二轮及界面来自当时开发版本；末轮才对应 `final-code-hashes.json` 的冻结实现。38 条记录合计 31 条 model、7 条 deterministic，**这是来源分布，不是模型正确率**。最终两条成本原文的核心事实正确但被保守规则拒绝，用户得到的是有明确标签、事实完整的确定性回答。

全部响应绑定同一案例内容指纹。重算方式为：对公开案例执行 Python `json.dumps(case, ensure_ascii=False, sort_keys=True, default=str)`，使用默认分隔符、UTF-8 编码且不追加换行，再计算 SHA-256。案例指纹和只读工具作用域的 `content_revision` 是不同层次的标识，不能互换；工具两页的作用域版本相同。

真实模型为 Nebius 的 `nvidia/Nemotron-3.5-Super-VL`，经 DSH CLI fallback 运行。SDK 仅完成本地适配契约检查。验证期间没有新增 ERP 写入、没有使用正式评委工作区，也没有发布公网更新。这些记录不证明真实客户交付、实际经营收益、生产 SLA 或任意问题的正确率。
