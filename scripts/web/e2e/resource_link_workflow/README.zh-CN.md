# 可复用资源链接测试框架

## 三份主文档

- [最终结果](resource-link-results.html)：按服务 → 资源类型 → 来源子行展示 iframe / 直接打开结果；保留 510 条历史入口记录。最终结果只由历史证据和 Sol 验收回执更新。
- [测试矩阵](resource-link-test-matrix.html)：87 种常见资源，固定编号 R0001–R0087，每种四个来源维度。支持状态初始为 unknown，必须根据官方证据确认，不把空计划当“不支持”。
- [测试指南](TEST_GUIDE.zh-CN.md)：从准备、创建到截图验收、清理的统一标准。

机器目录是 [catalog.json](catalog.json)。历史基线是 [historical-results.json](historical-results.json)，不能手工把状态改为通过。初版报告和旧 81 批记录已原样归档至本机 docs/resource-link-archive/，不再提交，也不是运行依赖。

`catalog.json`、历史基线、两份 HTML 报告及旧版执行矩阵/来源登记都是本机运行产物，不提交 Git。
干净 checkout 只附带 [catalog.example.json](catalog.example.json)：资源身份与候选 API 可复用，
支持状态为 unknown，创建未验证，所有点击均为 untested。首次运行可将其复制为本地 `catalog.json`；
续跑必须保留原目录、历史基线、SHA 绑定与原始证据，不能覆盖为示例或离线测试夹具。
报表缺少真实历史基线时仍失败关闭，不把示例或源码测试结果当验收通过。
离线测试数据由 `tests/web/resource_link_data.py` 在临时目录独立生成，与真实验收记录无关。

可复用程序、角色指南、目录、历史基线及两张 HTML 统一位于 scripts/web/e2e/resource_link_workflow/，纳入 Git。docs/ 用于本地设计、历史归档或单次任务文档，不作为运行依赖。某次计划、SQLite 状态、日志、截图和私有配置放 evidence/resource-link-workflow/ 或指定 --state 目录，整个 evidence/ 被忽略，不提交凭据及浏览器登录态。

产品包仅保留 iac_code.services.execution_policy 的通用进程内接口，默认无策略，不导入测试框架。
测试专用 web_entry 在启动真正的产品 CLI 前显式安装 execution_gate；专项规则、调用账及清理约束
均在本目录。Web 的实际加载清单同时核验产品接口、门禁和启动器；不能用普通 Web 冒充测试 Web。

## 架构

| 部件 | 数量 | 职责 / 程序限制 |
| --- | --- | --- |
| Luna（GPT-6 Luna / high） | 每个计划一个桌面可见会话 | 只读 next，执行唯一动作 ID；不准备计划、不验收、不改状态文件；同一状态库不能有两个活动调度者 |
| Sol（GPT-6.1 Sol / high） | 每个计划一个持久、桌面可见会话 | 串行准备 / 决策 / 产品修复 / 逐图验收；同一计划内不换，新计划可以换；修复期间停止新派发并排空 GLM |
| GLM | 每个编号资源一个会话 | 同资源重试接续该会话，但每次自建新的正式 Web 会话；只补缺失场景 |
| 临时 Server | 一个 | SQLite 事务、唯一资源所有者、单 Sol 租约、四槽、动作幂等、预算、依赖和互斥锁 |
| iac-code Web | 默认四个 | 16100–16103，独立进程、cwd、config、会话存储；不是共享一个 Web 的四标签 |
| Chrome | 所选机器上一个专用 profile | 四个精确 targetId；同机器共享国内站登录。两台机器各保留自己的登录态，不能全局 close/goto |
| OPS | 本监督会话 | 启停、修复和恢复临时 Server；持久库/会话/证据不随进程重启重建 |

程序在 scripts/web/e2e/resource_link_workflow/。Server 默认 16099，仅绑定 loopback；私有描述文件 0600。SQLite 状态、原始截图、回执和资源所有权都在独立 --state 目录中，必须保留。不能把“服务就绪”“GLM 报告完成”当通过。

