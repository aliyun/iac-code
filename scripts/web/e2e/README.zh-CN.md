# Web E2E 工具

## 当前入口

真实资源链接验收使用 [resource_link_workflow/README.zh-CN.md](resource_link_workflow/README.zh-CN.md)。
Luna 负责 Goal 调度，Sol 负责独立验收，GLM 负责单资源执行；支持四槽以及本机 / Mac mini 浏览器。
不要同时启动旧批次调度、watcher 或 heartbeat。

本目录不属于发布产品包。测试状态、截图、用户配置及真实云响应不提交 Git。

## 文件分工

| 类别 | 文件 / 目录 | 是否保留及边界 |
| --- | --- | --- |
| 现行验收框架 | `resource_link_workflow/` | 保留：调度、模型脚本桥、浏览器、独立验收、预算、所有权与清理；其目录和角色指南是当前入口 |
| 确定性 Web 契约回归 | `run_contract_scenario.py`、`verify_contract_dom.mjs` | 保留：真实 Web + 假 provider / 云 transport + 本地浏览器，不访问真实云 |
| 资源链接 UI 回归 | `resource_links.mjs`、`resource_application*`、`resource_incremental.mjs`、`resource_choice_fixture.py` | 保留：渲染、选择、普通/Shift 点击、恢复和增量 DOM；不能当真实控制台验收 |
| 落点与环境检查 | `*_landing.mjs`、`resource_link_*preflight*`、`runtime_env.mjs` | 保留：资源身份、页面加载、环境隔离；本身不产生 PASS |
| 显式真实云只读检查 | `resource_catalog_live.py` / `.mjs` / helpers | 保留：需明确安排，使用真实账号，不由普通 pytest 自动运行 |
| 旧版兼容校验器 | `run_resource_link_acceptance.py`、`resource_link_chrome_batch.mjs`、`qwen_batch_guard.py`、`resource_link_e2e_*` / schemas | 暂保留：已有离线回归及历史证据验证依赖它们；不是新计划入口。旧 guard 只在显式提供任务记录时重放 |
| 专项历史恢复校验器 | `failed_resource_preflight.py`、`recover_*.py`、`sanitize_resource_link_evidence.py` | 暂保留：仍有契约测试和兼容调用，不自动执行，也不能把恢复结果改为 PASS |
| 独立 Web 冒烟 | `web_repl_smoke.mjs`、`web_repl_visual_audit.mjs` | 保留：单独的产品冒烟 / 视觉检查，不参与资源计划调度 |

旧 81 批任务文档、旧派发指南、初版报告、固定 thread 的 heartbeat / queue / watcher 及其专项测试
已原样归档至本机 `docs/resource-link-archive/`；它们不再是当前源码或 CI 的依赖。
旧版总览 README 也保存在该归档中，不能用其中过时的端口、时限或角色指令启动新计划。
设计文档和历史原始证据分别在本机 `docs/design/`、`docs/evidence/`，同样不提交。

现有运行状态仍在 `evidence/resource-link-workflow/`，整个 `evidence/` 被忽略。
不为目录整理移动现有 SQLite、cwd、session、token、Chrome profile 或原始截图。
新运行可以显式指定独立的 `--state` 目录。当前累积结果仍使用
[resource_link_workflow/resource-link-results.html](resource_link_workflow/resource-link-results.html)，不是初版报告。
该报告、历史基线、带执行状态的目录及旧版矩阵/来源索引都是机器本地数据，已从 Git 排除；
更新源码不会移动、清空或改写现有数据。干净 checkout 仅包含中性的
[catalog.example.json](resource_link_workflow/catalog.example.json)，所有验收状态均为未测。
新机器可复制示例为本地 `catalog.json`；需要历史报表或续跑时，必须另行提供真实、独立核验的
`historical-results.json` 和原始证据，不能用测试夹具代替。缺少基线时仍拒绝生成历史验收结果。
离线 pytest 在临时目录构造独立假目录、假回执与假来源包，不读取本机运行数据，也不产生真实 PASS。

## 产品与测试的边界

产品仅包含 `iac_code.services.execution_policy` 的进程内通用扩展接口，默认不安装策略。
普通 CLI / Web 不导入本目录，不根据环境变量动态加载插件，也不包含资源专项测试规则。

测试专用 `resource_link_workflow.web_entry` 在启动真正的产品 CLI 前显式安装
`resource_link_workflow.execution_gate`。门禁、精确云调用、额度与响应绑定删除规则都在测试目录；
测试 Web 仍核对实际加载的产品、门禁和启动器 SHA。部署产品不需要携带这些测试脚本。

## 离线及本地浏览器回归

优先按当前框架 README 的离线测试命令运行。单项确定性契约 E2E：

```sh
uv run python scripts/web/e2e/run_contract_scenario.py --run-dir /tmp/iac-code-contract-e4
```

它启动确定性 provider、Aliyun transport fixture 和本地 OTLP 接收器，通过 HTTP 创建会话并核对
真实 Chrome DOM、刷新恢复、metadata 隔离和 provider 唯一终态。
`--skip-browser` 只用于后端定位，不是完整 E2E 通过证据。

资源链接浏览器回归复用已安装的 Playwright 和 Chrome，不自动安装浏览器：

```sh
IAC_RESOURCE_TEST_BROWSER="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
IAC_RESOURCE_TEST_NODE_MODULES="/path/to/existing/node_modules" \
node scripts/web/e2e/resource_links.mjs
```

回归使用假云事实和本地控制台夹具，不证明真实账号权限、登录或控制台页面支持 iframe。
真实云检查及创建、清理必须按现行框架独立审批和验收；配置就绪、HTTP200、模型总结都不是 PASS。
