# selling_solution_first 真实 E2E

本目录是 `selling_solution_first` 的独立真实 E2E 入口，包含 45 个 case。完整 case 清单与验收标准维护在
本文的「Suite 和 45 个 case」章节；这些 case 不会加入普通 `make test`，也不会改变原 `selling` runner
的场景语义。

这些用例会调用真实 LLM、真实阿里云只读 API；标记为 cloud-write 的用例还会创建并清理测试专属 ROS
Stack。运行前请确认当前账号和地域适合执行真实测试。

## 快速开始

列出全部 45 个 case（此命令不读取凭证，也不访问网络）：

```bash
uv run python scripts/pipeline/e2e/selling_solution_first/run_scenarios.py --list-scenarios
```

运行不写云资源的单个用例：

```bash
uv run python scripts/pipeline/e2e/selling_solution_first/run_scenarios.py \
  --scenario a2a-step1-clarify \
  --concurrency 1 \
  --allow-real-cloud
```

运行默认 `smoke` suite。该 suite 包含真实部署 case，所以必须同时确认云写：

```bash
uv run python scripts/pipeline/e2e/selling_solution_first/run_scenarios.py \
  --suite smoke \
  --allow-real-cloud \
  --allow-cloud-write
```

运行全部场景，默认并发度是 3：

```bash
uv run python scripts/pipeline/e2e/selling_solution_first/run_scenarios.py \
  --suite all \
  --concurrency 3 \
  --allow-real-cloud \
  --allow-cloud-write
```

## 前置条件

- 使用仓库依赖环境，推荐先执行 `make install`。
- `~/.iac-code/.credentials.yml` 配置可用的真实 LLM provider。
- `~/.iac-code/.cloud-credentials.yml` 配置可用的阿里云凭证和默认地域。
- REPL case 需要 POSIX PTY 和 `pexpect`。
- Web case 需要 Node.js、Playwright/Chrome；可用 `--skip-browser` 只检查真实 Web API，正式验收不应跳过。
- Desktop case 需要当前平台的已构建 native artifact，以及通过 `--desktop-command` 指定的平台原生 UI
  自动化 driver。仅启动 host 或只跑 frozen sidecar smoke 不足以通过 D01。
- 需要复用已有 VPC 的场景，建议传 `--cleanup-vpc-id`、`--cleanup-vpc-cidr` 和 `--cleanup-zone-id`。

Suite preflight 只运行一次：先用真实 provider 执行最小 normal-chat，再用 ROS `ListStacks` 做只读能力检查，
不创建资源。调试时可传 `--skip-preflight`，正式验收不应跳过。

## 并发、隔离和凭证

`--concurrency` 默认是 3。worker 之间只并发 case，单个 case 的交互保持串行。每个 case 都有独立的：

- `config/` 与 `config-backup/`
- `workspace/`、session 和 pipeline persistence
- A2A/Web 端口
- 预留 CIDR
- `iac-e2e-ssf-<case>-<suffix>` StackName

runner 从 `--credential-source-dir`（默认 `~/.iac-code`）复制 `.credentials.yml` 和
`.cloud-credentials.yml` 到每个 case 的 `config/`。它不复制用户的 projects、memory、logs、state、tasks、
历史或多模态缓存。复制文件不是软链接/硬链接，目录权限为 `0700`，文件权限为 `0600`。

suite 前后会比较源凭证的内容哈希和元数据，但产物只写 `sourceUnchanged` 等布尔结论，不写哈希和凭证内容。
`settings.yml` 默认不复制；显式传 `--inherit-settings` 才会复制。provider/model/API base 可以用对应 CLI 参数覆盖。

同一 suite 中端口和 CIDR 由线程安全分配器统一预留。共享浏览器、Desktop host、回滚 Stack cleanup 等少数
资源使用命名锁，不会让整个 suite 串行化。

## Suite 和 45 个 case

| Suite | Case |
| --- | --- |
| `smoke` | A01、R01、W01 |
| `core` | A01-A08、R01-R06 |
| `recovery` | A09-A23、R07-R13 |
| `multimodal` | A25-A27、R14、W02 |
| `safety` | A02、A10、A11、A18、A22-A24、D01、L01 |
| `web` | W01-W02 |
| `desktop` | D01 |
| `legacy` | L01 |
| `all` | 全部 45 个 case |

### A2A（27）

| ID | 名称 |
| --- | --- |
| A01 | `a2a-happy-multi-plan` |
| A02 | `a2a-safe-quote-cancel` |
| A03 | `a2a-step1-clarify` |
| A04 | `a2a-step1-replan-replace` |
| A05 | `a2a-step2-required-parameter` |
| A06 | `a2a-step2-structured-override` |
| A07 | `a2a-step2-reselect-new-intent` |
| A08 | `a2a-non-aliyun-early-exit` |
| A09 | `a2a-performance-backup-restore` |
| A10 | `a2a-input-during-backup` |
| A11 | `a2a-fault-checkpoints` |
| A12 | `a2a-running-step1` |
| A13 | `a2a-running-step2` |
| A14 | `a2a-running-step3` |
| A15 | `a2a-normal-running` |
| A16 | `a2a-cancel-step1` |
| A17 | `a2a-cancel-step2` |
| A18 | `a2a-cancel-step3` |
| A19 | `a2a-rollback-recovery-step1` |
| A20 | `a2a-rollback-recovery-step2` |
| A21 | `a2a-rollback-recovery-step3` |
| A22 | `a2a-rollback-stack-cleanup` |
| A23 | `a2a-rollback-cleanup-recovery` |
| A24 | `a2a-redaction-contract` |
| A25 | `a2a-image-initial-selection` |
| A26 | `a2a-image-asks-confirmation` |
| A27 | `a2a-image-interrupt-handoff` |

