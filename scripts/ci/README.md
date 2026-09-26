# E2E：本地与 CI 共用入口

`scripts/ci/run_e2e.py` 是用例选择、并发调度、超时控制和报告生成的唯一入口。它启动 `scripts/a2a/e2e/`、`scripts/repl/e2e/`、`scripts/web/e2e/`、`scripts/pipeline/e2e/` 中已有的验收脚本；已有脚本继续负责场景断言和资源清理。CI 只需检出代码、安装依赖、注入凭证、上传报告。

## 本地执行

在 iac-code 仓库根目录：

```bash
uv sync --locked --extra a2a --group dev
uv run --no-sync python scripts/ci/run_e2e.py --suite fast --jobs 3
uv run --no-sync python scripts/ci/run_e2e.py --suite full --jobs 3
uv run --no-sync python scripts/ci/run_e2e.py --list --suite all
```

`fast` 包含五个 A2A、REPL、Web API 确定性契约用例；`full` 增加 A2A 执行控制矩阵。Web 的浏览器 DOM 验收没有纳入，Web API 用例明确使用 `--skip-browser`。只跑一个用例可用 `--case a2a-success-contract`。`--jobs` 限定为 1–8，默认 3。每个用例用独立子进程、配置目录和日志目录；确定性用例不会继承常见 LLM 与阿里云凭证环境变量。

真实 LLM 与云资源用例显式运行：

```bash
uv run --no-sync python scripts/ci/run_e2e.py --suite live --jobs 2 \
  --credential-source-dir /path/to/test-only-config \
  --allow-cloud-write
```

凭证目录须含 `.credentials.yml`、`.cloud-credentials.yml`、`settings.yml`。建议使用专用测试账号、受限权限和资源配额。该套件包含两个会创建 ROS 资源的场景，以及两个真实模型/云环境场景。已有场景脚本会在完成或收到 SIGTERM 后尝试删除带有测试专用前缀且可证实归属的 Stack。每个真实用例有 45 分钟硬超时，之后最多留 15 分钟清理，再强制结束进程组。若强制结束发生在清理期间，必须检查 `cleanup_status` 和测试账号中的残留资源；不能将硬超时视为自动清理成功。

## CI 接入

CI 调用同一个入口，并用 `--run-dir` 指定报告目录。`fast` 适合快速检查，`full` 适合完整确定性验收。真实场景必须提供测试专用配置目录和 `--allow-cloud-write`。配置文件内容应通过 CI 的 Secret 注入，且不要进入代码、作业输出或上传附件。

`report.md` 是概要表，`report.html` 可展开每个用例，`summary.json` 和各用例 `ci-result.json` 是结构化详情，`junit.xml` 供 CI 测试报告展示。确定性套件可上传 stdout/stderr；真实套件仅上传筛选后的状态文件，不上传原始日志、凭证、工作目录或场景原始 summary。

## 失败复盘

先按 `report.md` 找到失败用例与首次失败检查，再看对应的 `ci-result.json`、场景 `summary.json`、日志和源码。Agent 应做有界复现，明确归类为产品缺陷、用例/断言缺陷、环境/凭证故障、云资源清理故障或超时，并写出证据、受影响场景和建议修复。真实云用例不要为了复现自动再次创建资源；先核对残留资源与清理结果。报告里的“初步线索”只是索引，不能替代复盘结论。

暂未纳入的场景和原因在 `--list --suite all` 及每次报告中列出。扩大真实云用例集前，应先验证对应脚本可无人值守运行、按规定超时退出、清理资源并产生可用报告。
