# 案例问答版本发布与验收

适用于带时间戳回合 ID 的问答版本。这里记录操作步骤和验收条件，不表示已经发布或完成真实联调。代码提交与公网发布分别记录；本次不启动 CI。

## 冻结同一份后端与前端

发布记录至少包含完整 Git revision、工作树是否干净、所选前端、实际安装的后端路径、构建后 `index.html` 和引用 JS/CSS 的 SHA-256。验收期间不再修改源码或重建前端；修改后应重新记录并复验受影响项。

API 从其已安装 Python 包旁的 `src/forgeflow/api/static/app` 对应目录提供 `/` 和 `/app/`。两套前端只选一套作为该实例的界面：

| 选择 | 在同一冻结 checkout 中构建 | 实际输出 |
| --- | --- | --- |
| `web`，中英文工作台 | `npm --prefix web ci`，然后 `npm --prefix web run build` | `src/forgeflow/api/static/app` |
| `web-public`，英文工作台 | `npm --prefix web-public ci`，然后 `npm --prefix web-public run build -- --outDir ../src/forgeflow/api/static/app` | `src/forgeflow/api/static/app` |

`web-public` 不带 `--outDir` 的默认输出是 `web-public/dist`，API 不会自动改为提供该目录。不要先构建一套、再把另一套的旧目录复制进发布包。`npm ci` 是按锁文件安装本地依赖，不是运行 GitHub CI。

VPS 通常由同一个 `/opt/forgeflow` checkout 和 Python 环境提供多个实例。`scripts/vps_reviewers.py` 给实例复用 `repo_root`，数据隔离不等于代码目录隔离。因此原地更新时：

1. 先在独立构建目录准备目标 revision，完成本地检查，记录构建文件哈希。确认线上 Python 实际加载的 `forgeflow` 路径，避免更新 checkout 却继续加载旧的非 editable 安装包。
2. 记录当前运行的实例与各自配置，等待现有执行结束。在替换共享源码、安装包或 `static/app` 前，停止所有读取这些路径的历史、QA、评委 API/executor 服务；若网关也加载同一 Python 安装包，一并安排停机。不要在仍运行的实例下覆盖共享文件。正常停止应等待执行器退出，不强制中断 ERP 写入。
3. 将冻结后端及所选前端作为一组安装。构建或安装失败时保持服务停止，恢复上一组匹配的代码与界面。保留配置、session secret、邀请码、case store、`turn-journal`、assistant 历史、ERP 容器/卷及 generation；此次问答升级不初始化、不轮换场景，也不覆盖规划与执行证据。
4. 启动升级前实际运行的服务。先在隔离 QA 工作区检查登录、界面和下面的回合行为，再开放评委访问。核对实际 HTTP 返回的 `index.html` 和引用资源哈希与构建记录一致，核对各实例确实运行目标后端版本。不要仅用健康检查代替配套版本核对。
5. 提醒已打开页面的用户手动刷新，再提交新问题。旧标签页里的 JavaScript 不会因服务端替换自动更新；不能依赖新前端的提示代码去提醒所有旧标签页。

现有 `deploy/Dockerfile.api` 会在同一多阶段构建中装入 `web` 产物和后端，便于整体替换，但它是只读 review 镜像，不应代替 VPS 的可写产品运行配置。VPS 安装器当前没有前后端 revision 握手或自动原子发布机制，以上版本核对与停机顺序由操作人员执行。

## 回合兼容边界

带 `turn_id` 的新流式请求使用 `t1_<毫秒时间戳>_<随机值>`。未知旧格式 ID 返回 `410 turn_id_upgrade_required`，应保留问题并提示刷新，不自动更换 ID 或重发。过期 ID 返回 `410 turn_expired`，缺失的恢复记录返回 `410 turn_unavailable`；这些情况不能显示成已获得答案。

仍可用的旧格式服务端记录允许按原 ID 恢复或停止。已取消回合恢复返回 `409 turn_cancelled`，被清理、缺失或重启时未完成的回合不承诺恢复。恢复只重放原回合，不新开模型调用。不带 `turn_id` 的旧无状态流接口仍兼容，但不应被前端用来绕过有状态回合的拒绝。

Stop 的确认只表示本地取消状态已经记录，不证明供应商已经停止远端推理。确认取消的 ID 即使在 TTL 清理和重启后，也不能作为新回合重新执行。

## 同一版本验收

用独立 QA 工作区与该次发布记录对应，不使用或重置五位评委的初始环境。记录用例、输入、最终响应、原始模型输出（若调用）、来源标签、相关 case/plan/version、运行时间和证据文件哈希。公开副本移除凭据及私有运行路径，失败记录照常保留。

| 项目 | 必须观察到的结果 |
| --- | --- |
| 失败证据问答 | 7/8 验收失败、无验收记录、回读失败均不能显示为已通过/已回读成功；中英文 JSON/SSE 的确定性降级来源明确，历史案例不被改写 |
| 数量与物料角色 | 正确摘要可接受；互换“2 件成品、5 件组件”的数量/角色后不能被放行；不能唯一绑定的摘要保守降级 |
| 协议升级与过期 | 旧格式新回合、过期 ID、缺失恢复记录分别给出对应提示；问题保留且没有自动重发或新增模型调用 |
| 取消、恢复与新回合 | 原 ID 的可恢复记录不重新调用模型；取消后恢复被拒绝；之后用户主动发起的新回合可完成。离开页面、切语言再恢复沿用原回合语言 |
| 取消记录容量 | 单用户/全局额度、仅取消流量的过期清理、重启后的计数有界；达到额度明确拒绝，现有回合仍可停止；已确认取消的延迟请求不重新执行 |
| 用量完整性 | 普通消息和成功压缩均计入，缓存不重复累计；中止、缺失和摘要重试标记不完整，缺失费用不写成零。历史未知用量不补造 |