### REPL（14）

| ID | 名称 |
| --- | --- |
| R01 | `repl-single-plan-happy` |
| R02 | `repl-multi-plan-natural-adjust` |
| R03 | `repl-step1-clarify-replan` |
| R04 | `repl-step1-replace-invalid-select` |
| R05 | `repl-step2-required-parameter` |
| R06 | `repl-step2-reselect-progress` |
| R07 | `repl-waiting-resume-all` |
| R08 | `repl-running-step1` |
| R09 | `repl-running-step2` |
| R10 | `repl-running-step3` |
| R11 | `repl-normal-running-cancel-resume` |
| R12 | `repl-interrupt-rollback` |
| R13 | `repl-rollback-cleanup-recovery` |
| R14 | `repl-multimodal-lifecycle` |

### Web、Desktop 和兼容性（4）

| ID | 名称 |
| --- | --- |
| W01 | `web-full-flow` |
| W02 | `web-multimodal-cancel-recovery` |
| D01 | `desktop-native-full-flow` |
| L01 | `legacy-selling-smoke` |

完整流程和逐项验收以本文的 case 清单及 runner 注册表为准。runner 使用 `ScenarioSpec.profile` 将上述
case 映射到共享的 A2A、REPL、Web、Desktop 驱动，避免复制旧的 3000/3900 行 runner。

## 主要参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--scenario` | 空 | 可重复；有显式 case 且未传 suite 时，只运行这些 case。 |
| `--suite` | `smoke` | 可重复；与显式 case 合并去重。 |
| `--concurrency` | `3` | 最大并发 case 数。 |
| `--run-root` | 系统临时目录 | suite 产物根目录。 |
| `--run-dir` | 空 | 仅单 case 且并发度 1。 |
| `--credential-source-dir` | `~/.iac-code` | 只读凭证来源。 |
| `--provider` / `--model` / `--api-base` | 用户配置 | 覆盖 runtime provider 设置。 |
| `--allow-real-cloud` | false | 允许真实阿里云只读调用。 |
| `--allow-cloud-write` | false | 允许 cloud-write case 创建/删除测试 Stack。 |
| `--skip-final-teardown` | false | 调试时保留测试 Stack；使用者自行承担清理责任。 |
| `--fail-fast` | false | 首个失败后停止调度尚未开始的 case。 |
| `--leave-running` | false | 仅单 case/并发 1 调试。 |
| `--desktop-command` | 空 | D01 的平台原生 UI driver 命令。 |
| `--desktop-package-root` | `desktop/dist` | D01 driver 审计的原生安装/打包产物根目录。 |

显式 case 和 suite 合并后按 A01…L01 的注册顺序运行，同一个 case 每条命令最多执行一次。

## 产物和退出码

每个 case 至少生成：

```text
summary.json
events.jsonl
config-audit.json
pipeline-snapshots/
tool-sequence.json
workspace/
templates/
cloud-resources.json
cleanup-result.json
logs/
```

Surface 会额外生成 A2A request/event/state、REPL raw/normalized transcript、Web API/DOM/screenshot 或 Desktop
host/sidecar/package audit。suite 根目录生成：

```text
suite-summary.json
suite-events.jsonl
credential-source-audit.json
.preflight/
```

退出码：全部通过为 `0`；case、cleanup、凭证完整性任一失败为 `1`；参数错误为 `2`；Ctrl+C/SIGTERM 为
`130`。中断时仍会停止子进程、尝试清理 ledger 内测试自有 Stack，并写出已有产物。

删除 ROS Stack 前 runner 必须同时满足：存在 Stack ID、记录的 StackName 与本 case 的完整 test-owned
StackName 精确相等、云端 GetStack 返回的 StackName 也精确相等。任何一项不满足都会拒绝删除并使 case 失败。

### Desktop driver 契约

D01 的原生 UI 自动化因 macOS、Windows 和 AppImage 平台不同，由 `--desktop-command` 指定的 driver 执行。
runner 会向 driver 注入以下文件路径，driver 必须完成整个用例后退出：

- `IAC_CODE_DESKTOP_E2E_RESULT`：结果 JSON。
- `IAC_CODE_DESKTOP_E2E_SCREENSHOT`：平台截图。
- `IAC_CODE_DESKTOP_E2E_HOST_LOG`：Desktop host 日志。
- `IAC_CODE_DESKTOP_E2E_SIDECAR_LOG`：Python sidecar 日志。
- `IAC_CODE_DESKTOP_E2E_PACKAGE_ROOT`：需要审计的原生产物根目录。

结果 JSON 必须声明 `pipelineName: selling_solution_first`、精确三个 `steps`，并将 host/sidecar 启动、候选
选择、candidate/confirmation 两次重启恢复、直接输入调参、取消、三步时间线、方案说明、询价、Desktop
runtime 和 normal handoff 对应的布尔字段置为 true；`cloudWriteObserved` 必须为 false。`packageResources`
还必须逐类确认 YAML、prompts、skills、hooks、tools 和 references 已进入实际打包产物。runner 不接受只保持
进程存活的“伪通过”。

## Runner 单元测试

下面的测试只验证 runner，不调用真实 LLM、云 API、浏览器或 Desktop：

```bash
uv run pytest -q tests/pipeline_e2e/test_selling_solution_first_run_scenarios.py
uv run ruff check scripts/pipeline/e2e/selling_solution_first/run_scenarios.py \
  tests/pipeline_e2e/test_selling_solution_first_run_scenarios.py
```

单元测试覆盖 45-case 注册表、suite、参数、并发上限、fail-fast、config 隔离、凭证复制权限、端口/CIDR、
命名锁、汇总退出码和子进程中断清理。
