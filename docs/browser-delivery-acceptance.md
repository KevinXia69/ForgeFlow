# 浏览器交付验收

`web/e2e/live-delivery.mjs` 用真实 Chromium 操作已构建工作台，登录、建案、输入问题、勾选批准确认并点击执行。它不拦截或模拟业务接口，不通过 `page.request.post` 代替界面操作。只有资源和结果核对使用额外 GET 请求。

## 运行条件

- 明确指定一个全新的独立 QA 工作区，连接真实合成 Odoo 沙箱及已配置的模型；不能借用五位评委的初始环境。
- 固定应用提交及构建文件，保留源码/依赖/产物哈希。验收期间不更新应用。脚本比较实际 HTTP 返回与提供的产物哈希；Git revision 由操作人员提供，不能仅凭这个字段证明后端进程版本。
- 本地安装该前端锁文件对应的依赖，并可启动 Chrome。运行测试脚本不等于运行 GitHub CI。
- 输出目录必须不存在。失败记录保留，脚本不会重发问题、重复批准或重置 ERP。

从私密运行配置向子进程设置以下环境变量，邀请码和浏览器会话文件禁止提交：

| 变量 | 含义 |
| --- | --- |
| `FORGEFLOW_ALLOW_LIVE_TEST=1` | 操作人员已授权该 QA 沙箱中的真实业务写入 |
| `FORGEFLOW_URL` | 指定 QA 工作台的完整 URL |
| `FORGEFLOW_INVITE` | 该 QA 环境的邀请码，无默认值 |
| `FORGEFLOW_TESTED_REVISION` | 目标应用的完整 40 位 Git revision |
| `FORGEFLOW_EXPECTED_ASSETS` | JSON 文件，键为 `static/app` 下相对路径，值为 SHA-256；必须含 `index.html` |
| `FORGEFLOW_SCREENSHOT_DIR` | 新建的私密结果目录 |
| `FORGEFLOW_CHECKPOINT_SCRIPT` | 可选的只读业务快照脚本，接收阶段名参数 |
| `FORGEFLOW_CHECKPOINT_PYTHON` | 快照脚本的 Python 解释器；配置 hook 时必填 |
| `FORGEFLOW_STORAGE_STATE` | 私密浏览器会话文件；配置 hook 时必填，仅供其复用已有登录做 GET |

```sh
# 在已从私密配置加载上述变量的进程中执行；不将凭据写到命令行或仓库。
node web/e2e/live-delivery.mjs
```

可选 hook 接收 `initial`、`after-prepare`、`before-approval`、`after-approval`、`after-reload`，退出码必须为 0，stdout 为 JSON 摘要。它只能读取源码、表快照和现有案例，不能代替浏览器建案、问答或批准。采集器只保留固定摘要字段；原始业务快照单独脱敏。没有配置 hook 时，不能声称已验证数据库表未变。

## 通过条件与证据

1. 首次进入真实工作台，案例为空，执行能力为 live sandbox；实际前端资源匹配冻结产物。
2. 浏览器建案后，方案等待批准，approval/execution 为空。
3. 问题通过有状态 SSE 发送；最终来源为模型，答案绑定同 case/plan/version，并在界面显示来源。
4. 批准弹窗未勾选时按钮禁用；勾选本身不提交请求；点击确认后才执行该版本。
5. 同方案完成真实执行、回读和原始 verifier，界面显示核验成功；不能用模拟检查或缺失证据代替。
6. 刷新后 execution 不变；返回问答仍显示原答案，并提示其依据为较早的待批准状态；恢复不新增模型或业务 POST。

结果保存为 `browser-acceptance.json` 和六张截图。截图遮盖邀请码及批准身份输入框；公开前仍需检查。`browser-session.json`、环境配置及 `failure-private.txt` 永不公开。记录中的批准是经用户授权的自动化浏览器点击，不代表人工可用性研究。

前端收到 SSE 的 `final` 后会取消自己的 reader，Chrome 调试接口可能无法再提供完整响应体。采集器通过原响应的 `clone()` 被动观察最终公开事件，不新增请求、不修改响应内容，也不保存 activity/推理文本。对应的本地浏览器回归：

```sh
node --test web/e2e/live-observer.test.mjs
```

失败时保留阶段、截图和已取得的结果。批准阶段超时可能已写入 ERP；先读回原案例核实，不自动重试。复验应保留前次记录并使用明确安排的独立环境。

公网验收必须单独核对实际部署版本、HTTPS、登录及该版本浏览器行为。评委邀请码提供应用访问权限，不能代替服务器管理权限；仅登录或健康检查成功不能证明新版本已发布。浏览器验收也不代替供应商账单或 Pi 用量实测。

已归档的运行见 [2026-10-27 浏览器交付验收](evidence/2026-10-27-browser-delivery/README.md)，包括首轮采集失败和修正后的独立成功案例。