这些故障组合首先使用本地固定样本和替身回归，不能将测试数当作真实模型调用数。可复用：

```sh
python -m pytest -q tests/test_case_evidence_routes.py tests/test_case_verdict_guards.py tests/test_answer_evidence.py tests/test_api_stream_turns.py tests/test_stream_cancel_capacity.py tests/test_turn_identity_api.py tests/test_pi_usage.py tests/test_runview.py
node --test web/e2e/conversation.test.mjs web-public/e2e/conversation.test.mjs
node web/e2e/case-turn-errors.mjs
node web-public/e2e/case-turn-errors.mjs
node web/e2e/case-turn-language.mjs
node web-public/e2e/case-turn-language.mjs
```

真实联调再保留至少一条本版本的完整业务记录：实际配置的 Nebius/NVIDIA 模型回答、准备方案、用户批准的版本、具体 ERP 单据变更、修改后回读和原始 verifier 结果。用同一 case/plan 串起证据，区分本轮新执行与历史回放；验证重复批准不新增写入。该记录只能证明所执行案例，不能据此推出客户已交付、净收益或完整费用。

已有工具的复用范围：

- `scripts/vps_reviewer_acceptance.py` 的 `prepare`、`model`、`approve`、`isolation`、`restart` 阶段面向 `qa1/qa2`，会真实准备/批准业务任务并将结果保存在 root 私有文件中。新记录使用 schema 2：要求 `--revision` 指定完整提交、干净 checkout、两实例匹配的 QA 身份/端口与独立数据目录；保存全部 Git 跟踪文件、实际 `static/app` 产物哈希以及配置 Python 的包解析路径。每阶段前后比对固定记录，并检查已保存的 ERP generation 和归属。源码、界面、实例目标或代次变化会拒绝继续；文件绑定不代替进程内存证明或公网 HTTP 资源核对。
- `scripts/vps_reviewer_release_check.py stream --case-id ... --output ...` 检查历史案例的真实公网模型流，不批准 ERP 写入，也不证明新回合协议、恢复或本轮 ERP 执行。
- `scripts/vps_gateway_acceptance.py` 可检查登录和隔离。仅未使用过的正式环境才适用 `--fresh`；已使用的环境不要为使检查通过而重置。

每轮选择新的 root 私有状态文件，旧文件保留。`prepare` 要求 QA 案例为空且 generation 未被占用；`--state` 不会重置 ERP。已使用的 QA 必须先按实例维护流程备份并轮换，禁止为了通过检查删除旧案例。操作示例（只适用于已安排的新 QA 环境）：

```sh
FF_RELEASE_REVISION=$(git rev-parse HEAD)
FF_QA_CAPTURE=/root/forgeflow-qa-release-YYYYMMDD.json
.venv/bin/python scripts/vps_reviewer_acceptance.py prepare --revision "$FF_RELEASE_REVISION" --state "$FF_QA_CAPTURE"
.venv/bin/python scripts/vps_reviewer_acceptance.py model --revision "$FF_RELEASE_REVISION" --state "$FF_QA_CAPTURE"
.venv/bin/python scripts/vps_reviewer_acceptance.py approve --revision "$FF_RELEASE_REVISION" --state "$FF_QA_CAPTURE"
.venv/bin/python scripts/vps_reviewer_acceptance.py isolation --revision "$FF_RELEASE_REVISION" --state "$FF_QA_CAPTURE"
.venv/bin/python scripts/vps_reviewer_acceptance.py restart --revision "$FF_RELEASE_REVISION" --state "$FF_QA_CAPTURE"
.venv/bin/python scripts/export_reviewer_evidence.py --source "$FF_QA_CAPTURE" --output /root/forgeflow-qa-public-YYYYMMDD
```

阶段必须顺序完成；失败、中断或已尝试的阶段不会自动重跑。已返回的案例、执行响应、原始 reward 与双实例部分结果会保留。批准超时可能已经写入 ERP，记录中的 `pending_or_unknown` 不能当作未执行，应先按 case/plan 查回执。不要删除状态文件后重复批准来消除失败记录。输出只打印阶段和异常类型，不打印网络异常正文。短时间重复登录受现有登录限额约束，安排阶段间隔时一并考虑。

新 schema 2 导出要求五阶段均通过，并核对同一 case/plan/version、模型答复上下文、批准及原始验收；缺阶段或身份不一致时拒绝发布为成功。公开副本保留版本/文件哈希与最终答案哈希，不包含邀请码、cookie、模型密钥、审批身份或私有运行路径；旧 schema 1 标为历史版本未知，日期取原记录，缺失时为 `null`。导出不表示已核实供应商账单。

归档时分别写明本地回归、浏览器操作、真实模型调用、ERP 执行及公网发布哪些已完成，哪些尚未完成。旧的 38 条问答、旧的 ERP 成绩和本次固定样本不能充当本版本新真实联调记录。

最新本地业务联调见 [2026-10-27 完整业务记录](evidence/2026-10-27-local-full-loop/README.md)。它使用独立本地环境和单案例驱动，不能当作新版 VPS 双实例验收脚本、公网发布或供应商计费核验已完成。
