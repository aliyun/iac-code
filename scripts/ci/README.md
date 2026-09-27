# E2E：本地与 CI 共用入口

`scripts/ci/run_e2e.py` 是用例选择、并发调度、超时控制和报告生成的唯一入口。它启动 `scripts/a2a/e2e/`、`scripts/repl/e2e/`、`scripts/web/e2e/`、`scripts/pipeline/e2e/` 中已有的验收脚本；已有脚本继续负责场景断言和资源清理。CI 只需检出代码、安装依赖、注入凭证、上传报告。

## 本地执行

在 iac-code 仓库根目录：

```bash
uv sync --locked --extra a2a --extra agui --group dev
uv run --no-sync python scripts/ci/run_e2e.py --suite fast --jobs 3
uv run --no-sync python scripts/ci/run_e2e.py --suite full --jobs 3
uv run --no-sync python scripts/ci/run_e2e.py --list --suite all
```

`fast` 包含 6 个 A2A、REPL、Web API 确定性契约用例，包含修复后的 `e3a-recovery`；`full` 共 42 个，另含 A2A 执行控制与权限等待/恢复矩阵。`e3a-recovery` 在已完成回合的安全交接点验证重启恢复；执行中快照的接管由执行控制拒绝。Web API 用例明确使用 `--skip-browser`，不启动浏览器。只跑一个用例可用 `--case a2a-recovery-contract`。`--jobs` 限定为 1–8，默认 3。每个用例用独立子进程、配置目录和日志目录；确定性用例不会继承常见 LLM 与阿里云凭证环境变量。

所有用例子进程都设置 `IAC_CODE_TELEMETRY_E2E_USER_ID`，保证遥测中的 `user.id` 带有 `e2e`，供报表排除测试流量；真实用例同时在复制后的 `settings.yml` 中保留或写入该 ID。入口清除继承的 OTLP 导出目标。仅 A2A、REPL、Web 契约及 Selling、只读 canary 等明确使用 `ObserveCapture` 验证埋点的用例设置 `IAC_CODE_TELEMETRY_LOCAL_ONLY=1`，只向本机临时接收器发送；其他用例不配置本机接收器，使用正常远端遥测。即使源码版的 `__release_date__` 为空，显式的 E2E 用户 ID 也允许这些普通用例正常上报。

真实 LLM 与云资源用例显式运行：

```bash
uv run --no-sync python scripts/ci/run_e2e.py --suite live --jobs 4 \
  --credential-source-dir /path/to/test-only-config \
  --allow-cloud-write
```

默认三文件模式下，凭证目录须含 `.credentials.yml`、`.cloud-credentials.yml`、`settings.yml`。建议使用专用测试账号、受限权限和资源配额。`live` 共 103 个：42 个 selling flow A2A/REPL 场景、8 个只读资源选择、16 个 REPL 旧场景、33 个 A2A 恢复场景、3 个 VPC 模板 smoke、1 个只读云 API canary。其中包含真实 ROS 资源创建用例。需要缩小范围时可选 `live-core`、`live-recovery`、`live-multimodal`、`live-readonly`、`live-legacy`、`live-safety`、`live-repl` 或 `live-smoke`，也可用 `--case` 指定单个用例。三个 VPC smoke 只接收 LLM 与 settings 配置，使用隔离的 HOME，不接收云凭证；它们验证生成模板，不创建 VPC。并行 selling 场景各用独立的 `10.250.0.0/16` 子网池，避免独立进程重复预留同一 VSwitch CIDR；原 runner 单独运行仍沿用原池。31 个旧 A2A 恢复场景在 CI 中使用本次运行专属的 StackName，并在结束时核验和删除该名称下的 Stack；硬超时后由独立清理进程再次尝试。selling、REPL 旧用例和 A2A 恢复用例的硬超时为 45 分钟，之后最多留 15 分钟清理，再强制结束进程组。硬超时不代表清理成功，需检查报告和测试账号残留资源。

CI 可改用 `--cloud-credential-helper /path/to/helper.py`，此时源目录只需 LLM 凭证和 `settings.yml`。Helper 接口为 `cloud --output <目标 .cloud-credentials.yml 路径>`；它在每个真实云用例开始前调用，长用例每 10 分钟调用一次，异常清理前也会再次调用。若 helper 需要独立 Python 环境，传 `--cloud-credential-python /path/to/python`。Helper 必须原子地写入私有权限文件，且不得在标准输出或错误输出打印凭证。模板 smoke 不调用 helper。本地原有的三文件运行方式继续支持。

## CI 接入

CI 调用同一个入口，并用 `--run-dir` 指定报告目录。`fast` 适合快速检查，`full` 适合完整确定性验收。真实场景必须提供测试专用配置目录和 `--allow-cloud-write`。配置文件内容应通过 CI 的 Secret 注入，且不要进入代码、作业输出或上传附件。

`report.md` 是概要表，`report.html` 可展开每个用例，`summary.json` 和各用例 `ci-result.json` 是结构化详情，`junit.xml` 供 CI 测试报告展示。确定性套件可上传 stdout/stderr；真实套件仅上传筛选后的状态文件，不上传原始日志、凭证、工作目录或场景原始 summary。

## 失败复盘

先按 `report.md` 找到失败用例与首次失败检查，再看对应的 `ci-result.json`、场景 `summary.json`、日志和源码。Agent 应做有界复现，明确归类为产品缺陷、用例/断言缺陷、环境/凭证故障、云资源清理故障或超时，并写出证据、受影响场景和建议修复。真实云用例不要为了复现自动再次创建资源；先核对残留资源与清理结果。报告里的“初步线索”只是索引，不能替代复盘结论。

总入口目前登记 145 个场景。暂未纳入的场景和原因在 `--list --suite all` 及每次报告中列出：Selling Web/Desktop、StartChat 权限等待、Qoder MCP 重连和浏览器 DOM 场景。Aone CI 没有浏览器；若未来提供浏览器运行机，再单独验证浏览器场景。真实用例在测试专用 Secret 配置后才能完成 CI 实跑验收。