## 运行入口

在仓库根目录执行，以下命令不创建云资源。默认 init 保持暂停。

运行依赖：项目 uv/Python 3.10+、Node.js 22+（WebSocket）、Playwright、Google Chrome、Aliyun CLI、现有百炼控制器和本机 Codex app-server。Node/Playwright 可复用 Codex 桌面自带依赖，实际路径填私有 environment.json，不写死在仓库。

```sh
uv run python -m scripts.web.e2e.resource_link_workflow init
uv run python -m scripts.web.e2e.resource_link_workflow configure scripts/web/e2e/resource_link_workflow/environment.example.json
uv run python -m scripts.web.e2e.resource_link_workflow plan --id review-v1 --resources R0001 --output evidence/review-v1.json
uv run python -m scripts.web.e2e.resource_link_workflow import-plan evidence/review-v1.json --budget-cents 0
uv run python -m scripts.web.e2e.resource_link_workflow status
```

environment.example.json 的路径须先按本机修改。先读 [OPS](OPS.zh-CN.md) 配置隔离凭据、模型后端、Chrome、四个 Web 和国内站登录，再 claim 切换派发所有权。claim 直接核对旧 guard 的持久 lease，不读取归档任务文档；旧 guard prepare/backfill 随后硬拒绝，与新流程不会双调度。

配置完成后，resume → next → execute <action_id> → next。next 会自动恢复同一临时 Server；动作只执行一次，不重试未知 new。需要暂停用 pause，暂停只阻止新动作，不隐瞒已运行任务和云资源。不要在此框架之外另开 watcher 或调度器。

## 模型适配与能力边界

GLM 固定 glm-5.3-prime / xhigh，支持本机 codex-bailian-control 和 Mac mini 的 codex-alilang-macmini 后端。两种模式均不调用控制器完全访问权限的 new 默认路径：本机适配器连接所选 app-server，启用普通终端和看图，不注册 dynamicTools。模型运行 workflow-agent.py，脚本经任务专用 loopback 桥调用原 Server；原授权、租约、调用账和验收仍由程序核验。Sol 固定 gpt-6.1-sol / high，每个测试计划绑定一个本机 thread，不跨计划永久共用；历史计划始终保留原绑定。

模型 sandbox 为当前 job workspace-write / network / never：仅该 job 工作目录可写（排除通用临时目录），网络为 loopback 脚本桥所需。它不是云 IAM/网络防火墙；模型仍不得直接操作云、浏览器、SSH 或修改原状态。客户端私有令牌只代表本 job，不交付 Server 总令牌或云凭据。inspect 图片复制校验原始 SHA 后由原生 view_image 查看，不借路径或 JSON 验收。旧 thread 保留原历史，但不再使用遗留动态工具，也不为抢工具订阅反复归档会话。

| 模式 | GLM / Chrome | 四个 Web、Server、SQLite、Sol、截图文件 |
| --- | --- | --- |
| local（默认） | 本机百炼 / 本机专用 Chrome9225 | 本机；Web16100–16103 |
| macmini | Mac mini 百炼 / Mac mini 专用 Chrome9225 | 仍在本机；通过 SSH 转发访问，不搬 Web 或验收状态 |

Mac mini 模式由程序管理一个 SSH 通道：本机4510 → Mini4500（GLM），本机9226 → Mini9225（CDP）；Mini16100–16103 → 本机同端口 Web。全部请求使用 loopback，Web 的裸 origin 不变。截图由本机 Playwright 从远端 Chrome 取回并写入原本机 OUTPUT_DIR，不依赖模型自行 SFTP。每个 job 在提交前同时冻结数据库、launch 和原始 transport 的后端；旧回合不随当前模式切换，原调用累计数不重置。

脚本模式额外由 adapter 管理本 job 的临时反向通道（SSH 自动分配独立远端 loopback 端口），四槽不会共用客户端令牌/监听器。只部署标准库 Python 客户端和 0600 私有描述文件，不复制 Web、云配置或总令牌。adapter 退出/崩溃时通道随父进程管道 EOF 回收；通道未知不自动重连或重发操作。连接设置限时只用于启用前检查，不限制模型/资源执行总时长。

