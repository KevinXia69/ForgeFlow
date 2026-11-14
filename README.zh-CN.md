# ForgeFlow

**从采购或生产异常，到经过核验的 ERP 变更。**

ForgeFlow 将 AI 问答、资料检索、约束规划、人工审批和执行回读连接到同一个工作台。
你可以调查供应商取消或生产中断，审阅具体恢复方案，批准明确的动作，再检查 ERP 中
实际发生的变更。安装包在本机启动 Odoo 20、数据库、知识服务和后台任务。

[打开公网工作台](https://forgeflowagent.com/app/) ·
[本地安装](#本地安装) ·
[基准结果](#基准结果) ·
[一个恢复案例](#一个恢复案例) ·
[架构](#架构) ·
[源码安装](#源码安装) ·
[English](README.md)

<p align="center">
  <img src="docs/images/local-workbench-home.png" alt="ForgeFlow 工作台首页" width="32%">
  <img src="assets/readme/forgeflow-recovery-case-en.png" alt="供应商恢复案例：3 件失效供给、13 件关联需求、50 件计划覆盖" width="32%">
  <img src="assets/readme/forgeflow-architecture-en.png" alt="ForgeFlow 恢复架构：Router、调查 Worker、CP-SAT、人工批准、Executor、Verifier 和 Control Engine" width="32%">
</p>

*工作台、恢复案例和架构一览；同一产品同时支持公网和本地运行。*

## 公网还是本地

ForgeFlow 有两种运行形态，核心产品流程相同：

| 形态 | 适合 | 数据与模型 |
| --- | --- | --- |
| **公网工作台** | 不安装 Docker，直接评审产品 | 项目提供的访问凭据和预配置模型 |
| **本地安装** | 在自己的电脑上运行 | 本机 Odoo 20、PostgreSQL、Qdrant 数据；用户自己的服务 Key |

公网和本地实例不共享数据。两边都使用同一套案例、调查、规划、审批、执行和独立回读流程。

## ForgeFlow 交付什么

| 能力 | 业务结果 |
| --- | --- |
| **证据驱动的调查** | 把异常与受影响订单、现有供给、库存和相关业务规则关联起来。 |
| **约束驱动的恢复规划** | 使用 OR-Tools CP-SAT，在选定场景的数量、时间、资源和经济约束下计算采购与生产方案。 |
| **绑定批准的受控执行** | 把具体方案绑定到一次授权决策，然后执行方案中记录的业务动作。 |
| **独立的 ERP 验证** | 从 ERP 回读执行后的记录，并保持方案、执行日志和验证结果之间的关联。 |

## 基准结果

**两项选定的 ERP-Bench 恢复任务均为 100/100。**

| 恢复任务 | 原始 verifier 得分 | 通过的适用规则 |
| --- | ---: | ---: |
| 供应商取消 — 任务 `2280` | **100 / 100** | **51 / 51** |
| 工作中心停机 — 任务 `2299` | **100 / 100** | **95 / 95** |

*已记录的产品审批工作流验收 · Odoo 19 合成场景 ·
2026 年 10 月 · 被评估源码 `bf880f2` · 原始 ERP-Bench verifier。*

[供应商取消报告][a-report] ·
[供应商取消原始得分][a-score] ·
[工作中心停机报告][b-report] ·
[工作中心停机原始得分][b-score]

这些基准记录保存在一个固定的公开提交中。当前源码树包含的是 Odoo 20 集成；表中标识的是这些历史结果所使用的环境。

## 一个恢复案例

某供应商取消了一笔 **3 件**的采购，该采购关联到一张 **13 件**的销售订单。恢复方案需要协调 **5 张销售订单、共 50 件**的需求。

ForgeFlow 替换失效采购，同时保留仍然有效的供给安排：

**计划供给：** 成品库存 **7 件** + 保留制造 **40 件** + 替代采购 **3 件** = **50 件**。

对于直接关联的订单，方案用组合后的成品库存、保留制造和替代供给覆盖全部 **13 件**。

批准后的恢复产生 **2 个采购单据动作** 和 **7 个已验证执行步骤**，随后得到 **51/51** 的原始 verifier 结果。

[已记录案例与方案][case-record] · [执行回执][a-execution]

## 架构

ForgeFlow 把模型驱动的调查、数值规划和业务执行分开。控制层持久化任务进度，并校验从一个阶段进入下一个阶段的权限。

工作台助手通过只读工具解释受业务范围约束的证据。调查运行时可以生成结构化的方案请求。CP-SAT 求解数值约束，受控执行路径应用已批准的动作。

源码入口：
[Router 与 Worker](src/forgeflow/superagent/workers.py) ·
[方案准备、执行与验证](src/forgeflow/superagent/plan_effects.py) ·
[API 与工作台操作](src/forgeflow/api/app.py)

## 知识与业务事实

文档知识和 ERP 记录承担互补的职责。文档提供政策与产品背景；结构化 ERP 查询确定恢复决策所需的标识、数量、日期和状态。

源码包含文档处理和语义检索模块。语义回答适配器在检索前应用服务端解析的租户、文档代次和访问控制范围，并支持配置的 embedding 与 reranking 服务。

[受范围约束的语义回答](src/forgeflow/knowledge/semantic_answer.py)

## 本地安装

先安装并启动 Docker：Windows/macOS 使用 Docker Desktop，Linux 使用 Docker Engine
和 Compose v2。Docker 必须运行 Linux 容器；主机不需要另装 Python 或 Node.js。
建议使用 Engine 28 或更新版本，网络隔离边界见[安装指南](docs/LOCAL_INSTALL.md#before-starting)。

| 系统 | 下载 | 完整解压后的入口 |
| --- | --- | --- |
| Windows | [ForgeFlow-v3.11.0-Windows.zip](https://github.com/KevinXia69/ForgeFlow/releases/download/v3.11.0/ForgeFlow-v3.11.0-Windows.zip) | 双击 `Start-ForgeFlow.cmd` |
| macOS | [ForgeFlow-v3.11.0-macOS.zip](https://github.com/KevinXia69/ForgeFlow/releases/download/v3.11.0/ForgeFlow-v3.11.0-macOS.zip) | 打开 `Start-ForgeFlow.command` |
| Linux | [ForgeFlow-v3.11.0-Linux.zip](https://github.com/KevinXia69/ForgeFlow/releases/download/v3.11.0/ForgeFlow-v3.11.0-Linux.zip) | 执行 `bash start.sh` |

启动器会打开一次性本地配置页。填入自己的服务 Key，保存页面生成的私密邀请码，
等待服务就绪后访问 **[127.0.0.1:17800/app/](http://127.0.0.1:17800/app/)** 登录。
无需复制公网邀请码、数据库或他人的凭据。

首次启动需要联网下载或构建镜像、初始化独立合成 ERP，可能需要数分钟。
公开镜像无法拉取时，完整发行包可从随包源码构建，不要求私有镜像仓库账号。

**发布门禁：** 只有 Windows、macOS、Linux 原生启动器检查，以及 Linux amd64、
arm64 真实空栈测试全部通过，才会生成可下载的发行包。启动器检查使用 Docker
替身；空栈测试使用合成 ERP 数据和外部服务夹具，不代表每个供应商账号、每台
Docker Desktop 主机都已验收。具体见[验证范围](docs/LOCAL_INSTALL.md#installation-acceptance)。
尚未发布的源码版本不一定已完成这些门禁。

## 公网即用，本地填写自己的 Key

[公网工作台](https://forgeflowagent.com/app/) 默认提供现有 DeepSeek V4.1 Flash 1017
及 Max 推理，评委登录后无需填写模型 Key。设置中的个人模型配置是可选项，
仅对当前标签页生效，不会替换平台默认模型。

本地安装时需要配置自己的服务：

| 服务 | Key | 用途 |
| --- | --- | --- |
| 对话与调查 | 选择支持的服务商或本地 Ollama | 模型推理、工具调用和声明的图片能力 |
| 联网搜索 | DeepSeek | 官方搜索工具的辅助模型；官方 DeepSeek 对话可复用同一个 Key |
| 文档处理 | DashScope | DocumentMind 企业资料解析 |
| 语义检索 | SiliconFlow | 向量化与重排序 |

官方 DeepSeek 对话共需三种 Key；其他云端对话服务还需独立的 DeepSeek 搜索 Key，
合计四种。Ollama 本地模型无需对话服务 Key，但搜索、文档和语义检索仍需要。
每次联网搜索也会产生 DeepSeek 辅助模型用量。

本地支持 DeepSeek、Nebius、OpenAI、Anthropic、Google/Gemini、OpenRouter、
DashScope/Qwen、SiliconFlow、Ollama 和自定义端点，提供四类协议路径。
具体兼容组合和图片/工具能力限制见[安装指南](docs/LOCAL_INSTALL.md#provider-and-protocol-matrix)。

保存配置前必须通过真实的对话连接与工具调用测试，该测试可能产生模型用量。
它不代表独立的文档解析、语义检索或联网搜索账号也已通过验证。

Qdrant、PostgreSQL 和 Odoo 在本机运行，内部密码由安装器生成。模型和文档服务会
使用你自己的 API 账号并产生相应费用；本地部署不等于所有推理离线运行。

## 本地能力

- **带证据的问答：** 流式回答、临时文件/图片附件、可打开原文版本的引用。
- **企业知识库：** 上传资料、查看解析状态与原文块、检索已发布且有权访问的资料。
- **三条示例入口：** 采购制度参考、明确标注的资料参数对照、历史合成恢复示例。
- **ERP 与恢复规划：** 查询采购、销售、库存、BOM 和生产事实，处理受支持的多产品采购、
  多层 BOM 和多个制造订单恢复。
- **人工审批和执行：** 审批绑定具体版本与动作，执行后保留逐操作回执和独立回读结果。
- **持续任务：** 来源轮询、调查调度、任务状态和异常结果持久保存。

聊天不能审批或执行。安装后的演示业务数据是合成数据，经过审批的动作会实际写入本地
Odoo；标注为历史示例的页面保持只读。自动物料替代、任意 ERP 即插即用和全工厂排程
不属于已支持范围。[详细能力边界](docs/en/current-capabilities.md)。

## 停止、改 Key 和升级

Windows 使用 `Stop-ForgeFlow.cmd`，macOS/Linux 使用 `bash stop.sh`。
停止脚本等待正在进行的工作安全结束，保留所有 Docker 命名卷。再次启动沿用原数据。

更新 Key 使用 `Start-ForgeFlow.cmd -Configure` 或 `bash start.sh --configure`。
留空会保留已有 Key；切换模型服务商时需要填写对应 Key。

升级前先成功停止旧版本、备份命名卷，并阅读新版本迁移说明。没有自动数据库回滚；
不要通过删除卷或运行 `down -v` 来修复启动问题。配置、数据库、资料和本地任务均属于
需要保留的安装状态。

## 隐私和证据

凭据保存在权限受限的本地 Docker 配置卷中，**并非静态加密**。提示词、相关证据和
上传资料可能发送给相应 API 服务。不要公开配置链接、邀请码、配置卷、业务备份或带
敏感内容的截图。发行包不包含生产数据库、真实上传资料或服务 Key。

历史 Odoo 19 合成实验在两个选定 ERP-Bench 任务上记录了 100/100、51/51 与 95/95
原始核验规则通过。它们是特定版本的历史研究证据，不代替本安装包的 Odoo 20 验收，
也不证明客户生产可用性或实际节省金额。[历史记录与来源](#基准结果)。

## 开源许可

ForgeFlow 原创代码采用 [Apache License 2.0](LICENSE)。第三方项目和资料保留各自许可，
见 [NOTICE](NOTICE)、[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 及参考包说明。
