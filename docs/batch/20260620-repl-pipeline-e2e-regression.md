# REPL Pipeline E2E Regression

## 背景

本次将 `codex/auto-test-repl` 分支上的真实 REPL pipeline 回归测试能力 cherry-pick 回主工作区，用于补齐交互式终端入口的端到端回归覆盖。该覆盖与既有 A2A E2E runner 目标一致，都是验证 pipeline 行为，但入口改为真实 REPL / PTY 黑盒交互。

## 本次做了什么

- 新增 `scripts/repl/e2e/run_pipeline_scenarios.py`，通过 PTY 启动真实 `iac_code.cli.main`，默认使用当前用户真实 `~/.iac-code` 配置、真实 LLM provider 和真实阿里云工具链。
- 新增 `scripts/repl/e2e/README.zh-CN.md`，记录真实 REPL 回归的运行方式、场景列表、产物和验收标准。
- 新增 `tests/repl_e2e/test_run_pipeline_scenarios.py`，覆盖 runner 的参数校验、脱敏、scenario dispatch、acceptance check、自动 teardown、图片 fixture paste 等纯 helper 逻辑；该 pytest 不启动真实 REPL、不调用真实 LLM 或云账号。
- 补充 REPL 图片场景，runner 复用 `scripts/a2a/e2e/fixtures/text-images/` 的静态 PNG，通过真实 bracketed paste 把图片路径送入 REPL，让普通图片粘贴路径生成 `ImageBlock`。
- 为会创建真实 ROS stack 的非 cleanup 场景加入 test-owned StackName 追踪和自动 teardown，要求 observed stack name 必须匹配 runner 注入的 `iac-e2e-*` 名称，避免误删非本轮测试资源。
- 为 cleanup 场景加入真实 ROS 状态对账：回滚后旧 stack 必须进入 cleanup target 并被删除，新 stack 必须保留，最终 runner 再删除测试拥有的剩余 stack。
- 收紧 pipeline 部署与 cleanup 相关提示和测试，保留通用产品安全约束，避免把具体 e2e 场景里的固定资源名作为产品逻辑依赖。

## 覆盖场景

真实 runner 覆盖 16 个 REPL 场景：

- `scenario1`
- `ask-waiting`
- `ask-waiting-resume`
- `image-initial`
- `image-ask-waiting-resume`
- `image-selection-waiting-resume`
- `image-normal-handoff`
- `image-interrupt`
- `selection-waiting-resume`
- `selection-invalid-then-valid`
- `evaluate-resume`
- `rollback-step2`
- `rollback-step3`
- `rollback-step4-selection`
- `rollback-step5-cleanup`
- `rollback-step5-cleanup-recovery`

## 验收口径

每个场景不是只看进程退出码，而是要求 `summary.json` 中所有 `checks` 都为 `true`。通用验收包括：

- 必须捕获 PTY transcript。
- transcript 中不能出现 traceback、pexpect EOF/TIMEOUT、权限拒绝等终端错误。
- 创建资源的场景必须在 cleanup ledger 中观察到 ROS `CreateStack` 资源，并确认 StackName 为 runner 注入的 test-owned 名称。
- cleanup 场景必须同时检查 PTY 文本、cleanup ledger 和真实 ROS GetStack 状态。
- runner 必须完成最终 teardown，并在删除前校验云端 StackName 属于本轮测试。

## 运行命令

真实回归需要显式带 `--allow-real-cloud`：

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/repl/e2e/run_pipeline_scenarios.py \
  --allow-real-cloud \
  --scenario scenario1 \
  --scenario ask-waiting \
  --scenario ask-waiting-resume \
  --scenario image-initial \
  --scenario image-ask-waiting-resume \
  --scenario image-selection-waiting-resume \
  --scenario image-normal-handoff \
  --scenario image-interrupt \
  --scenario selection-waiting-resume \
  --scenario selection-invalid-then-valid \
  --scenario evaluate-resume \
  --scenario rollback-step2 \
  --scenario rollback-step3 \
  --scenario rollback-step4-selection \
  --scenario rollback-step5-cleanup \
  --scenario rollback-step5-cleanup-recovery
```

日常单元测试只验证 runner helper：

```bash
uv run pytest tests/repl_e2e/test_run_pipeline_scenarios.py -q
```