配置与切换步骤见 [OPS：双执行机器](OPS.zh-CN.md#双执行机器web-始终在本机)。只允许暂停、排空回合/槽位、处理完原资源后切换；本机仍是默认模式。离线测试验证绑定、转发与故障拒绝，不代表 Mac mini 的模型、浏览器或云测试已经实机通过。

模型与 effort 的统一配置为 [roles.json](roles.json)：Luna / Sol 均 high，GLM 为 xhigh。Sol 和 GLM adapter 直接读取该配置，创建持久线程并设置可识别名称。Luna 通过桌面创建接口显式指定同样的配置。

桌面可见是正式启动门禁，不是 thread/start 返回 ID 就算完成。Luna、Sol 必须在主 Codex 桌面版列表中可见并能打开；GLM 在百炼桌面版。当前独立 Sol app-server 的实时桌面发现仍需实机验证，未确认时不能宣称已接入桌面。优先使用桌面共享后端，不把孤立 CODEX_HOME 下的隐藏线程当正式 Sol。

本版本支持本机 macOS/Linux 进程所有权检查；Windows OPS 进程适配尚未实现，明确 fail closed。模型端点、脚本执行/返回策略和真实 Web/云行为需要在正式执行前实机验证；离线测试不声称覆盖真实 LLM 或云端。这次实现不自动恢复已删除的 luna，不启动收费云测试。

创建能力默认关闭，但**不要求另建受限 RAM 身份**，可复用现有 test-guima（包括管理员身份）。用户允许免费/按量可删除资源不等于任意写权限：OPS 审核并冻结精确 API、完整参数/模板、地域、创建次数、成本及清理方案。创建前产品用同一实际签名凭据调用 STS，程序核对批准账号；模型无法改白名单。

隔离 Web 启动时强制加载执行门禁：只有当前完整 session key 的精确结构化 aliyun_api 调用可执行；Shell、MCP、文件写入、专用部署捷径和其他工具不能绕行。云写调用前持久预占一次机会，未知结果不重发，Server/Web 重启不清零。删除只绑定本任务真实创建响应中的 ID，且只在 cleanup 阶段启用。普通产品进程不设置测试门禁环境变量，行为不变。

ROS 和 ROS 托管 Terraform 仍走真实 Web 产品的 ROS CreateStack/DeleteStack API，但模板须先审，禁 TemplateURL/动态文件加载。当前不放行 ros_deploy/本机 Terraform/Shell 捷径；若产品选了这些入口，应修执行输入，不能关闭门禁。此门禁是测试工作流约束，不是假称云端 IAM 已受限，也不是云账单系统；收费项仍须实际官方成本审核和用户预算授权。

## 分工指南

[Luna](LUNA.zh-CN.md) · [GLM](GLM.zh-CN.md) · [Sol](SOL.zh-CN.md) · [新计划](PLAN_GENERATION.zh-CN.md) · [OPS](OPS.zh-CN.md)

新增资源只追加永久 ID；未测资源可生成新 plan_id。历史链接 PASS 不重放，创建链未核验则仍单独排创建项。HTML 是视图，不是调度真相；SQLite 和原始回执才是运行状态。

## 离线验证

```sh
uv run pytest -q tests/web/test_resource_link_workflow.py tests/web/test_qwen_batch_guard.py
uv run pytest -q tests/web/test_resource_link_agent_client.py
node --test tests/web/resource_link_workflow.test.mjs tests/web/resource_link_agent_bridge.test.mjs
uv run ruff check scripts/web/e2e/resource_link_workflow tests/web/test_resource_link_workflow.py
```

覆盖四槽互斥与依赖、单 Sol、旧调度互锁、冻结 PASS、截图 SHA、清理门禁、未知提交、模拟模型 transport、真实本地 Server 重启和表格生成。测试不使用真实模型、用户凭据或云 API。正式启动前仍须完成一次真实模型接口与专用 Web 环境烟测。
