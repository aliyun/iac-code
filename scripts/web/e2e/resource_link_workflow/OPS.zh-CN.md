# OPS：临时 Server、环境与故障恢复

已有资源可作为 QUERY 样本，不要求由 GLM 创建。原生 NAS 人工样本恢复使用
`inspect-existing-query TASK --handoff RESOURCE/handoff.json --output RESOURCE/proposal.json`；
在暂停且排空后独立核对真实账号、精确原始响应、依赖和健康状态，再保存 approved 的私有审查文件，
运行 `queue-existing-query --evidence RESOURCE/review.json`。新只读 packet/会话/签名来源独立绑定；
原 packet、调用、额度和所有权不变，不重放已通过项。外部测试资源如实登记人工来源并纳入清理，
既有业务资源不能因为可查询而获得删除授权。原 Luna 负责 next/execute；原 Sol 先看实点原图，
再交接独立清理，全部消失核实后才能最终 close。

同源实点仅缺字段像素时，不重新 QUERY。暂停排空后以实际原生诊断运行
`inspect-existing-query-capture TASK --diagnosis RESOURCE/diagnosis.json --output RESOURCE/proposal.json`，
独立看诊断原图并批准后 `queue-existing-query-capture --evidence RESOURCE/review.json`。
此入口封存原 source/session/实际 occurrence/原 PNG/签名累计调用，由原 Luna 派原 GLM 只补图。
NAS DataFlow 允许原生路径 tooltip、同一唯一行的“修改数据流动”既有元数据表单只读查看禁用
CPFS/Fileset/OSS 字段，然后仅取消；绝不填写、勾选、确定或保存。诊断不是正式验收证据。

OPS 是独立监督会话，允许本任务范围内本机进程及程序绑定的 Mac mini Chrome/SSH 启停、状态备份、修复和离线测试。不能创建第二调度器；不能重建或删除持久状态“解卡”。

当前六资源验证采用用户要求的全程主动护航，不依赖 OPS 或 Luna 的定时任务。唯一 Luna 使用实际 active goal 持续 next/execute；OPS 主动处理原始证据/输入/授权及故障，真实工作等待使用有界等待并持续跟进，不以一次 heartbeat 一个动作或静默数小时替代推进。goal 为 blocked 时不能只在消息中声称 active：核对真实 goal 返回，恢复能力不可用则请求用户在原 Luna 恢复，不创建替代线程或调度器。

## 首次环境配置

### 双执行机器：Web 始终在本机

`worker_host=local|macmini` 只切换 GLM 和 Chrome；四个 iac-code Web、Server16099、SQLite、Sol、签名凭据与截图文件始终留本机。不要在 Mini 启动第二个工作流 Server 或复制本机凭据。同一测试计划内不更换 Luna 的 goal 或 Sol，仍无定时任务；新计划允许新的 Luna/Sol。

角色按冻结 plan_id 绑定。新计划首次 Sol job 不继承旧全局 Sol，adapter 正常创建新线程并固定到该计划；已有计划从自身历史 jobs 恢复原 Sol，重启或升级不替换。程序仍只有一个活动 Sol 租约。新 Luna 的交接由 OPS 在旧计划已收口、所有回合终态、无未知提交/未处理所有物、旧调度者停止后执行；不能让两个 Luna 同时调度，也不清空 SQLite、历史 PASS 或 wire 次数。用户要求复用旧角色也只能在计划开始前决定，不能中途为缩短历史而换人。

本机模式沿用百炼4501、Chrome9225。Mini 模式沿用 `codex-alilang-macmini` 管理的远端4500；本机适配器通过自有 SSH 的4510访问它，不调用控制器 `new/send`。Mini Chrome9225 经本机9226控制；反向转发 Mini16100–16103 至本机同端口 Web。SSH 地址每次由配置的 `ip_resolver` 重新发现，强制原 known_hosts 的 `macmini-dynamic` 主机键，不使用缓存 IP 兜底或暴露 Web 到公网。SSH 服务必须允许反向转发且保持 loopback（GatewayPorts=no 或 clientspecified）；不能使用 GatewayPorts=yes 的全网监听配置。

先在私有 environment 配置 `macmini` 中本机 resolver / SSH key / known_hosts 和远端 user / Python / Chrome 可执行文件 / 专用 profile / worker_cwd。示例不含凭据，路径须替换；worker_cwd 是远端受控工具线程的工作目录，不需要复制仓库。远端 app-server 必须由原控制器保持 ready/connected，不另启一个4500占位进程。

在没有活动回合、租约、未处理云 owner、原资源未完成 GLM thread 的计划边界操作：

```sh
uv run python -m scripts.web.e2e.resource_link_workflow pause
uv run python -m scripts.web.e2e.resource_link_workflow backup /absolute/private/host-switch.sqlite3
uv run python -m scripts.web.e2e.resource_link_workflow configure /absolute/private/environment.json
uv run python -m scripts.web.e2e.resource_link_workflow worker-host-select macmini
uv run python -m scripts.web.e2e.resource_link_workflow server-stop
uv run python -m scripts.web.e2e.resource_link_workflow status
uv run python -m scripts.web.e2e.resource_link_workflow macmini-link-start
uv run python -m scripts.web.e2e.resource_link_workflow chrome-start
uv run python -m scripts.web.e2e.resource_link_workflow bootstrap 0
```

首次 configure 时保持原 `worker_host=local`，添加 Mini 配置；用 worker-host-select 做实际切换，不通过手改 worker_host 跳过 target 保存。首次选择 Mini 后四个 target 为空；为1/2/3重复 bootstrap，并在 Mini 专用 Chrome 完成一次国内站登录。两台机器各保留一份 profile；以后切回会恢复各自 target，不复制 cookie，不重复已通过的测试。四个 Web 原 PID/cwd/config/session/token 不重建。链路启动返回 ready=false 是尚待检查，不是失败或 PASS；正式派发仍要通过真实 Web 解锁/精确 target、Chrome 所有权和所选控制器检查。

切回本机：先暂停、排空并备份，`macmini-link-stop` 只停止精确自有 SSH，不关闭远端 Chrome；再 `worker-host-select local`，server-stop/status 重载同一 Server，核对恢复的四个 target 与本机 Chrome 后 resume。不改已绑定历史模型端点，不迁移在跑资源到另一后端。默认本机 cdp_endpoint 保持9225；不要手改为9226，Mini 转发端口由程序决定。

网络中断：保留原 job/thread/turn、SQLite、profile、target、调用数与 launch/trace；先确认原 SSH PID 真正已退出，再在原模式执行 macmini-link-start 恢复同一链路。活 PID 身份/配置冲突直接拒绝，不杀未知进程、不本机兜底、不重复 new。Chrome 已退出可 chrome-start 恢复同一 profile；仍存活的异常 PID 只能独立核验后 chrome-adopt。旧回合核查继续使用本机 `adapter.mjs <state绝对路径> <job_id> --read`，后端从原 durable binding 读取，禁止改为桌面 read_thread 或当前模式的另一控制器。

截图仍在本机按原始 PNG/SHA、同账号地域资源、普通/Shift 真点击及清理证据由唯一 Sol 验收；后端配置、SSH就绪或HTTP200不产生 PASS。

修改 environment.example.json：repo、Node/Playwright、Chrome 可执行路径、百炼和 Sol app-server 的 loopback 端点。不在文件中填真实 token/AccessKey。configure 保存私有环境文件；更改后重启同一 Server 加载。

Sol 后端可由本机 codex app-server --listen ws://127.0.0.1:4502 启动；GLM 使用现有百炼控制器的 app-server。先只读核对控制器 ready/connected。当前采用普通脚本，须实际确认当前 job cwd、workspace-write/network/never 和无额外可写根；返回不兼容时不切回危险 full-access new，修适配或报告精确能力阻塞。后端可将隐含 cwd 从 writableRoots 中规范化移除（空数组），此时同时核对返回 cwd 与 runtimeWorkspaceRoots，而不是把等价策略误拒绝。

macOS 试运行应优先配置当前桌面自带的 codex_cli（例如 ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex），不要依赖 PATH 中可能固定旧版本的 codex。先核对版本及 model/list；旧 CLI 拒绝指定模型时属于接入故障，不换模型、不另建 Sol。确认原 turn 真实终态后停止精确自有4502进程，更新配置、重启同一后端并 resume 原 thread。

Luna / Sol 都必须在主 Codex 桌面可见，配置分别为 gpt-6-luna/high、gpt-6.1-sol/high（roles.json）；GLM 在所选机器的百炼桌面、glm-5.3-prime/xhigh。Luna 通过桌面创建接口；Sol 优先连桌面共享 app-server，使用持久线程。独立4502只是传输备选，不保证 UI 实时发现：核对与桌面相同的会话存储，按精确 threadId 确认能列出并打开，再开始正式测试。不修改用户全局模型默认值，不另建 Sol 副本来解决可见性。

```sh
uv run python -m scripts.web.e2e.resource_link_workflow provision --from-config /absolute/private/iac-code-config
uv run python -m scripts.web.e2e.resource_link_workflow chrome-start
uv run python -m scripts.web.e2e.resource_link_workflow web-start 0
uv run python -m scripts.web.e2e.resource_link_workflow bootstrap 0
```

为槽 1/2/3 重复 web-start/bootstrap。provision 只复制三个配置文件至独立目录，不复制历史会话。复用现有测试身份即可，不要求改 IAM；管理员身份必须如实记录，安全边界由程序执行门禁承担。同机器的四槽共享一份专用 Chrome 登录态，本机默认 state/chrome-profile；Mac mini 使用配置的远端 profile，两机各自保留，不交换 cookie。四个 Web origin 各自 token 解锁，程序只读取私有 token 文件导航根页，不代 GLM 创建会话。

bootstrap 只能处理空闲槽。浏览器被用户关闭时，先精确收口或保留正在执行 lease，重新 chrome-start；不能替正在执行的资源换 target 并称原证据连续。把执行缺口交 Sol，下一次 attempt 在恢复后的新 target 执行。

claim 直接核对旧 guard 的持久租约（不启动旧调度、不读取已归档任务文档），在 repo/evidence/resource-link-workflow-owner.json 建持久 owner；旧 prepare/backfill 硬拒绝。新流程停用后若确需旧流程，由 OPS 核对所有任务、回合及资源清理完毕、备份后单独交接，不能运行时删除 marker。

测试 Web 通过本目录 web_entry 显式安装 execution_gate 后启动真正的 iac-code CLI。
普通产品不加载这些测试脚本；实际加载清单须包含 execution_gate、web_entry 和产品通用
execution_policy 的原始 SHA。升级此边界后仍须暂停排空、备份、重启原四槽并 web-attest，
不能把没有策略的普通 Web 认作测试 Web，也不移动原库、cwd、session、token 或 target。

## 创建授权

原生 ROS 空 RocketMQ 测试实例已创建、实点图仅被原生公告遮挡，且原删除/QUERY 从未调用时，
可用 inspect-retained-capture 原GLMjob --output R编号/新提案.json。暂停、排空、备份后独立核对
原终态 GLM/Sol、Sol inspected 原 PNG、同会话签名来源、全部原 grants/累计计数及自身未用删除。
批准原提案后 queue-retained-capture 原GLMjob --evidence R编号/审查.json，仅排原 GLM 续跑。
不新增身份/创建/读取额度、不覆盖旧图/来源/cleanup intent。先保留新真点击落点图、原生关闭精确
RocketMQ 公告并补图，按原 DeleteStack FIRST 完成清理，再执行原先未安装过的 QUERY 维度。
若实际原签名已经到期，须先独立审查并登记一次原作用域的 `authorize-cleanup-identity`；
新补图提案绑定这项尚未使用的身份续核及原累计次数，补图后才执行一次真实同账号 STS。
补图排队本身不增加身份、创建、删除或查询额度；不得重复登记身份或重置原调用。
QUERY 沿原 authority 首次安装原 grant；其业务样本不成为 owner。全部新图与原清理仍由同一 Sol
独立验收；候选、排队、公告关闭和服务重载不产生 PASS，当前签名轮换仍真实拒绝，不伪造身份。

同一原生 ROS 空 RocketMQ 已创建，但全页同标识引用混入其他产品 href、点击在
selectOccurrence 前置门禁明确拒绝时，同一 retained-capture 入口也支持已由原 Sol inspect
的产品页诊断 PNG（actual_ui_click=false）。必须逐条核对原 owning-backend 终态及
script/request→script/rejected 的精确已知落点歧义错误，诊断 result 原件完全相同；
任何未知/成功点击、occurrence 文件或补图均拒绝。真实签名来源和自身创建代次仍独立核验，
不把诊断图升级为正式点击，也不伪造其来源绑定。新实点仅原生展开自身 GetStackResource
卡；同一 scope/session/authority 和未用删除保留，新增身份/查询/创建额度均为0。
上一已清理代次的 capture 指针只能在当前显式追加操作确实绑定该 prior_scope、原 Sol
清理回执重验通过、旧 scope 无活动/未知且原删除成功时，由 queue 原样归档到历史键后
选择当前代次。旧审查、原证、调用和所有权不改，不手删 meta，不允许复用同代 checkpoint。

原生 ROS 的空标准版 RocketMQ 来源使用 `ops_native_rocketmq_sample_admission`。只接纳冻结的
ROCKETMQ Instance ROS/QUERY 缺项、独立人类许可、完整单实例内联模板、零 Topic/Group/消息及
轮询的官方成本审查、一个自身响应绑定 CreateStack/DeleteStack 和完整区域列表消失核查。
source_review/cost_review/operation_review/proposal 各自绑定 inputs 原件 SHA；QUERY 业务样本
始终只读，不登记或清理。暂停、排空、备份后 authorize-temporary-samples，再 authorize-creation，
由原 Sol approve、Luna 派发。首次准入不可换名/attempt 替换，不开放其他 Ons 操作或新增额度。

初次空 RocketMQ 样本已实际创建、实点失败并经原 Sol 独立接受清理后，追加补测仍走现有
`additional_source_requests` 和 `authorize-creation`，不能替换 `temporary_samples` 初始记录。
新提案的 ROS/QUERY 调用、完整模板、名称及上限须与原件逐字相同。OPS 的
`ops_additional_sources_review` 另含 `native_rocketmq_followup`：kind 为
`ops_native_rocketmq_followup_review`，approved/task_id/execution_sha256/proposal_sha256、
proposal/source_review/cost_review/original_admission 逐件 path/SHA，及原始程序
predecessor_cleanup_receipt（receipts 路径/SHA）。必须明确 within_existing_task_and_hourly_scope、
not_unlimited_rebuild、creation_after_failed_capture_and_verified_cleanup 均为 true。
来源/费用审查沿用精确原生类型但绑定本次执行摘要；原 Sol 清理接受事件、精确根栈/成员及
原响应必须匹配，旧创建/删除真实各用一次且无未知调用。程序只绑定新显式操作的独立 authority，
不改旧额度/初次准入或自动增加其他来源能力。授权后当前新 owner 尚未清理时仍可正常读 context；
不能把新 owner 错当未清理的旧代次。最终仍须新实点原图/补图、原响应绑定删除和独立 Sol 验收。

已创建但尚未实点的 MSE 托管 TF 栈可以使用 `inspect-managed-continuation <原GLMjob>
--output R编号/新提案.json`。它只投影独立终态 Sol review、原 session/packet/ownership、成功创建
及终态栈、全部原 grants/attempts/counters 和未用执行读取。独立审查后只改 approved=true，
用 `queue-managed-continuation <原job> --evidence R编号/审查.json` 排给唯一 Luna。
没有新增身份/创建/读取额度；无实际 Delete 时才可从误入的 cleanup phase 恢复 execute。
原文件和失败 cleanup intent 不覆盖；原 GLM 在新输出目录补读取、来源及真实点击，再按原
授权清理并逐件核实，唯一 Sol 看图及清理证据后验收。真实未知调用、已点击/删除或签名漂移拒绝。

实际 OAuth/STS 轮换导致原 MSE 剩余调用被拒时，`inspect-managed-continuation --renew-identity`
可投影一次同 scope/账号/完整会话的身份续核。OPS 独立核对新旧真实签名来源和原创建响应，
批准原提案后由 queue-managed-continuation 仅追加一个 STS grant；不复制旧身份或任何资源额度。
原 GLM 必须先 continue-execution(stage=identity)，等真实同账号成功后再分组 continue-read。
原成员来源、原响应绑定删除及阴性清理读取以不可变审查连接这一次新签名，旧签名记录不重写。
账号不符、未知写、重复续核或提案漂移仍拒绝；身份续核不是导航或清理通过。

同一 MSE 续跑已真实完成全部成员/详情读取并冻结新来源，但点击在传输前因创建代次绑定错误
被拒时，可用 inspect-managed-continuation --capture-only。只投影最新终态原 GLM 的精确来源、
完整同会话签名成员与原创建链，允许 reported/blocked 的原代次；无实际点击、删除或未知调用。
独立审查后原 queue-managed-continuation 仅排补点击及原清理，新增身份/查询/创建额度均为0。
点击追溯原创建 job，并独立核对已批准且实际同账号的签名后继；不把新续跑 job 当新创建。
原 GLM 不再 request/source/continue-read(source)，只实点缺项，然后沿原额度清理并核实。
旧来源、读响应、失败记录和 PASS 保留，Sol 仍独立看新图和真实清理，不因来源完成自动通过。

NAS DataFlow 首次 QUERY iframe 缺健康来源时，可在原小时许可范围内审查
`ops_native_nas_query_sample_admission`：绑定冻结任务、最终完整 proposal/source_review/operation_review
各自 `{path,sha256}`、execution_sha256。只接纳门禁固定的原生依赖图，不是通用 QUERY 写权限。
来源审查 `ops_native_nas_query_source_review` 须绑定账号、地域、唯一名称、完整 grants SHA、
官方材料、隐式依赖及所有新建物反向清理；小时费用仍另审全部依赖、请求和网络费用。
先排空/暂停，再 `authorize-temporary-samples`，之后对相同完整提案 `authorize-creation`。
首次准入必须在任何 GLM/attempt 前；准入后整份提案不可变，不因新名称、attempt 或 SHA 刷新额度。
只测试自身成功创建且 Running、父 FS/Fileset/OSS 关联一致的 DataFlow；点击前程序核验本会话签名
查询的 toolUseId 和自身创建链。QUERY 同样要明确 cleanup 请求、实际反向删除和逐件消失证据。
这只补查询导航来源，不记 CREATE/ROS/TF 通过，也不重放已有 open PASS。

Fileset 原图描述省略属于采样缺项，不是链接错误或无样本。`nas_fileset_description` 仅允许
同一实际点击 NAS Fileset 列表、唯一原始 FsetId 行的原生描述 hover；正式补图仍绑定自身
成功签名 DescribeFilesets 的父文件系统、FsetId、完整 Description 和 FileSystemPath，
不缩减字段，不编辑或导航，不用 OPS 诊断图计 PASS。Sol 独立 inspect 原图及 tooltip 图。

先读 status.progress.attention：机器交接给出稳定 id、原任务/精确回合和 packet 提案路径/SHA。
创建授权仍逐操作独立审原提案，不能把交接当授权；原证变化先核对，不自动 approve/retry。
同一 id 的重复状态不需要重复通知，但无人处理的交接必须主动处理。can_progress=true 时其他
资源继续由唯一 Luna 推进，不为单项输入/权限问题全局暂停。retry-blocked 要求原任务全部模型
真实终态；解决后 Sol context 保留交接材料，只复核变化，不重新完整扫描。
input 修复没有共享代码写权限，可与其他槽并行；product 修复仍排空并阻断新 GLM，排空期间
Sol 可提前准备其他 packet。控制框架/门禁本身改动仍用本指南全局维护备份/排空/重载流程。

派前失败返回 dispatch_deferred 时，没有启动 GLM 或预留预算/云能力；程序把该空闲槽登记
slot_preflight 交接，不再反复选择同一坏槽。恢复原 Web/Chrome/target 或已查明接入故障后，
执行 `slot-recover <原槽号>`：控制端重新核对真实 Web 加载 SHA、原进程、精确 target、解锁与
控制器 ready/connected，成功后才解除本槽隔离。它不创建模型/云资源、不刷新云身份或写 PASS；
真实国内登录仍须独立检查。普通 resume/dispatch-enable 不会清除隔离，重启也不会失忆。
四槽首波仍要求四个可用槽；已进入滚动阶段时其他隔离好的槽继续推进，不误杀活跃租约。

已有专用 Chrome 不需要重复登录：先 pause 并确认没有活跃模型回合，再用 `chrome-adopt --pid <实际PID> --profile <实际专用profile>` 接管。程序核对 PID 启动身份、CDP9225 和 profile，不接管普通用户浏览器。

派发前程序检查本机进程、专属 target、Web 解锁和百炼 ready/connected；**国内站登录仍须 OPS 在专用 Chrome 实际检查**，就绪状态不代替登录。产品出现创建/删除权限面板时，只在批准的操作与所有权范围内确认单次请求；不得永久放行、自动批准未知命令。

默认创建关。Sol 先产出 packet 提案（即使授权未齐也须先写提案，不能只 block 账号权限），OPS 核对：用户授权范围、官方费用、依赖 VPC/区域/CIDR、唯一测试名、每个完整 API 参数/内联模板、一次创建及对应删除、只读确认语义。已有 VPC 只作前置查询，绝不批准删除；若需创建依赖，应另有精确 create/delete grant 并一起登记清理。

已冻结计划的费用许可后来获用户批准时，先 pause、排空模型和 issued action、备份，再使用 OPS 专用 `amend-budget <原plan_id> --budget-cents <总预算分> --evidence inputs/...json`。审查为 schema_version=1、kind=ops_run_budget_review、approved=true，绑定原 plan_id、尚未收口 task_id、frozen_plan_sha256、old_budget_cents/new_budget_cents 以及 human_authorization 路径/SHA。原始授权材料为 kind=human_fee_authorization、approved=true，保存实际用户原话、相同 plan/task 和 max_budget_cents；不得用模型总结代替授权。命令只提高原 run 预算并追加审计，保留冻结资源/模板、已有预留、原 authority、wire 次数和 PASS；同证据重复提交幂等，原预算或 SHA 改变拒绝。不得重新 import、换 plan_id 或手改 SQLite。预算修改本身不批准云操作：Sol 最终提案须预留实际预算，OPS 仍独立审精确提案并 authorize-creation。模型工具不暴露此命令，本地预算也不是云端硬封顶。

在 state/inputs 内保存原授权与 OPS 逐操作审查记录，然后执行：

### 小时费用许可与旧累计预算分开登记

`register-hourly-fee <plan_id> --evidence inputs/...json` 仅在暂停且模型/issued action 排空后登记。
审查 `kind=ops_run_hourly_fee_review`、schema_version=1、approved=true，绑定原冻结计划 SHA、
排序后的完整 resource_ids/scene_ids、currency=CNY、max_concurrent_hourly_cents 及
human_authorization={path,sha256}。人类原话材料为 human_hourly_fee_authorization，
明确相同范围、小时上限和 total_budget_cents=null。登记不可变、重复提交幂等，status.active_plans
及角色 context 均提供实际小时授权。原冻结计划、累计预算、调用、预留、所有权和 PASS 不变。

登记本身不是云端硬封顶，也不构成任何 API 调用许可。精确操作另用以下小时费用契约；
**临时来源的资源类型准入仍是独立门禁**，不能因为小时审查通过便绕过默认只读属性。

最终 packet 增加 `hourly_cost={max_hourly_cents,cost_evidence:{path,sha256}}`，路径在 inputs 内。
`budget_cents` 保留原累计预算门禁，不转换为小时金额；纯按小时计费且无额外累计费用时可为0，
但正数 hourly_cost 必须如实披露并经过独立审查，不能把它称为免费。原有收费追加来源的累计
审查/预留复用契约不变；未包含的请求、流量或一次性费用必须另行覆盖，不可漏算。

cost_evidence 原件为 schema_version=1、kind=ops_hourly_cost_inventory、approved=true，
绑定 task_id、currency=CNY、账号、完整 execution_grants 的 canonical SHA、相同 max_hourly_cents，
并声明 includes_all_dependencies=true、includes_request_and_network_costs=true。components 为非空数组，
每项包含唯一 id、整数 max_hourly_cents、minimum_billing_seconds、billing_mode=pay_as_you_go|free、
非空 estimate_basis/limitations、bounded_usage=true 和非空 official_sources（逐件 inputs 路径/SHA）。
free 项必须金额0，各项合计须与 packet 精确相同。OPS 独立读原官方材料核实含义、规格及费用，
程序只核算术和来源完整性，不把任意 JSON 或估算本身当作官方价格/账单。

原独立操作审查须同时绑定最终完整 proposal_sha256，并包含逐字相同的 hourly_operation_review：
schema_version=1、kind=ops_hourly_operation_review、approved=true、plan_id、task_id、execution_sha256、
hourly_cost 以及 hourly_permission（已登记小时许可的 evidence 路径/SHA）。仍用 authorize-creation，
再由唯一 Sol approve 同一完整原提案；来源、成本、调用、原许可或提案变更都重新拒绝，不能
只批准执行摘要而在 requests/scenes 中偷偷换内容。授权时不预占费用、不调用云。

正式 dispatch 在同一 SQLite BEGIN IMMEDIATE 事务内复核全部原件并预占小时费用、槽位、job。
合计包括所有未释放计划/任务，执行结果未知、模型终态或 Server 重启不会释放费用。已有同任务
预占不能转给新 job/attempt；不足时跳过该 ready 任务，选择其他合法可负担任务，不降低首波门槛。
status.active_plans.hourly_cost_holds 和 context.hourly_cost_holds 提供真实预占，后者覆盖全部计划。
并存不同上限时采用当前各份许可的较严上限；不借新计划躲旧残留。

只有现有 Sol accept 已独立核验完整创建/删除/必查消失原证、OPS recover-blocked 通过原清理与
未知创建硬检查，或 reconcile 证明精确 job 在 turn/start 前失败且原 wire 无调用/活动能力时，
内部路径才能释放预占。保留 released 记录及原证绑定；无条件释放、按时间过期、仅 owner 列表为空、
HTTP 删除成功或 worker 报告均不构成释放入口。仍有未释放费用不能 finalizable。

冻结 `failed_navigation_managed` 仅含 TF 导航且从未执行 GLM 的任务，可用同一
`authorize-temporary-samples` 登记 `ops_hourly_managed_tf_sample_admission`（schema_version=1、
approved=true、task_id/plan_id/frozen_plan_sha256/execution_sha256）。`proposal`、`source_review`、
`operation_review` 均为 inputs 原件 `{path,sha256}`。提案保留精确缺项、budget_cents=0、正数
hourly_cost、ROS 托管单一完整 main.tf、一份 CreateStack/DeleteStack 各一次及完整清理请求。
operation_review 同时满足上面的独立小时费用契约并绑定完整 proposal_sha256。
source_review 为 `ops_hourly_managed_tf_source_review`、approved=true，绑定 task_id/account/
execution_sha256，sample_binding 包含 template_sha256、region、stack_name 及按 HCL 原序列出的
resources=[{type,name}]；明确 full_inline_template_reviewed、implicit_dependencies_reviewed、
no_existing_resource_mutation、all_new_resources_response_bound_cleanup 全为 true，附 estimate_basis、
limitations 和逐件 SHA 的非空 official_sources。OPS 必须独立审实际语义、隐式依赖和清理，
静态资源扫描不是 Terraform 语义证明。首次准入不可替换，费用证据也不能换；后续会话沿用原
authority，不能借新名称/attempt 补创建额度。此入口不放开 QUERY 写操作，不批准云调用或 PASS，
还须独立 authorize-creation、Sol approve、真实截图及原资源清理验收。

MSE 托管栈的 `mse_managed_child` 清理读取由程序从原签名成功栈、预删除成员及原删除响应投影；
不让模型填写已知业务 ID。控制端仅对官方精确 QueryClusterInfo / DescribeVpcAttribute /
DescribeVSwitchAttributes 的不存在状态、错误码和消息识别原始负响应，保留 unknown、额度和原文。
GetStack 的嵌套 Stack 响应及原栈删除后 StackNotFound 也须同账号/会话/签名归属链。
识别负响应只证明原必查读取已执行，不自动将 owner 标 deleted 或写导航 PASS。

原 MSE 栈删除已实际签名成功、原身份额度耗尽后，OAuth 的具体 STS 签名轮换可能阻断剩余
清理读取。暂停并排空模型后，可沿用 `authorize-cleanup-identity` 独立审一次身份续核；
`ops_cleanup_identity_review.post_delete_readonly` 须逐字绑定当前检查所得的
`successful_deletions` 和 `cleanup_read_calls`。仅此已删除的原托管 MSE 来源适用。
旧创建、成员、删除的签名和全部次数不改；只追加一次原 STS 调用，原会话先单独执行并实际
证明同账号后，才允许原响应绑定的必查清理读取。新签名不能重建、再删除或回到 execute。
原证漂移、续核失败或已用尽即拒绝，不能换回合补满。阴性读取仍保留原 unknown 响应，
由 Sol 独立核验消失与截图后验收，不因身份恢复自动产生 PASS。

冻结失败导航计划需要新建临时 OTS 空表来源时，默认只读属性不因费用许可自动解除。暂停、排空、备份后，OPS 用 `authorize-temporary-samples <原task_id> --evidence inputs/...json` 登记唯一不可变准入。`kind=ops_temporary_sample_review` 审查绑定原冻结计划 SHA、原人类许可路径/SHA、Sol 原始完整提案路径/SHA、execution_sha256 和 ROS/TF 两份 sample_bindings（同一已验证现有实例、地域、不同表名、内联模板 SHA）。仅原生 ROS 和 ROS 托管 Terraform 各一个空表，零预留 CU、不写数据、不建索引、不新建实例或改变网络/IAM；预算不超过人类许可和本 run。完整 HCL 仍须独立核对 pinned 官方 provider，不能把文本检查当语义审查。

该命令只追加准入审计，不改变冻结目标、PASS、创建验证、预留或调用计数，也不批准云操作；仍需逐操作 `authorize-creation` 和 Sol approve。context 的可信 `readonly_retest_dimensions` 才包含精确例外，不能改 resource.retest_scope。模板、名称、调用或账号漂移不继承准入；不同 session/attempt/子集提案沿用原 execution_sha256:dimension authority，不能复制创建额度或替换准入。测试后原响应绑定删除及栈 ID、栈名、表名消失核查仍必需；本地预算不是云端硬封顶。

唯一已审 OTS 托管 TF 空表若在 apply 前被 pinned 1.140.0 的主键枚举校验拒绝，可修正
`STRING` 为 `String`，但不能覆盖上述原准入或复用已耗尽的写权限。须保留原成功创建/删除、
真实终态和独立清理证明；原签名 GetStack 的 plan stderr、OperationInfo 必须精确证明该
校验错误且无 apply。OPS 独立确认没有创建过 TF 子表，故仍在用户“一个空表”范围内，
而非把费用许可当重试许可。最终 plural additional_source_requests 提案仅保留 TF iframe，
调用、父实例、栈名、表名、provider、地域不变，仅一处枚举与新操作的身份/预查各一次。
`ops_additional_sources_review.temporary_template_corrections[精确request_id]` 必须为
`ops_ots_tf_primary_key_correction`，绑定 task_id、原 admission 路径/SHA、前后模板 SHA、
原失败 attempt ID、pinned 官方原始源码路径/SHA/URL、original_table_never_created=true
及独立 human_scope_interpretation；另需累计费用审查和原预留绑定。原终态、所有物、未知
调用和创建/删除累计仍逐项重验，生成独立新 operation authority。名称漂移、其他模板改动、
已发生 apply、原来源或审查漂移、换 request_id 复制该修正均拒绝；不是通用续期或自动重试。

同一剩余 OTS 托管 TF 表在上述 String 修正后，若真实 apply 被 `OTSMissingHeader`
（`x-ots-sourceip`）拒绝，可独立审一次 Provider 版本试验。它不是旧枚举修正或已证明的修复：
`temporary_template_corrections[request_id]` 使用 `ops_ots_tf_provider_header_correction`，绑定原
task/plan/frozen SHA/不可变 admission、最新实际失败 attempt、空资源成员原响应、已提交 deleted
owner 和自身清理证明 SHA。原签名 GetStack 必须证明 1.140.0/TF1.5.7 apply 缺头失败，原栈及
表已真实清理；同账号、同地域实际 GetFeatureDetails 须明确支持 alicloud1.260.0，而非别的 Provider。
仅一处 `1.140.0→1.260.0`，原 String、父实例/名称、完整其余调用/次数/读阶段/清理均不变。
OPS 另审完整固定标签 client/table/go.mod 原件及 SHA，明确 `runtime_source_ip_unknown=true`、
`bounded_trial_risk_accepted=true`、不手工填 IP/头或扩权，并解释原人类单表范围尚未用尽；
保留原累计100分及真实累计费用/旧预留。未知调用、旧未清理、版本之外改动或改 request ID
复制试验均拒绝。新独立 authority 仍需 authorize-creation 和唯一 Sol approve，不改旧 journal。
实际安装/版本/请求头仍可能失败，保留原始日志、停止进一步自动创建、清理自身栈；只有真实
新来源、点击截图及唯一 Sol 验收才产生 PASS。该入口不改变已有枚举修正或原人类资源范围。

```sh
uv run python -m scripts.web.e2e.resource_link_workflow authorize-creation --proposal inputs/R0001/packet-proposal.json --evidence inputs/R0001/ops-operation-review.json --max-budget-cents 0
```

命令不写云，只生成 creation-authorizations/<执行SHA>.json 下的独立 0600、schema_version=3 授权：existing_account、资源编号、账号、预算、完整 execution_grants 的 SHA、原审查证据 SHA；lifetime=task、expires_at=null。授权不因固定时钟失效，任务安全释放槽时程序关闭对应能力。旧 schema_version=2 的到期记录保留，重新审原调用消耗后才换任务期授权，不能自动重置额度。四个槽的资源授权互不覆盖。Sol 必须把同一提案提交 approve，参数改变需要重新审查，不能扩大范围。

实际 API 门禁保存在 state/execution-gate.sqlite3；每个 Web 的环境绑定此库及唯一槽号。没有活动完整会话/能力时默认拒绝。正式 STS GetCallerIdentity 的原始 AccountId 与批准账号一致后，才放行同一凭据、会话的其余 API；当前只支持可稳定绑定的 AK/STS 签名凭据，动态签名需增加适配，不把空凭据指纹当账号一致。旧 Web 进程没有门禁标记会拒绝派发，须 stop/start 同一槽（保留 config/session/token）。

云调用前写入 unknown 意图并扣次数，响应由实际传输层入库；未知结果、限次、创建物不会因 Server/Web 重启清零。DELETE 的参数只能由原始创建响应投影，不能填已有 ID。产品的永久允许/全权限模式不能覆盖门禁。Sol context 自动登记实际创建物，accept 额外核对 wire journal；漏报或未知创建不允许 close。HTTP 删除成功仍不是清理完成，必须原始只读消失证据。

同名对象的所有权须绑定原 GLM 创建代次；自动 wire 登记、执行端登记、Sol 原响应登记及 accept 共用 generation-aware 键。已证明原代次的历史键/删除证明保留，不迁移为新代次或改写旧 PASS。来源代次无法证明时保守要求独立验收，不把旧 deleted 行当新创建已清理；重载须包含 ownership_generation 的实际导入 SHA。

同一执行授权的次数跨模型回合/attempt 累计，不因换 session 或新 job 清零。已消耗创建额度后需要新的明确操作提案与 OPS 审查，不自动用重试复制新额度。活动能力过期或创建结果未知时由 OPS 查原始云证据；当前没有“自动续租/自动重建/猜 ID 删除”捷径，不能绕过门禁推进。

确需修复后新增一个免费测试来源时，保留原 Sol 提案，另存带 `additional_source_request` 的最终提案：唯一 `request_id`、维度、精确原 `prior_scope`、原提案路径/SHA 和修复材料路径/SHA。OPS 原始审查须为 `kind=ops_additional_source_review`、`approved=true`，绑定最终 `proposal_sha256`，且 `additional_source_approved` 与请求完全一致。authorize-creation 只读核对原回合真实终态、所有本任务资源已核验删除、原 create/delete 各一次成功且累计已消耗、原调用/账号/名称/模板完全相同；仅生成这一明确新增操作的独立授权，不修改原授权或 wire 计数。未知结果/未清理/改变参数/重复 ID 换证据拒绝。正常缺项只沿用尚未消耗的旧维度 authority，不用此新增来源机制。执行后仍须真实新来源、截图和清理验收，没有模型时限或自动新增来源。

初次纯 QUERY 由 Sol 原始 packet 批准，不要求创建授权。后续 QUERY 使用 plural `additional_source_requests` 时仍须 OPS 独立审查并 authorize-creation：若原初次 QUERY 合法地没有创建授权，程序改为追溯原 terminal Sol/GLM、最后一次 packet_approved 的完整 packet SHA、先后时间和实际同账号签名身份/精确成功读取，不补造历史创建授权。已有但无效的旧授权不能走此替代；带新增操作/selection、写调用或非零预算的前置计划不能冒充初次 QUERY。新增只读授权在 approve、context、request 边界都重验，原 wire 计数不修改；没有云写操作时不要求伪造 cleanup_recipe。

SAE 自定义命名空间的独立新增 QUERY 可只为原 `DescribeNamespaceList` 增加布尔 `ContainCustom=true`。
OPS 审查中的 `query_parameter_reviews[精确request_id]` 须为 `ops_sae_include_custom_query_review`、
approved=true，逐字绑定 grant_id、original_params、approved_params 及 official_evidence/official_sha256。
只接受原国内地域、SAE Namespace 场景、各一次且已成功耗尽的原 STS/元数据读取；其他参数、版本、
账号、身份调用和配额不变。原终态/授权/签名及全部 wire 仍逐项重验，不改原 journal；这不是任意
参数变更通道，也不自动批准视觉映射或 PASS。最终提案 SHA 仍独立 authorize-creation。

SAE Application 的既有样本原生默认组缺失时，可以独立审查同账号、同国内地域的另一个已知
应用；只允许 `ListApplications` 的 `FieldType=appIds` 下更换精确 `FieldValue`，原身份/读取各一次、
版本、分页和其余参数不变。`query_parameter_reviews[request_id]` 的 kind 必须是
`ops_sae_existing_application_sample_review`，批准并逐字绑定 `sample_selection_approved`、
grant_id、original_params、approved_params；原请求的 `query_sample_reselection` 保留不改。
独立审查复用原始有界诊断 report/SHA、同次 STS、第一页最多十项 ListApplications 及该
新 AppId 的 DescribeApplicationGroups 原响应/SHA/RequestId/时间顺序；同一原始应用行的
AppName/AppDescription 和唯一 `_DEFAULT_GROUP`（GroupType=0/精确 GroupId）必须真实存在。
`official_evidence` 为逐件 SHA 绑定的 `ops_official_sae_existing_application_review`。
仅替换自身 QUERY 场景的 identifier、source 路径/SHA/指针和上述两项 render_text，其他场景、
类型、账号、地域、冻结缺项及旧 PASS 不变。原操作终态、累计耗尽、无未知或未清理所有物
仍全量核验；这不是通用参数变更、修应用/组、额外扫描或创建配额入口，真实点击仍须另验。

原生 ROS 的单一空 ResourceGroup 若由 OPS 实际删除，而原模型 DeleteStack 从未调用，可在该新增操作请求中显式绑定 `external_cleanup={path,sha256}`。证明 `kind=ops_external_ros_group_cleanup_provenance`、approved=true，绑定精确 task_id/scope/原 authority，pass_added=false、model_wire_unchanged=true，以及 `original_creation/delete_result/stack_absence/group_pending_delete` 四份路径/SHA。程序只读重验原签名创建及代次、独立 SDK 的同账号身份/原栈/唯一子资源/空成员/响应绑定删除六条真实调用、不可变请求和响应及时间顺序、删除后 DELETE_COMPLETE、native deleted owner 与原证明，以及该子组独立人类 PendingDelete 原始授权事件。必须保持该 scope 和累计模型删除 used=0、无删除 attempt；未知结果及其他类型拒绝。这不是把 OPS 调用补进模型 journal，也不批准自动重建；新增来源仍须另行逐操作审查，旧 PASS 不重放。

后续 Sol accept 遍历历史创建代次时，对已 deleted 且绑定上述实际批准请求的精确原 scope，再只读重验完整 external_cleanup 链，允许其独立 OPS 删除/删除后核查替代未发生的模型 cleanup 读取。只豁免当前已核验的历史空 ROS 资源组代次，不因 deleted、暂缓 ROS 或其他代次已清理就跳过门禁。原 GLM 须终态、唯一当前 Sol review 精确绑定；其他活动回合、原证漂移或未知调用拒绝。当前 TF/ROS/API 创建代次和其他资源仍须原 required_cleanup，不重删旧栈、不把 MODEL delete used=0 改成1、不新增 PASS。

已批准的小时制 MSE 托管 Terraform 开发版单节点订单若实际返回 `IllegalRequest`、原栈
`CREATE_ROLLBACK_COMPLETE`，且原栈及实际创建的独立 VPC/VSwitch 已由 OPS 删除并核查消失，
可逐操作审查一次专业版三节点纠错来源。`temporary_template_corrections[request_id]` 使用
`kind=ops_mse_tf_professional_correction`，绑定原临时准入、冻结计划 SHA、原 authority、前后完整
TemplateBody SHA 和 `external_cleanup`。除 `mse_version` 与 `instance_count` 外，原版本1.260.0、
测试名、私网/公网带宽0、网络、输出、调用及额度均不可改变。新提案须有自己的独立小时费用清单
和 `hourly_operation_review`；不得覆盖原开发版费用准入或把预算0解释为免费。

此例的 `external_cleanup` 为 `ops_external_failed_mse_stack_cleanup_provenance`，approved=true，
绑定 task/scope/authority、deleted ownership_id、owner_cleanup、audit、delete_result、absence_result
各原件路径/SHA，明确 model_wire_unchanged=true、pass_added=false。程序重验原实际签名创建与
账号/会话、失败状态及实际网络创建日志、原模型 create used1/delete used0、独立同账号单次
DeleteStack→DELETE_COMPLETE，以及原精确网络ID/测试别名的只读消失证据和时间顺序。
新纠错操作获得独立 authority；原调用记录、合成未决记录及未使用模型删除额度均不修改。
只支持这一已证明清理的失败 MSE 来源，不是一般模板重试、额度重置或成功样本重测。
验收遍历历史代次时，只对已绑定批准纠错请求的原代重验该 OPS 清理链；当前代仍须完整正常清理。

收费新增来源也允许，但 packet 必须保留真实非零 `budget_cents`。独立 OPS 审查增加 `paid_operation_review`，绑定 `human_authorization/human_sha256` 和 `cost_evidence/cost_sha256`。费用材料为 `kind=ops_cumulative_fee_review`、approved=true，绑定 task_id/account/execution_sha256、完整 additional_operations、原人类许可路径/SHA、相同 budget_cents；明确 includes_previous_operations=true、estimate_basis、limitations 和逐件 SHA 的 official_sources。这不是实测账单或云端硬封顶，必须包含先前调用、新增操作、仍待执行维度及 provider/控制台费用盲区，不把收费 OSS 伪称免费。

同一任务已经预留足额的累计费用包时，OPS 可明确审 `budget_reservation_reuse={task_id,reserved_cents,cost_evidence,cost_sha256}`。程序独立核对本任务原 reservation 恰好等于非零 packet 预算、run 仍容纳该预算、费用材料和人类范围均未变；派发时仅新增预留0，原100等非零包完整保留，不退还、不重置、不复制云调用额度。无此独立审查仍按原规则累加预留。跨任务/金额漂移/原预留缺失/原证改变硬拒绝，不能借该机制免除新增操作和真实清理审查。

复测审查必须按 `执行SHA:维度` 核对累计调用，不能只查裸执行 SHA。新执行 scope/session 仍先独立核实实际身份；仅切换到 cleanup 不清除同一签名凭据的机器身份绑定，不应无条件重复 STS。产品清理指令允许沿用身份的前提是门禁仍核对相同账号、scope、完整会话和实际签名凭据；凭据轮换或绑定缺失仍必须重新核实，身份额度耗尽则停止并保全证据，不能改 journal、模板、名称或新 SHA 补满创建额度。OPS 派前须审核凭据有效期与执行/清理时间窗，不能因省下一次身份查询便假定轮换和清理风险已经解决。

packet 要有每个维度 cleanup_requests 和 cleanup_recipe；依赖/失败残留全部登记。实际费用不等于预留估算；收费项核对计费及预算，不把本地预算计数当云端费用封顶。用户广义免费/按量许可不授权任意付费和无限重试。无法核实成本/未知创建需要 OPS 原始证据核查，不能重新创建或修改 journal 伪装成功。

新版冻结资源设 execution_policy_version=2，审查逐条 read_policy，而非只审文字承诺：execute/cleanup 独立 grant id 和额度、GetStack 至少30秒实际传输间隔、每个创建物的 required_cleanup/after_delete 必查项。所有计划要求的精确 ID、名称及子资源消失读取应各自列明。程序在等待前不扣额度，取消等待不产生 wire 意图；每次醒来重新核对原完整会话、签名身份、阶段与累计额度。迁移只加可空政策列，不重写旧 grant、attempt 或 authority_limits。维护前备份原 wire 库，重载后对原列和全部行逐项摘要核对；服务恢复不算新测试 PASS。

修输入后原创建/删除都未调用，但新会话的身份查询已耗尽时，可独立审批一次只读身份衔接。新提案只把原 identity quota 加一；所有其他调用、参数、名称、模板、配额保持原样。OPS 审查 `kind=ops_identity_continuation_review`、`approved=true`，绑定最终 `proposal_sha256` 和 `proposal_execution_sha256`，在原 `carry_forward_dimensions` 维度中增加 `identity_continuation`（唯一 request_id、prior_scope、remediation_evidence/sha256）。程序只读核对原 GLM 终态、所有创建物已清理、原身份成功且账号一致、身份累计耗尽、无未知调用、该维度 create/delete 累计仍为0，沿用原 authority。没有新创建额度，不改 journal，不自动审批或省略新会话真实身份。再次消耗或任何参数变化都拒绝当前审查。

GLM 无固定回合/资源时限，不累加 ROS TimeoutInMinutes 来阻断派发，也不设置二十分钟能力或一小时新授权时限。ROS TimeoutInMinutes 仅按云 API 合法参数审核，不修改原模板或调用以绕过额度。执行能力绑定原任务/槽/完整会话，清理沿用原响应ID，直到安全释放槽后程序关闭；真实签名凭据到期仍由产品刷新并核实同账号，不能忽略实际身份。RPC、启动、单次 API 的故障超时不应升级为整个资源测试截止时间。

仅身份衔接按同一 authority 的全部原 scope 累计核验，而非要求最后一个 scope 自己耗尽累计次数。每个 scope 必须属于本任务、相同维度、已终态 GLM；身份调用与账号不变，实际响应全部成功，逐 scope 的 used 与原 attempts 数量相同，used 总和与 authority_limits 相等且累计额度已耗尽。旧 scope 的额度不得超过本次原额度；未知、跨任务、活动回合、调用漂移或计数冲突继续拒绝。允许独立审查下一次身份读取，不增加原查询/创建/删除次数，不写回旧 journal。

`inspect-cleanup <原GLM job_id> --dimension ROS|TF|CREATE` 是 OPS 只读恢复审查入口，不是续租/清理执行。要求本任务所有模型回合已终态、原任务仍持有原槽、原完整自建会话在同 target 已解锁且空闲；其他隔离槽的任务不阻断本任务清理。再从只读 wire 快照核对原 grants/authority/累计额度、实际成功创建响应及原签名账号，投影待清理ID。未知结果、绑定变化、计数冲突或缺少成功创建响应一律拒绝，不猜已有ID。返回 candidate_only、activation_allowed=false、cleanup_confirmed=false；删除额度0仍保持0。独立审查后才能使用下述 queue-cleanup 入口；candidate 本身不能解除 review_only 或写 PASS。任务没有创建且已合法释放槽时，不应调用此入口伪造恢复需求。

已成功创建但原删除额度仍为1时，OPS 独立核对 candidate 的原 job、维度、scope、authority、账号、owned_from_wire、精确 delete_call，保存 kind=ops_cleanup_only_review、approved=true 的审查 JSON。`queue-cleanup <原GLM job_id> --dimension ROS|TF|CREATE --evidence <inputs相对路径>` 再次验证原始 journal/会话/剩余额度，仅登记 cleanup_pending；不发模型、不调用云、不写 PASS。唯一 Luna 正常 next/execute 才启动同一资源原 GLM 的 cleanup 回合，保留原 attempt/槽/会话/authority；该回合只能 snapshot/wait/cleanup/continue-read(stage=cleanup)/cleanup-source/submit。只读续查还须原删除已成功且写额度耗尽；不允许转 source 阶段或其他维度。不能创建新会话、重装 grants、重置额度、换接口删除或补拍旧 PASS。原删除已用/未知、机器身份变化或额度耗尽均硬拒绝；真实清理原始响应仍由唯一 Sol 独立验收，不能用已排队当清理完成。

空 ResourceGroup 的删除前 `ListResources` 若尚未实际调用，独立审查另以 `predelete_read` 逐字绑定 fresh candidate 中该条完整 remaining_call。程序仅交付原 cleanup_allowed、从未尝试且仍有额度的精确前置读取；先取得同一签名来源的完整空成员响应，再执行原响应绑定删除。删除门禁本身不放宽；非空、失败、未知或已经尝试的前置读取仍拒绝，不补额度。删除后的 GetStack 不得移到删除之前；这个恢复入口不创建资源、不新增 PASS。

仅清理回合因已查明框架故障终态、实际删除尚未调用时，可在新的独立原始审查中绑定 `retry_job_id`、owning adapter 保存的 `retry_terminal_evidence` 与 `retry_terminal_sha256`，再给同一 queue-cleanup 加 `--retry-job <精确原cleanup job>`。程序重验原完整会话、所有权、同 authority 和删除剩余仍为1；核对同任务 cleanup 回合真实终态、审查 SHA 改变、该 retry job 尚未消费。只追加恢复审计和排队，保留前审查/回合/意图，不改变原 attempt、计数、能力或 PASS。未知提交、已删除、使用旧审查或重复消费同一 retry job 均拒绝；不能把这个入口当自动创建重试或清理额度续期。

同一资源后续维度或创建代次的清理，按 `original_job_id/dimension/authority` 独立保留恢复记录；任务指针只选择当前一次清理。旧记录与原审查不清除，切回已登记的相同绑定仍必须满足上述精确 terminal retry 门禁，不能借切换维度复制恢复机会。每次仍独立 inspect/审查实际创建响应、原会话和未消耗的原删除，所有本任务模型回合须终态；这个选择不新增云操作额度或 PASS。

原删除已经实际成功、额度为0，但异步消失尚未确认时，使用 `queue-cleanup <原GLM job_id> --dimension ROS|TF|CREATE --read-only --evidence <inputs相对路径>`。独立审查必须为 `kind=ops_cleanup_readonly_review`、`approved=true`，绑定原 job/dimension/scope/authority/account/session_id/owned_from_wire，并逐字保留 fresh inspect-cleanup 返回的 `successful_deletions` 与仅 cleanup 阶段、remaining>0 的 `cleanup_read_calls`。程序要求原 phase=cleanup、所有创建均有实际成功且耗尽的原删除、无未知结果；只有零计数而无真实删除响应不能批准。已有恢复回合的重新排队仍绑定精确 `--retry-job` 与新审查中的 owning-backend 终态原证及 SHA，不能重复消费。

执行阶段的 OSS GetBucketInfo 若有原签名身份、完整会话、精确2019-05-17/国内地域/bucket、实际HTTP403、SignatureDoesNotMatch/EC0002-00000201及一致的HostId/RequestId响应头，程序可单独识别为确定的只读签名拒绝，供原创建物的cleanup-only审查。inspect-cleanup显式返回rejected_detail_reads、原响应SHA与absence_verified=false；原unknown、原响应和已用额度不改。它不是不存在、通过或清理读取，不允许source续查，不能替代required_cleanup。未知写、其他403/500、缺签名身份/响应绑定/计数冲突或清理阶段读错误仍拒绝。OPS独立核对原删除从未调用且原响应绑定额度仍为1，按原审查queue-cleanup；实际删除成功后仍须全部原只读消失证据及Sol独立验收，产品签名问题另行修复，不新增创建额度。

该模式不新增或安装能力、不切阶段、不再次删除；发送前重新验证原审查和 journal，仅向原封存会话交付尚未消耗的精确 cleanup 读取。身份/名称等额度为0仍保持0，不能把其他读取替代它。必查项已耗尽时另行独立取原范围只读证据，保存原盲区；不能充值 journal。PendingDelete 仍是残留，读取续查和排队均不产生清理确认或 PASS，最终由唯一 Sol 核验。

## Server 崩溃或代码修复

### 保留现存测试资源的产品诊断

已执行任务因产品/门禁故障 blocked、仍占原槽且有未清理 owner 时，不应为了进入代码修复先删除
可复用样本。暂停、全部模型终态、排空 issued action 并备份后，可使用
`queue-preserved-repair <task_id> --evidence <inputs相对JSON> --reason <具体原因>`。
材料为 schema_version=1、kind=ops_preserved_product_gap、task_id、diagnosis、formal_test=false、
pass_added=false，绑定 packet_sha256、排序后的全部未删除 ownership_ids、original_job/review_job
各自的 id/thread_id/turn_id，以及 sources（state 相对 inputs/jobs 原件路径/SHA）和
code_evidence（repo 内 src/tests 路径/SHA/fact）。必须为最新终态原 GLM 与独立 Sol review。

该入口只排原唯一 Sol 的 product repair；原 attempt/slot/packet/所有权、未知记录、费用预占、
原 authority/调用额度和 PASS 全部保留。不激活云能力、不派 GLM，也不认定清理完成。
resume 后由唯一 Luna 正常调度；Sol 修复后必须 block 交 OPS，不能 approve 新 packet 或覆盖
原执行授权。后续同代续行/身份续核仍须单独实现并审查原证，不能把诊断排队当作续行许可。

### 凭据轮换后的清理身份续核

原任务已经成功创建、删除尚未调用，但当前签名凭据到期或轮换且原 identity 已耗尽时，
OPS 可独立审查一次 `authorize-cleanup-identity <原GLM job> --dimension CREATE|ROS|TF --evidence inputs/...json`。
必须维护暂停且所有模型终态，原 self-created session/target/账号/authority、实际成功创建响应、
原未调用删除和全部累计原证不变。审查 kind=ops_cleanup_identity_review、approved=true，逐字绑定
fresh inspect-cleanup 的 scope、authority、account、owned_from_wire、session（原 session_id）、
identity_call（原精确 STS 调用）、additional_identity_reads=1 和真实 reason；材料须0600。
程序只追加独立的一次身份读取及不可变审查摘要，不修改原 grant/quota/used、attempt 或旧身份，
不增加创建/删除。仅 cleanup 阶段可执行，不能在 execute 阶段借其创建。
恢复清理审查的 reason 必须写当前真实恢复事实与原证路径/SHA；程序将已封存的说明一并交给原产品会话，
避免旧失败被误当当前状态。说明不代替实际签名身份，原未消耗的身份/删除仍逐调用门禁核验。
之后仍独立 queue-cleanup，由唯一 Luna 派原 GLM 清理回合；产品正常刷新凭据后先单独执行
这一实际 STS 读取，返回 AccountId 必须与原账号一致，门禁才允许原响应绑定删除。
该 supplement 一次耗尽后不自动续期；审查改动、其他未知结果或已调用删除拒绝。
它不能替代 ResourceGroup 的同签名原栈/子资源/成员空证据，不能修补不存在的来源或记 PASS；
仍缺这些证据的资源单独按精确原证恢复，不强行派一次已知失败清理。

### 修输入后的精确只读衔接

首次纯 QUERY 已实际完成身份核验、但读取在传输前因参数不匹配被门禁拒绝时，不补造历史创建授权。
只审 `identity_continuation`：完整原提案 SHA、原 Sol 批准事件/终态和原 GLM 终态须能追溯；
同账号成功身份及累计已用次数、未调用的原查询逐项核验。仅身份累计 quota 加1，查询调用、参数、
额度和原 authority 不变。最终提案携带逐字相同的 `query_identity_continuation`，不再享受初次 QUERY 免审；authorize-creation
封存完整提案与独立 OPS 审查，运行时重验两者 SHA 和原累计 authority。已有但无效的原授权、
未知调用、未清理所有物、提案/账号/原读取漂移均拒绝；重启、新会话和新 attempt 不重置次数。

原创建/删除从未调用，但原身份及必要预查已耗尽时，OPS 可独立审 `read_continuation`，
不自动重置或复制写额度。它在原 `carry_forward_dimensions[维度]` 中点名唯一 request_id、
prior_scope、remediation_evidence/sha256 和 `grant_ids`（包括 identity，仅原 identity/read）。
仅这些已累计耗尽的原读 quota 各加1；所有调用、参数、名称、模板及其他额度逐字不变。
审查 kind=ops_read_continuation_review、approved=true，绑定最终 proposal_sha256、
proposal_execution_sha256 和逐字相同的 read_continuation_approved。程序核对同 authority
所有原 scope 属于同任务/维度/终态 GLM、真实成功身份账号一致、已用次数与原 attempts
吻合、所有创建物已独立清理、写次数仍0。只接受已有精确否定读取分类，原 unknown、
响应和次数不改；其他未知读/写仍拒绝。新会话仍实际核实身份，不拿旧身份直接放行。

多维度衔接使用外层 kind=ops_read_continuations_review、approved=true 和精确 carry；
每项 read_continuation_reviews 或 identity_continuation_reviews 单独绑定维度、最终
proposal_sha256/proposal_execution_sha256 与自己的请求。新增来源组合沿用原外层新增
操作审查，不以外层创建批准代替只读批准。请求ID不可换证据复用，审查不调用云。
非零预算必须保留实际数额，并通过原 paid_operation_review 人类许可/官方资料/累计
费用审查；已有足额预留只在原 budget_reservation_reuse 独立审核后增量为0，不称免费。

临时 OTS 样本准入可随上述独立只读衔接保留，但完整模板、父实例、名称、写额度及原
execution_sha256:dimension 必须与不可变准入相同。首次 authorize-creation 重验终态原 scope
与耗尽原读；授权安装后仅重验封存审查和结构绑定，不因合法后续回合已运行或消耗新读而
重新要求其终态。仅携带候选请求不解除只读限制。carry-only 的付费衔接同样核对实际已预留
金额及累计费用审查，独立批准复用时增量为0，不重复预留整笔预算，也不清零任何调用。

用户明确允许“成功清理后额外创建一张空 OTS 表”时，不能伪装成旧的
`original_table_never_created` 模板修正。仍用 OPS `authorize-temporary-samples`，但提供
`kind=ops_additional_empty_table_admission` 的独立不可变材料：精确冻结任务/计划 SHA、
人类新增一张 ROS 托管 Terraform 空表及累计费用授权路径/SHA、最终完整提案路径/SHA、
execution_sha256、sample_binding（原父实例/表名/地域/完整模板 SHA/empty_table_only）、
operation_review 路径/SHA。后者必须是已独立审查的精确 `ops_additional_sources_review`，
含真实成功耗尽前代创建/删除、清理、累计费用及原预留复用证明。仅补剩余 TF iframe，
完整调用/模板须与已验证前代一致。程序以任务及原人类问答固定新增数量权限，不因材料
复制、换名、改注释、attempt 或 request_id 重新发额度；原首次准入与 wire 永久不变。
仅暂停且排空可登记；这不是云操作授权，仍须独立 authorize-creation 和后续 Sol 批准。
运行期重验完整封存提案，但不错误要求新一代所有物先清理才能读取自身清理上下文。
该入口不授权 NAS、额外父实例、网络/IAM 写入或其他维度，也不自动补任何 PASS。

NAS 原空文件系统及挂载目标已独立清理、原 TF 两种导航仍未执行时，人类可批准一代空样本，
也可明确委托 OPS 在本任务期内按原范围持续完成缺项，不再逐代询问。
后者原话材料的 scope 须含 authorization_lifetime=task、sample_limit_basis=per_generation、
ops_reviewed_rebuilds_until_remaining_scenes_complete=true、cleanup_before_next_generation=true、
no_new_cost_allowance=true；每代仍只准一个空文件系统/自身挂载点/托管栈。
程序按真实最新 GLM prior_scope 固定唯一后继，改名/attempt/request_id/复制材料不能分叉。
两种许可均登记 `ops_additional_empty_general_nas_admission`。仍用
`authorize-temporary-samples`，原不可变准入和全部 wire 保留；审查绑定原准入、当前人类原话、
完整最终提案、独立操作审查、当前 expected-zero 费用审查和原 restricted network 原始来源，
每项均为 `{path,sha256}`。新提案只含原 TF iframe/open，完整模板、名称、账号、地域、
12条 grants、次数和响应绑定清理与前代一致；原 create/delete 各一次成功耗尽、全部所有物
清理、精确原回合终态仍由 additional-source 门禁重验。新权限独立绑定人类问答及原任务，
复制材料、改 attempt/request_id/名称不能复制同一代权限。持续许可也不自动创建：每代必须先查明
前代故障、验证修复、核对真实终态及清理，再独立审完整操作；不允许盲目重建确认已知问题。
原授权不续杯，不允许挂载或数据操作、
付费依赖或 IAM 写入。准入不是云执行许可，仍须 authorize-creation 和唯一 Sol approve；
运行期核对封存原证但不要求新代次先清理才允许读取自身验收上下文。测后真实清理及截图验收仍必需。


### 用户明确批准 ResourceGroup PendingDelete 收口

只有人类明确批准时，OPS 可运行 `accept-pending-delete <task_id> --evidence inputs/<资源>/审查.json`。
审查 kind=ops_user_accepted_pending_delete，绑定真实 owned、原创建证据、实际同账号精确 ID
ListResourceGroups 的 PendingDelete 原始响应和人类原话/SHA；必须诚实保留 absent=false，
cleanup_disposition=user_accepted_pending_delete。程序仅在暂停、排空且原任务 blocked 时登记，
不调用云、不修改 wire/额度、不新增 PASS、不释放槽，不允许其他资源或 Active/未知状态。
owned 的 deleted 表示“按用户授权视为已删除”，不代表云端已消失；原响应和授权事件永久保留。
同一资源组的原生 ROS 类型与 ROS 托管 Terraform 类型都可接受该精确例外；保留原始 resource_type，不能改类型绕过校验。每个新 owner 仍需自身实际签名身份、精确 ID 的 PendingDelete 原证及独立人类授权绑定；其他资源类型仍拒绝。
创建 API 的 wire owner 没有 resource_type 时，保留原 product/action，不手补 ROS 类型。程序仅接受 resourcemanager/CreateResourceGroup，逐件核对原 CREATE scope 下未改的成功 wire-source、完整会话、实际创建响应 Id/AccountId 与原 owner 一致；仍须当前 owner 的独立人类授权和真实 PendingDelete 原证。其他操作、跨代次、来源漂移与真实 absence 的要求不变。
之后 reopen-review，由唯一 Luna 调度唯一 Sol 核验原截图、其他所有物及缺项，继续正常流程。
不得为此伪造 confirmed_absent/absent=true，不自动推广到以后任务。

GLM 的 context/cleanup-source 另提供只读 `dimension_transition`，调用 request 相同的上一维度硬闸，不增加额度或接受清理。用户已允许本任务空资源组 PendingDelete 不阻塞进度时，全部实际必查读取、根栈删除终态和不可变清理源齐备即可继续其他批准维度；不要让“等子组物理消失”的指南覆盖该人类决定。最终每个新 owner 的 accept-pending-delete 仍须自身精确原证及授权，实际 absent=false 永久保留。阶段衔接观察不是 PASS 或资源消失证明，实际 request 会重新核验。

### 受阻维度与可执行维度分阶段

首次准备尚无 GLM/验收回执、但已有 QUERY 样本且缺 ROS 托管 Terraform 来源时，不要伪造
execution_gap 回执。冻结 `failed_navigation_managed` 仅缺 QUERY/TF 的任务可先准备纯 QUERY
阶段：保留原 `execution_selection`，TF 的每个 deferred scene 绑定同资源原始
`preparation_missing_input_handoff` 路径/SHA；另加 `preparation_selection`，精确包含
`job_id/thread_id/turn_id/path/sha256/frozen_plan_sha256`。原 Sol prepare 必须真实终态，且其
task_blocked 事件确实交接该文件、完整冻结 scene 集不变。此路径不创建假历史 verdict。
OPS 用 `kind=ops_initial_selection_review`、approved=true、精确 task_id/最终 proposal_sha256、
execution_selection_approved/preparation_selection_approved 和空 carry_forward_dimensions
独立审查并 authorize-creation。仅首次预算0、身份/execute只读各一次，无写/清理/额外来源；
准入拒绝本任务任何旧 GLM、packet、owner、观察、verdict、wire能力/调用或不同既有authority。
批准后的运行期重验不可变原证与完整提案，不再错误要求当前合法GLM不存在；原累计authority
不变，不能再次用此路径补额度。QUERY通过后仍保留TF未执行/缺来源、资源blocked，不可close。

该合法初次 QUERY 阶段因采集问题需另审一次查询时，保留原 preparation_selection 与逐字相同
execution_selection；提案附 `preparation_selection_continuation={"initial_proposal":"inputs/R编号/原初次提案.json",
"initial_sha256":"原SHA"}`，以及唯一 QUERY 的 plural additional_source_requests。原初次提案必须
已有有效独立授权，原 worker 真实终态、同账号签名身份和所有原读取各一次成功且累计耗尽；
调用/配额不变、预算0、无写/清理。OPS 使用 ops_additional_sources_review，另外逐字批准
preparation_selection_approved、preparation_selection_continuation_approved、execution_selection_approved，
空 carry 与精确 additional_sources_approved。原始准备交接、冻结 TF 全部缺项、原 wire 与旧图
不改，不伪造 TF 验收回执；这是一次独立新 QUERY 操作，不重新使用初次准入，也不自动重试。
approve 先核验完整分阶段授权，再要求当前执行维度的官方适用性已确认；暂缓TF可保留unknown。
未审分阶段、当前QUERY仍unknown或普通完整任务有unknown时依然拒绝，不靠伪造supported解闸。

同一资源某个维度已独立验收为 execution_gap / product_gap / fail，且尚有可合法执行的其他维度时，可审 `packet.execution_selection={"dimensions":["CREATE","QUERY"],"deferred_scenes":{...}}`。这是当前全部缺项维度的非空严格子集，不改变冻结计划、四维度适用性、原缺项、历史截图或 PASS。每个暂缓 scene 都必须有 `{reason,path,sha256}`，绑定本资源 `receipts/` 下已终态的唯一 Sol 原验收回执；程序重验 SQLite 原回合及 verdict，不接受模型自述或伪造新 gap。

独立 OPS 审查必须包含 `approved=true`、精确 task_id、最终完整 proposal_sha256、逐字相同的 execution_selection_approved。requests、execution_grants 和 scenes 只包含本阶段维度；未新增操作的每个维度必须且只能用 carry_forward_dimensions 保留原提案/SHA/完整调用/次数及累计 authority。确需新增操作仍走上述逐操作额外审查，不能用分阶段复制造云额度。非零预算仍保留真实累计费用审查；没有新增操作时费用材料的 additional_operations=[]，原足额 reservation 仅在独立复核 budget_reservation_reuse 后增量为0。

GLM 与 Sol review 的 context.expected_scenes 是当前批准阶段，all_expected_scenes 保留全局缺项。暂缓维度不能 request/source/cleanup/click/evidence 或写 observation/verdict。唯一 Sol 仍独立验收本阶段原始来源、普通/Shift 原图、创建检查点及全部未清理所有物；受阻旧回执不重放。当前阶段通过并真正清理后释放槽，但尚有全局缺项则资源仍 blocked，明确保留原暂缓原因，不能 close 或宣称全通过；本阶段失败仍进入原修复队列。后续按事实解决权限/输入/产品问题后重新准备剩余项，不自动循环重复已消耗调用。

仅清理恢复在发送前重验冻结审查 SHA、原 scope/authority/完整会话和原响应绑定删除参数；从实际创建响应派生的精确调用作为机器段交付，不能让模型补 API 可选默认参数。此段只用于删除本任务已经创建的对象，不用于创建/查询生成引用；仍经过原签名与传输门禁，不扩展参数、次数或授权。任何参数漂移、已用删除额度或原证改变均拒绝提交，不改 journal、不重放 PASS。

cleanup 在写入提交意图和启用删除阶段之前，先核对原完整会话已解锁、空闲；如果本 target 的资源预览还开着，程序点击固定关闭按钮，再核对同一会话的预览已关闭。关闭失败不提交清理、不写未知提交标记。已经存在的未知标记不删除或自动重置，仍由 OPS 独立查明原 wire 调用后走 cleanup-only 恢复；不重放导航或创建。

只补一个维度的新来源、其他维度精确操作不变时，OPS 审查 JSON 可含 `carry_forward_dimensions`：逐维度原 proposal 相对路径、`proposal_sha256`、`execution_sha256`。authorize-creation 验证原有效独立授权、同任务/账号、完整调用/内联模板/次数完全相同，把原累计 authority 键保存在私有授权的 dimension_authorities。实际 request 与 cleanup inspection 都使用该键；不会复制或修改 wire journal，不会重置已用次数。GLM/Sol packet 的 advisory authority_plan 不具有授权效力。任何参数/配额改变必须另审，不能伪装 carry-forward。

补充来源与未执行缺项可在同一 packet 合法组合。单项保留 `additional_source_request`；多个明确操作使用 `additional_source_requests` 数组，两种格式不能混用。数组每项有独立 request_id、维度、原 scope、原提案/SHA 和修复材料/SHA，维度及 ID 不重复；OPS 审查为 `kind=ops_additional_sources_review`、`approved=true`，`additional_sources_approved` 必须与完整数组逐项相同，绑定最终 proposal_sha256。每个操作独立核对原创建/删除、终态及所有创建物已核验清理；QUERY 只能是原有同账号、精确有界只读调用及身份的另一次明确审查，不允许混入写操作。新增操作的累计 authority 绑定本任务和该操作本身，不因组合、排序、packet 改写而重置。其余全部 execution_grants 维度必须且只能在 `carry_forward_dimensions` 保留原 authority。补充维度不能 carry；不重放 PASS，不扩大范围，不自动续额度。

原 QUERY 提案已合法续核身份、identity quota 大于1时，新增只读来源仍只审一次新身份读取。
在该 QUERY 请求显式绑定 `query_identity_quota_reduction={"grant_id":"identity","previous_quota":2,"new_quota":1}`
（previous_quota 必须等于实际原额度）。新提案只把这一身份额度减为1，其他读取调用、参数和额度完全不变。
程序只读重验原授权及同 authority 全部 scope 的终态、实际成功同账号身份响应、逐 scope 次数和累计耗尽；
原提案、quota、authority_limits、attempts 均不修改。新一次操作仍须独立逐项审查、费用及最终 SHA 绑定。
此入口不适用于创建维度，不允许增加读取、复用未耗尽额度、未知响应或复制创建机会。

补充来源与仅身份衔接组合时，外层保留上述新增来源审查；对 carry 维度中的每个 identity_continuation，另在 identity_continuation_reviews[维度] 内显式审查 kind=ops_identity_continuation_review、approved=true、dimension、最终 proposal_sha256/proposal_execution_sha256 及逐字相同的 identity_continuation_approved。不能把外层创建批准当作身份批准；缺少、额外、错维度或错摘要均拒绝。原身份耗尽、剩余读取、写操作未调用和终态/清理核验仍执行原规则，原 authority 和 wire 计数不变。

NAS DescribeAccessGroups 对精确组名及 standard/extreme 文件系统类型返回真实 `InvalidAccessGroup.NotFound`、HTTP404 和非空 RequestId 时，程序可识别为确定的否定只读结果，避免将其误作未知创建阻断后续阶段。原 wire 的 unknown 状态、响应及消耗次数仍完整保留；这不是成功调用或 PASS，不适用于其他错误或写操作。清理仍须原成功删除、原栈删除终态与独立原始消失证据，Sol 按原图和来源验收。

这项精确否定只读判读同样适用于 additional-source 的原 journal 核查；未知写、其他错误、缺少 grant 或 RequestId 仍拒绝，不改任何计数。NAS 元数据补图仅是原有视觉字段的真实只读 UI 采样：保留原落点图，查原批准精确组名/描述，取消弹窗，不削减 render_text，不把规则页缺字段伪装为通过。OPS 提供原始控制台证据后由 Sol 显式选本场景策略并独立 inspect 双图，操作授权仍单独原始审查。

ROS SNatEntry 父级元数据修复后的定向复测，可在单项 `additional_source_request` 中使用
`ros_snat_parent_binding`，精确绑定 resource_type、stack_id、logical_resource_id、identifier、
snat_table_id、nat_gateway_id、region_id。只接受原终态 ROS 场景、原签名账号、原成功
GetStackResource 的真实物理标识，且原全部读取已耗尽。独立 OPS 审查最终提案 SHA 后，
仅允许一次身份、一次带 ShowResourceAttributes 的原逻辑资源读取、一次精确 SNAT 读取、
一次精确父 NAT 读取；后三项均为 execute 只读，VPC 两项固定 PageNumber=1/PageSize=10。
不增加写操作、不修改旧 wire、不接受换 request_id 复制同一父级操作。元数据用于保留
ROS 来源的真实父子关联，不记 QUERY 或创建通过；原 ROS iframe PASS 不重放。

程序支持 `sol-backend-start` 启动并登记自有 Sol app-server（4502），这不创建模型回合。端口占用时拒绝启动，不杀无关进程。

SLB ACL 补图只接受自身真实来源的 AclId、AclName、IP版本与国内地域，限定 `slb_acl_metadata`、SLB 类型和实际 slb/slb4service 同源页面。OPS 诊断原图先证实详情缺 IP 版本、列表同一 ID 行可读，框架才能用固定侧栏与原生横向滚动补拍。OOPIF 的缓存 URL 为空时读取精确 frame 的真实 document location 并严格校验，不把空 URL 当放行依据。固定列遮挡时根据实际文本/固定列几何调整滚动，不改样式/隐藏 DOM；失败保留，不自动降字段。原图和補图由 Sol 分别 inspect，OPS 诊断不记 PASS、不重放历史 PASS，不扩展原云操作额度。

同一 Server 换代码后：`pause` → `backup <新备份路径>` → `server-stop` → `status`。最后一步自动启动同一 16099 Server，加载原数据库；不执行 init，不产生模型新回合。独立 adapter 重新读取私有描述文件，Server 重启本身不重发 turn。

OTS 已审枚举纠正的 admission 与运行期 context 必须区分：新 OPS 审批仍全量核对前代终态、
清理、配额和原证；已批准同 packet 的只读 context 重验不可变授权/原失败/官方来源/费用和
精确 authority，不要求当前代 owner 先被验收 deleted，否则会挡住 Sol 自己的清理验收。
此分类不标已清理、不添加 PASS、不增加写额度；实际清理仍由唯一 Sol 根据本代原始 wire 核验。

ResourceGroup 详情补图只允许 `resource_group_metadata`、真实来源精确 Id/Name/DisplayName、
account 级 global 范围与国内 api_region。保留原 Overview 落点图后，只点击同一详情四标签栏中的
原生“设置”，不点同名侧栏，不编辑/保存/删除或构造 URL。只在该组当前活动内容 pane 内核验
三项完整可见字段；登录、会话、frame、实际路由变化或遮挡均保留诚实失败。自身签名来源字段
不符时不得开始补图。Sol inspect 原图和独立 metadata 图后显式核验；该路径不证明权限、
新增来源授权、已删除对象可用或 PASS，原 FAIL/PASS/调用记录及 PendingDelete 例外不改。

ECS SSHKeyPair 的列表补图只接受 `ecs_keypair_metadata`、本场景精确密钥对名称、完整32位指纹、已审 ECS 类型与国内地域。先保留原普通/Shift点击落点图，再在实际 ecs/ecs4service 同地域列表用固定原生“密钥对名称”搜索控件筛选；唯一同名 data-row-key 行必须展示自己的完整指纹。仅允许原生导航折叠与横向滚动，操作后恢复折叠状态；不构造 URL、改样式、隐藏固定列、扩大截图或调用创建/删除。视口仍不足、遮挡、加载、登录或来源缺口都保留原图并如实报缺项。已有源的机制诊断不计正式 QUERY 通过，也不重放旧 PASS；未来补测仍需自身新会话、原始签名来源和精确操作审查。

代码尚未重载时，status 保留数据库事实并给出 `control_runtime.ready=false/requires_ops=true`；next 返回 maintenance wait，不发行 action。execute/launch/resume 仍拒绝旧代码执行。CLI 的 HTTP 拒绝不作为“服务不存在”处理，不再由此启动第二 Server。维护完成后核对 ready 和 import-time SHA 再 resume；HTTP 可达不代表测试通过。

恢复期间只需要 Sol 审原图/修复、但执行环境尚未就绪时，OPS 设置 `review-only --reason <非秘密原因>`，再 `resume`。review_only 独立于全局 pause，持久跨重启；next 仅产生 review/repair，execute、launch 和提交边界再次硬拒绝 prepare/GLM，旧 issued action 失效。已在跑回合不被标完成或抢占。唯一 Luna 仍走原 next/execute；OPS 不派模型。只有 OPS 独立确认凭据、环境、输入和授权均恢复后才 `dispatch-enable`，普通 resume 不解除此闸。用户暂停仍用 pause，停止所有新模型调度。

1. pause（停止新派发），status 查看精确 jobs/slots/资源。
2. backup 到新文件；check 校验 SQLite。磁盘满或 integrity 失败，保存全部目录，不能 init 替换数据库。
3. 修程序与离线测试。Server 使用 server.lock 单实例 OS 锁，数据库 WAL/FULL；重启直接 next 恢复同一状态，原 model adapter 不因 Server 重启盲目再提交。
4. 独立 adapter 已确认 thread/turn：从对应 app-server thread/read 读取原始精确回合。未知提交不能重发；reconcile --raw 只接收匹配已绑定 thread/turn 的真实终态响应。
5. 未知 thread 或只有 thread 无 turn 时，保留 unknown 租约，先查 backend 原始线程/回合找到精确键；不自动 new。需人工决策时报告，不回填伪造响应。
6. 执行没有 submit 也进入 reported/blocked，不自动通过；Sol 只补缺项。已有截图原图和 PASS 永不重放。

正常 Sol product repair 的安全屏障是 repair_scope=product、task=`repairing` 且所有 GLM 回合已终态，不是要求执行中的 repair 同时看到全局 paused=true。input repair 不写共享代码，不触发全局排空。OPS 维护/登记时用 pause，加载完成后 resume 让唯一 Luna 调度；产品修复期间程序已有屏障阻止新的执行派发。context.repair_safety 从原 SQLite 返回精确范围、绑定、唯一 Sol 与排空证明，repair_file 在每次源文件写入的同一事务重新核对。不要以口头“已暂停”替代程序证明，也不要为了满足错误的全局 pause 前提反复重派空修复回合。

桌面打开由4502托管的 Sol 后可能持有原 rollout 写锁，使独立后端 resume 报 active writer。原会话必须先核对无活跃回合；可对同一个 Sol 做可恢复的归档释放桌面 writer，保留完整线程 ID/历史，不能杀主桌面 app-server 或另建 Sol。adapter 会通过 **4502 自己的 thread/unarchive** 恢复同一个线程，再 resume；不要立即调用桌面取消归档，它会再次持有 writer。框架运行时不要从桌面打开/读此 Sol，完成后可正常查看。只读检查使用 `node adapter.mjs <state绝对路径> <job_id> --read`，将真实 thread/read 原响应保存为私有文件，再交 reconcile。adapter 已退出、尚无 thread_created/turn_created 轨迹、后端全部回合真实终态且没有本 job 输入，才可分类 not_started；RPC 未知但可能已 turn/start 的情况仍保留 unknown，不能借此重派。

GLM 的 new-session 通过 UI 新对话和不调用工具的开场消息实体化产品会话，随后才绑定侧栏完整键和云能力。OPS 不代建会话。正式产品仅在测试门禁开启、完整键匹配时读已审批精确调用作为动态系统指令，便于使用固定内联模板；这不是原始来源，更不产生资源引用或扩大 wire 能力。普通产品无此指令。

空闲槽的旧会话预览可能遮挡新会话按钮。new-session 在原精确 target 的前台锁内先核对空闲、解锁；仅用固定关闭按钮关闭这一个已知完整旧会话的预览，再核对旧键未变和遮罩已消失，才继续 GLM 自己的真实 UI 新会话操作。关闭失败、旧键变化、忙碌或锁定继续拒绝，不代建会话、不猜键、不导航其他标签。该恢复不产生截图 PASS，也不安装或重置任何云能力；历史截图和会话保留。

模型 transport 与脚本请求/响应必须绑定当前 lease 的 thread/turn。历史动态工具证据照旧保留。Server 描述文件可重建，state.sqlite3、inputs、jobs、receipts、slots/config 和 Chrome profile 不能丢。

OPS 的 adapter --read 通过同一 Server 的只读 job-binding 获取 SQLite 持久 thread/turn/role（包括已终态 job），并核对启动标记与原始 transport 是否冲突。新 GLM 的 launch.thread_id 为空是启动时状态，不代表未建线程；不可手工填标记或仅凭轨迹猜绑定。读取只调用所属后端 initialize 和只读历史接口，不 resume/new/turn/start，完整原响应存 0600 文件；绑定缺失、冲突或后端返回不含精确回合均拒绝，不切换后端。

长期 Sol 的全历史 PNG 可能超出单个 WebSocket 响应大小。已绑定精确 turn 的 Sol `adapter --read` 使用所属后端的 metadata-only thread/read，再用 thread/turns/list 的单回合分页找到原键、itemsView=full 读取该完整回合；保存原始 metadata 与每个分页 RPC 响应，并标明 evidence_scope=exact_bound_turn_full。不省略该回合的工具/图片、不拿 summary 代替原始证据、不 resume/订阅/重派；缺失回合、重复游标或非 full 返回均拒绝。GLM 原始核查仍保留完整 thread/read；尚未绑定 turn 的 Sol 使用下述完整分页文件。

尚未绑定 turn 的长期 Sol 同样逐回合读取 full，但每份原始 RPC（含原图）单独保存0600文件，
索引标为 all_turns_full_paginated_files，避免全历史超过单响应或 JavaScript 字符串容量。
reconcile 独立逐文件核对 SHA、原 thread、游标连续/终止、真实终态及没有本 job 输入，
不信索引的摘要或模型总结；完整证据不删除、不裁图，未知原提交仍不可重发。

若 Sol 的 context 在连接后端前失败，可能尚无 raw-rollout。仅对持久绑定原 Sol、
turn_id 为空且 submission=not_submitted 的 job，adapter --read 可记录轨迹 missing/empty
这一事实并完整读取所属后端历史，不补造轨迹。reconcile 仍须核对同一绑定、实际已退出的
adapter PID/身份、全部完整分页及无本 job 输入，才分类 not_started；不能据缺文件自行重派。
查询临时样本的真实成功依赖也按 QUERY 和原 GLM 创建代次登记所有权；这不开放模型的
QUERY register_owned 或任何云调用权限，不以登记代替删除及独立验收。

长期唯一 Sol 历史会累计原始 PNG。派前只读核查使用 thread/read(includeTurns=false) 与 thread/turns/list(itemsView=notLoaded) 的完整状态分页，任何未知/活动回合、重复游标或页异常均拒绝；thread/resume(excludeTurns=true) 保留同一线程和全部持久历史，但不把整份图片历史塞进响应。OPS 独立原始证据读取仍使用 owning backend 的 adapter --read，不能拿元数据替代验收截图或未知回合核查。

桌面已加载的线程可能忽略 `thread/resume` 的 sandbox 参数；adapter 必须先以 `thread/settings/update` 明确设当前 job workspace-write / network / never（唯一 writableRoots 为本 job cwd，排除通用 tmp），再核验 resume 的实际返回，并在 turn/start 再显式绑定相同策略。网络用于普通脚本连接本 job 桥，不开放任意云/浏览器操作许可；不能因为已有线程返回 full-access 而降低核验标准。策略拒绝发生在 turn/start 前时，OPS fresh thread/read 确认所有旧回合终态、没有本 job 输入、adapter 已退出，才可 reconcile 为 not_started。只有 wire journal 没有本 job 调用/活动能力且无未清理所有物，程序才释放未执行槽、返还本次预算预留并回到原 approved packet；不增加 PASS、不清零云调用次数、不伪造清理证据。

持久线程切换 job 时，resume（包括 archive/unarchive 后）仍可能保留旧 developer 消息中的脚本路径
或执行阶段；本机 settings/update 没有指令更新字段。adapter 先按 owning backend 完整核对所有回合
终态并验证当前 sandbox/cwd，再在 turn/start 前用 thread/inject_items 追加本 job 的真实 developer
指令，明确仅替代旧 job 路径/模式、保留所有安全要求。线程 ID、原始历史、云调用及所有权不变；
不篡改 rollout、不换 Sol、不复制客户端描述符、不为更新指令反复归档。追加或提交结果未知时不重发，
交 OPS 核对原精确回合。旧路径桥接拒绝不算产品失败，更不自动通过。

共享桌面曾抢先回答 dynamic-tool 请求，返回 `Unsupported dynamic tool namespace: null`。
当前已用普通脚本替代：不注册 dynamicTools，不靠归档/恢复抢工具订阅，不要求用户躲开工作会话。
app-server 的独占 writer 仍存在：仅在后端明确返回 already has an active writer 时，程序再次核实
全部旧回合终态后，最多一次归档/恢复同一线程交接空闲 writer；不改线程 ID/历史、不重发 turn。
真正活动/未知回合或 RPC 结果未知不走此恢复，也不自动重派模型。
若独立后端连 thread/archive 都返回同一写锁冲突，说明 writer 在另一个进程；适配器不会杀进程或循环重试。
OPS 先用 owning backend 核实全历史终态，再用 lsof 核对该精确 thread-writer-lock 持有者。
由原桌面持有时，可通过桌面的 set_thread_archived 临时归档该唯一空闲线程以释放原 writer，随后由
原执行后端 unarchive/resume 同一 ID。不得删除锁文件、终止整个桌面后端、换 Sol 或启动新回合试锁。
旧线程工具定义可能仍被后端恢复，但开发者/回合指令明确只运行 workflow-agent.py，adapter 拒绝旧工具请求。
脚本每次调用原 Server 的 job 接口，临时桥令牌只绑定当前精确 job/thread/turn；总令牌、capability 和云配置不传远端。
本机客户端安装到 job/sandbox；Mini 仅部署客户端和 0600 描述文件，用 SSH 自动分配的任务专用 loopback 端口。
adapter 父管道 EOF 关闭本任务通道，原 Web/CDP/GLM 总链路不变。设置连接检查超时不是 GLM 时限。
原始 script/request、script/result/rejected 与 transport 轨迹保留于 raw-rollout；图像载荷不写轨迹，原图 SHA 和本机原文件不变。
Sol inspect 后用原生 view_image 看 image_path 才能验收；diagnostic_only 不因复制而升级。
失联或未知调用只核查原回合/原账，不自动 reconnect/retry、不重建资源。无实际点击仍为 execution_gap。

无执行槽的 blocked 任务，确认原因已修复后可 `retry-blocked <task_id> --reason <原因>`。曾执行且保留槽的 blocked 用 `recover-blocked <task_id> --proof <原始证明JSON> --reason <原因>`；先 reconcile 到全部回合真实终态，证明必须包含 task_id、每个 owned resource 的 cleanup，创建结果未知时另有 creation_uncertainty_evidence。程序不增加 PASS，只释放可证明安全的槽并进入 repair。

若只是验收工具未暴露已有原始证据路径（非清理失败），修复 artifact_index / read_artifact JSON pointer 后可用 `reopen-review <task_id> --reason <原因>`：只允许全部回合终态的 blocked 任务，保留 owned、槽、观察和已通过结果，回到同一 attempt 的 Sol review；不创建 GLM、不释放资源、不直接写 PASS。验收通过清理后再补执行缺项。

创建代次修复后，已 retry 的 pending 任务若程序核实真实成功 wire 创建缺少自己代次的 owned，且所有回合终态、无待消费派发动作，也可通过同一 reopen-review 回到原 attempt 验收。只补原始证据与该代次的清理证明，不更改旧 owned/计数/PASS，不创建新云对象；正常 pending、无执行历史及已 complete 的任务仍拒绝。

旧恢复记录已经真实核验删除，但 owned.evidence 仍保留创建源时，可在上述命令加 `--proof <原cleanup证明JSON>`：只绑定本任务已经 deleted 的精确 owner、原资源对象、成功消失证明及原始 SHA，逐项保留旧证据引用审计。不能把 present/unknown owner 标 deleted，不能代替清理或 Sol 截图验收。新的 recover-blocked 自动持久绑定原删除证明；保留创建截图验收仍逐项检查证明/原响应 SHA，无需重复创建。

每份 cleanup 证明绑定 ownership_id、原始 resource 对象、absent=true、failed=false、source_evidence 与 source_sha256；引用实际有界云核查响应。未知创建证明覆盖全部执行的创建维度，confirmed_no_untracked=true 并绑定原始证据 SHA。没有查明资源残留，不写这些确认值，也不释放租约。

原 ROS PrefixList 因 AddressFamily 大小写返回明确 HTTP400 StackValidationFailed、无 StackId 时，wire 的 unknown 和消耗次数保留，不能当成功创建。OPS 独立核实原签名账号、同 scope/session/凭据、原成功精确列表名空响应及同账号精确栈名 ListStacks 完整零结果后，将逐调用原始响应、路径/SHA 和原 journal 保存为 `kind=ops_independent_rejected_create_absence` 的 inputs JSON。所有本任务回合终态且无未清理资源时，执行 `reconcile-rejected-creation <task_id> --evidence inputs/<资源>/原证.json`，程序仅追加精确 attempt 与证据 SHA 的恢复审计；不改 wire、额度、所有权或 PASS。最终验收重验原证与原 journal，只有这一已证明未创建的拒绝调用不再永久阻挡。其他未知写、500、无 RequestId、已返回 StackId、名称/账号/原证变化仍拒绝。输入修正需要新的精确 ROS 操作提案和 OPS 审查，其他维度沿用原累计 authority。

同一入口也支持精确的原生 ROS Role 模板校验拒绝：只有单一 `ALIYUN::RAM::Role`，原 `Action=["sts:AssumeRole"]` 被明确 HTTP400 StackValidationFailed 拒绝、无 StackId；原签名身份、精确 GetRole HTTP404 否定预查和完整零栈预查一致，另有同账号独立身份、原栈名完整零列表、原角色名 GetRole SDK 原始 HTTP404。原四条 wire 和全部独立原证逐项/SHA 校验；其他 Role 错误不通用放行。恢复仅追加审计，不补创建额度或 PASS。原生 ROS 的 Action 表示由 Sol 按官方 schema 修正，不能未经审查改变直接 RAM API 或托管 Terraform 的策略输入。

NAS 原生 QUERY 的 CPFS 订单明确返回 `COMMODITY.INVALID_COMPONENT` 时，同一入口只接受
`kind=ops_independent_rejected_native_nas_create_absence`、schema_version=1、approved=true。
绑定 task_id/original_job/attempt_id/actual_account、完整原 creations 导出 wire_source 和独立
independent_absence（各为 inputs 路径/SHA）；confirmed_no_residual/no_journal_mutation=true，
new_creation_authorized/pass_added=false。必须为原固定杭州 cpfs/advance_200/3600/720/PAYG
非 DryRun 请求，原 HTTP400、正文和响应头 RequestId、商品错误内层 RequestId 完全一致、无文件系统ID；
原签名身份/会话/工具键/精确响应绑定调用、已用单次额度，以及本代自身网络已提交清理逐项核验。
独立原始报告须证明同账号身份和创建之后对该独立 VPC 的 CPFS 完整零列表，保留真实调用与时间。
这只追加已核实拒绝的解释，允许原诊断输入交给 Sol；不改 unknown/调用/额度、不批准新订单或 PASS。
参数修正仍须官方依据、完整新提案、当前成本和独立逐操作审查；不得重复原已知失败参数。

托管 MSE 原 dev1 栈根创建成功、子集群订单被 `IllegalRequest` 拒绝时，不能冒充上述根创建校验失败。仅对已独立清理的原失败代次，可用 `kind=ops_reconciled_failed_managed_mse_member` 输入调用同一 `reconcile-rejected-creation`：绑定 predecessor 路径/SHA、原 authority、cleanup_request 的 prior_scope/external_cleanup，以及完整原 creations 导出。程序重用 professional corrective 的严格清理校验：原固定三成员模板、全部原 wire/签名/额度、失败回滚日志、实际单次 OPS DeleteStack、DELETE_COMPLETE、同账号原 VPC/VSwitch 精确空列表和 MSE 原别名空列表、原已删除 owner。只登记 `父attempt-mse-unresolved` 这一派生未知成员的解释，不改变实际根创建、wire unknown、额度或 PASS；部分已返回成员 ID、原代次未清理或未知归属仍拒绝。允许后来代次仍持有自己的已绑定 owner，但最终 accept 必须另行核验后来代次的每个真实创建和清理，不能用旧清理抵扣。原回合须全部终态；最终验收再次核对所有原证 SHA 和原 journal。

SLB 2014-05-15 的精确 DescribeAccessControlListAttribute 只读 HTTP400/AclNotExist，可按官方公共错误码解释为否定读取。仍需原始非空 RequestId、slb.aliyuncs.com HostId、实际传输状态、批准的地域/响应绑定 AclId；错误头与正文矛盾拒绝。分类不改 unknown、额度或所有权，不单独证明已清理；必须同签名 scope/会话、原创建响应、实际成功删除之后的精确 ID 及完整同名列表原证交 Sol 独立核验。清理来源导出缺失时只能恢复原会话现存响应和原 wire，不再次删除，也不能把错误页当导航 PASS。

准备阶段已证实产品缺陷、尚未执行 GLM 的 blocked 任务，不必先派一次已知失败测试才能修代码。OPS 先 pause、排空所有回合、备份；在 state/inputs 保存 schema_version=1、kind=preparation_product_gap、task_id、diagnosis、sources（inputs 相对路径及 SHA）与 code_evidence（repo 内 src/tests 路径、当前 SHA、fact）。然后执行 `queue-preparation-repair <task_id> --evidence <相对 inputs 的 JSON 路径> --reason <原因>`，再 resume，让唯一 Luna 正常 next/execute 调度唯一 Sol repair。命令硬拒绝已执行 attempt、任何 GLM 历史/所有权、本任务 wire 调用、活动能力、未知调用、未终态回合或未消费 action；不授权云创建、不清零计数、不改 PASS。过期的其他任务能力记录保留。代码/来源 SHA 变化须重新独立核对，不能复用旧缺陷判断。此路径不能替代执行后 recover-blocked 的真实清理证明。

其他任务已独立验收收口、所有原回合终态且全部所有物已删除时，其保留的旧否定读取或已核实拒绝调用可继续保留 `unknown` 原状态，不阻塞本任务的准备修复。程序必须同时核对原 GLM、原任务 complete、独立 acceptance_committed 的非重试收口以及无未删除所有物；未知 scope、未收口/未验收任务或仍有残留继续拒绝。本任务任何 wire 调用仍拒绝此入口，不能以历史收口为由重置它的调用或创建额度。

OSS GetBucketInfo 的确定否定只读结果只接受原批准版本 2019-05-17、精确 bucket 参数、实际 HTTP404、NoSuchBucket、同名 BucketName 和非空 RequestId；存在响应头 RequestId 时须与正文一致。原 unknown 状态、响应与已用额度不改，这不是清理完成或 PASS。清理恢复审查还独立核对原 scope/full session/签名账号；未知写仍拒绝。PutBucket 的所有权和删除目标只能通过共享 response_identifier 校验实际成功响应的空正文、Location 与 RequestId，再去掉一个前导斜杠，不能借请求名或通用 URL 拆解补造 ID。

原 GetBucketInfo grant 使用 `$created_id` 时，判读只从同 scope、完整会话、唯一成功 PutBucket 的真实 Location 投影名称；核验原 authority/累计次数、实际签名身份和精确 HostId。凭据轮换还须已冻结 OPS 单次 cleanup identity 审查及真实同账号 STS，不接受仅账号字符串相同。原 unknown/响应/次数保持；删除先后和必查项另核验，不能据判读自动写清理或 PASS。后续 DeleteBucket/ListBuckets 的 wire 与产品来源保存真实 status、非秘密 RequestId 和未改正文；历史丢失字段不补造。官方允许完整 ListBuckets 省略分页键，明确截断/游标仍保留，不默认补 false。

RAM 2015-05-01 精确 GetRole/GetPolicy 否定读取只接受真实 HTTP404、EntityNotExist.Role/Policy、ram.aliyuncs.com HostId、非空 RequestId，以及 Message 中与原参数逐字相同的名称；GetPolicy 必须为 Custom，参数不允许额外过滤或通配。新门禁应保存实际传输状态和未改写原正文，不能因 SDK 原证附带 statusCode 就假设产品正文也带此字段。旧 journal 和已用额度不改；其他错误、未知写及缺少状态来源仍拒绝。预查不存在不是删除后证据，原成功删除之后仍需实际精确读取，由 Sol 独立验收，不自动写 absent 或 PASS。

RAM 详情读取的原授权参数为 `$created_id` 时，不从错误文本猜名称，也不改写 grant。只读判读在同一原 journal 快照中核对唯一成功 CreateRole/CreatePolicy、相同 scope/完整会话/实际签名凭据及 STS 原账号、原 response_path/target_param、相同累计 authority 与已用次数，再从该成功创建响应投影名称，交上述精确否定规则核对。来源导出、continue-read、cleanup inspection 和新增来源的前驱核验复用同一判读。清理完整性仍另查该读取确实在成功删除之后；此机制不批准新调用、不把 unknown 改成 succeeded、不记 PASS。原创建响应或签名绑定缺失、路径/名称漂移、重复创建响应、额度冲突继续拒绝。

切换下一维度的 previous-cleanup 门禁也复用此原响应绑定判读，并逐项核对全部已审 required_cleanup 在原成功删除之后实际执行；缺少原 grant 的 attempt 硬拒绝。清理来源导出、ROS 删除终态和独立 Sol 消失验收仍必需。恢复只解除已查明的误拒，不新增调用、改写 journal、补额度或产生 PASS。

## 产品 Web 修复

SAE Application 的描述不能由应用名子串抵扣。`sae_application_metadata` 只允许
同账号/地域、精确 appIds 筛选的成功 ListApplications 唯一行，逐字段绑定 AppId、AppName、
AppDescription 与 sourcePath/toolUseId。原生基础信息中应用名、应用ID、应用描述必须是
不同标签的完整可见值；补图只滚动原应用描述行，不编辑/复制/改样式/扩视口/构造 URL。
祖先裁剪、iframe 外层裁剪、遮挡、加载及场景/路由变化均拒绝；保留原落点 PNG，唯一 Sol
inspect 原图和补图后决定，不由 DOM 或 sampler 自动通过。

原 SAE QUERY 实际点击已独立验收为 fail、且唯一终态 Sol repair 已定位上述字段采集缺陷时，
OPS 可独立审原 `queue-navigation` 的窄例外：`capture_recovery.kind=sae_application_field_capture`、
console_inspection=sae_application_metadata、cloud_calls=0，绑定本资源终态 Sol 原诊断路径/SHA、
original_clicks 中全部精确原 JSON/PNG 的路径/SHA。它不是通用失败重放：只能原 SAE QUERY
缺项，原 packet/成功只读 wire/完整会话/target、同一真实 occurrence 和来源均不变；无未清理
所有物，原 FAIL 和所有 PASS 永久保留。新回合另存图，禁止请求/身份/查询/新会话/云写；只允许
在同槽关闭原预览和按原 occurrence 实点后原生滚动补图。没有独立 Sol 字段诊断、原图漂移、
任何 PASS、未知 wire、其他资源/来源/inspector 或重复申请均拒绝，不能借此复制额度。

纯 QUERY 的结构化资源链接若藏在折叠工具卡，程序从当前成功签名、wire 已核验的精确
resourceType/identifier 引用绑定唯一 toolUseId/turnId，只原生展开该回合和来源卡后点击。
模型自行拼接的 Markdown URL 不是可替代来源；不展开思考作为来源，不构造 URL，多个来源
owner 不猜。原始诊断、旧 gap、额度和 PASS 保留；展开本身不是实际资源点击或通过。

ROS 只读导航因产品解析歧义而尚未实际点击时，可在暂停、所有回合终态后保存
`kind=ops_navigation_only_review` 的精确原证审查，并执行
`queue-navigation <原GLM job> --evidence inputs/...json`。程序重验原 packet、完整自建会话、
原成功签名只读 source、wire 摘要及独立 gap 回执，预留原 target/slot；仍只由 Luna next/execute
派同一持久 GLM 的 navigation 回合。只补缺失点击，新截图另存；禁止新会话、请求、云调用、
清理、覆盖旧图或 PASS 重放。必须先重载修复的原 Web，保持原完整会话活动；原来源和累计次数
不复制、不修改、不追加额度。点击限定原 ROS 成功来源卡，不能借 API 同标识链接。
唯一 Sol 仍须独立 inspect 新图、原 source 和真实落点后验收；OPS 外部样本另负清理责任。

创建场景的 evidence 截图不是导航点击，但存在本维度当前 product-source 时必须绑定该原始来源及完整自建会话，不能继续引用准备期输入。成功来源还要经同账号签名读取和本创建代次响应核对；缺少来源、空引用或不属于本代次仅保留失败诊断，creation_source_verified=false，不能写创建 PASS。旧图、旧输入和既有 PASS 不改写。全部截图仍由唯一 Sol 独立 inspect 并核验清理；该绑定和离线回归不自动通过。

原执行阶段确定的 OSS 签名拒绝可复用于 previous-cleanup 和新增来源前驱核查，但不是消失证明。进入下一维度仍要求成功响应绑定删除、原清理来源、ROS 删除终态及全部 required_cleanup 实际读取。新增来源还要求原 GLM 终态、全部所有物已被 Sol 核验清理和独立逐操作审查；保留原 unknown、原响应及已用次数，不增加原额度。其他错误、未知写和清理期签名失败继续阻断。

结果表必须包含已冻结任务中尚未执行的入口，明确标未测；没有 verdict 不得整行遗漏。新增未测行不会产生 observation、创建检查点或 PASS，不跨 ROS/托管 Terraform/创建 API/查询 API 借用结果。

执行后已清理且释放槽的 blocked 资源，需要 Sol 修输入并查看 OPS 原始诊断图时，使用 `queue-input-repair <task_id> --evidence <相对inputs的JSON> --reason <原因>`，不能伪造正式 observation 或重复派 GLM。维护时 pause、排空模型、备份；提案为 schema_version=1、kind=ops_input_repair，绑定精确 task_id、diagnosis、formal_test=false、pass_added=false、sources（state 相对 inputs 路径/SHA）与 diagnostic_images。每张图逐项声明 path/sha256、diagnostic_evidence/diagnostic_sha256、original_job_id、source_evidence/source_sha256；图和诊断 JSON 均须在本资源 inputs/R编号内，诊断 JSON 须绑定原终态 GLM、原图/SHA、原始来源/SHA，并明确 cloud_writes=0、formal_test=false、pass_added=false。

同一入口也接受已经独立 acceptance_committed(retry=true)、slot 已释放的 repair 任务：OPS
用上述完整诊断证明将测试输入/采集问题重分类为 input，不必先伪造一次 blocked。
任意 repair、在跑 repair、活动模型、未消费动作和未核实创建物仍拒绝；不改旧 FAIL/PASS 或额度。

程序重验本任务实际创建代次已独立清理、所有模型终态和无未消费动作；只追加不可变诊断交接并进入原 Sol repair 队列，不改变 attempt、observations、PASS、所有权、授权或 wire。resume 后仍由唯一 Luna next/execute 派原 Sol。inspect 返回未改原图和 diagnostic_only/eligible_for_verdict=false，每次重验所有 SHA；诊断图不能替代正式图或写验收 verdict。图/来源改变、跨任务原 GLM、未知创建、残留、重复消费均拒绝；需要新云操作仍单独审查。

当前国内站环境截图可保留在原共享 `inputs/ops-domestic-*/` 路径，不为通过资源隔离校验而复制或改名。
在上述本资源诊断 bundle 的 image 中明确 `evidence_role=current_console_environment`，其本资源
diagnostic_evidence 须为 `kind=ops_current_console_environment_diagnostic`、approved=true、精确 task_id、
eligible_for_verdict=false、captured_by=ops_not_original_glm，并绑定原 capture observation.json 的
host/target/PNG及全部 SHA。original_job_id 仅绑定诊断所针对的原终态 GLM，不声称环境图由它生成。
程序只接受原国内 home 控制台、零云调用/零资源点击捕获，并仍禁止 formal review/receipt 使用该图。
该例外仅恢复 Sol 对真实当前登录图的独立查看，不证明原角色权限、资源详情或导航 PASS。

账号级资源不因原始引用缺 regionId 而当作跨地域或无样本。程序只对已审 account 类型核验同一 API 原来源的 requestRegionId，并保留实际签名账号、会话、toolUseId 和创建响应绑定；其他类型仍精确核对 regionId。原始来源不改写，离线修复不产生点击 PASS。导航输入需区分 identifier 与显示名称：name_prefix 不是任意资源属性过滤，列表路由不保证包含 identifier，依据真实 DOM 契约选精确 resolved occurrence。

复合资源标识的原始来源/所有权不可改写为裸名称。已验证的 NAS Terraform 原始 PhysicalResourceId 为“权限组名:文件系统类型”时，程序仅在当前签名会话、同栈创建与 ROS 原始资源响应绑定、类型/属性无冲突后，将点击定位和真实详情等待投影为裸组名；原始 JSON 同时记录 source_identifier 与 navigation_identifier。其他类型不通用拆冒号。真实名称与描述标记、普通/Shift 固定 occurrence、原始截图及 Sol 独立验收仍全部保留；这个投影和离线回归不产生 PASS。

web-attest 必须绑定实际同槽父/子进程：启动加载的门禁、API、资源提取/合并/目录 Python 模块 SHA，以及启动时资源目录/静态入口与关联 JS 的 SHA。旧仅两模块证明不再满足执行门禁；目录或静态文件改变也拒绝，须重启原槽，再 bootstrap 原 target 刷新入口/import cache token。assets 是启动文件清单，不是浏览器截图或已点击证明；仍须正式新会话及截图验收。

加载清单也覆盖 OAuth provider (`iac_code.services.providers.aliyun`) 和结果存储 (`iac_code.tools.result_storage`)。这两项的修复必须由原 Web 子进程启动时输出对应 SHA，并通过 web-attest 对照磁盘；不能用控制 Server 的 SHA 或其他模块代替。加载清单变化时，旧证明默认拒绝，先保持 pause/review_only、备份并排空 GLM，再恢复原 Web/Server，不手工补写证明。

登录修复还必须核验 `iac_code.web.token_transport`、`iac_code.web.resource_signin`；模型请求策略核验覆盖 `iac_code.providers.openai_provider`、`iac_code.providers.dashscope_provider`、`iac_code.providers.thinking`。它们均由同一实际 Web 子进程启动时出具 SHA，缺项或旧 SHA 拒绝。离线回归、正常启动和重载本身不计资源链接通过。

补充只读权限登记修复还须核验 `iac_code.web.console_readonly_policy` 的启动加载 SHA；不能用未改动的角色模块 SHA 代替被它导入的策略登记。登记变化后重启原四槽，并从各自 Settings 只读元数据确认新 revision/needsRepair；这不写 RAM，也不证明云端新权限生效。仍由用户显式 Settings 修复，再独立核对真实有效策略及原生错误是否恢复。

OAuth 登录可以用于测试：正常产品 provider 先刷新为完整、未过期的 STS 三元组，门禁仅绑定这套实际签名凭据，不保存或绑定 OAuth 刷新令牌。首次身份请求必须单独执行，得到真实 AccountId 后再调用其他接口；凭据轮换后需要重新确认身份。缺 token、过期 STS、未知动态凭据来源仍拒绝。不能把管理员身份或登录 UI 当作实际身份验收。

四槽若复制了同一 OAuth 刷新令牌，会发生轮换竞争。修复产品的 canonical 源文件锁和原子保存后，OPS 才能迁移：pause、确认所有回合终态、备份 SQLite、停止四个原 Web，然后运行 `cloud-source-adopt --cli-config <原0600 CLI配置> --account <已批准账号> --region <已批准国内地域>`。命令只做一次有界签名身份核对，从最新 `test-guima` profile 取得完整 OAuth/STS；私有备份各槽旧文件，四个独立 config 的云凭据文件改为同一私有 canonical 文件的符号链接。其他 config/session/token/cwd 不改。部分迁移默认拒绝覆盖，须核对私有备份恢复；重复完整迁移不会再次刷新。迁移不授权云创建、不改 journal、不计 PASS。

迁移后，Sol 的 `cloud_read` 从同一 canonical 源正常刷新，只向 CLI 提供一次性的 0600 `StsToken` profile 和显式 `--profile test-guima --config-path`，调用结束或失败都删除临时文件，不复制 OAuth 刷新令牌。共享源失效时硬拒绝，不能回退原 CLI 副本。测试运行期间不再使用原 CLI OAuth profile 进行外部刷新；需要重新登录/更换账号时先暂停并单独核查，不自动重复迁移或换测试名。重启四个原 Web、核对加载 SHA/相同 target 后仍须独立检查正式身份与登录态，不能凭迁移成功恢复 GLM。

web-stop 对已确认自有启动身份及独立进程组做有界停止（包括 uv 子进程），保留所有状态；五秒未退出才强制停止该自有组。活跃执行租约禁止停止。端口探测使用与 Web 相同的重启语义，TIME_WAIT 不算活跃监听者；其他进程实际监听仍硬拒绝。bootstrap 发现多个同 origin 根页必须 OPS 核对精确旧 target 后清理，不随意选标签或关闭登录页。

web-stop <slot> / web-start <slot> 只检查记录 PID + 启动身份，拒绝误杀已复用 PID。停止不删除 config/cwd/session/token。端口被无关进程占用时不杀它；报告占用并由 OPS 决定。
保留租约的 `web-restart <slot>` 在暂停且全部回合终态后恢复同一进程，并刷新原精确浏览器 target。
重载后只从侧栏选择刷新前封存的完整已有会话键；不点新会话、不发送消息、不调用云。
仅核对旧 DOM 的 idle/session 不够：旧 SSE 连接可能已断，服务已有新失败回答而页面仍展示旧创建结果。
原会话缺失、歧义、锁定、忙碌或键变化须保持维护暂停并调查，不选最近会话兜底，不称新测试 PASS。
修改共享源码前排空 GLM，运行测试后重启受影响槽。不要在有在跑任务时复制/覆盖配置。任何 target 丢失、origin 改变或锁定都会拒绝操作，不能选当前 Chrome 标签兜底。

## 结束与报告

NAS MountTarget 地址列原生仅显示数据库图标时，`nas_mount_target_address` 只允许已实证的
杭州同父文件系统 `/mount` 落点、同一成功签名 DescribeMountTargets 唯一行。
保持 `render_text=[完整MountTargetDomain,VpcId]`，父文件系统、域名、VPC、VswId 与当前
sourcePath/toolUseId 逐项绑定。保留原图后，仅原生滚动地址单元格并 hover 该单元格唯一
database_fill 图标；完整域名 tooltip 和同一行 VPC 都须未截断、未遮挡并实际出现在补图。
不点复制/编辑/挂载/卸载，不改样式或视口，不借 ProtocolMountTarget 或 QUERY 抵扣 TF。
两图交唯一 Sol inspect；诊断和采集修复不是 PASS，新来源仍独立审查，旧次数不重置。

SLS Project 的原生标题省略时，`sls_project_metadata` 仅允许已实证的杭州项目概览标题 hover。
保留原实际点击图；完整名称、原始完整描述及账号、地域必须来自本次成功签名 ListProject
唯一同名行，并绑定该行 sourcePath/toolUseId。补图仅 hover 同一原生标题，真实 tooltip 中
完整名称、描述和杭州标签须全部可见；不切项目、不构造URL、不改样式、不新增云调用。
两张原图由 Sol 独立 inspect，诊断图不记 PASS，旧 FAIL/open PASS/累计调用原样保留。

OOS SecretParameter 的 API 主标识 `p-*` 与原生详情名称不同。框架只在本次签名会话已核验后，
将自己的 `ListSecretParameters/2019-06-01` 唯一原始行 `Id/Name/Type=Secret` 与引用属性、Name alias、
sourcePath、toolUseId、国内地域逐项绑定，再以 Name 等待真实页面；固定/占位选择器均保留
`source_identifier` 的原始 ID 和 `navigation_identifier` 的原生 Name。不能通用 alias 回退、改来源
或任意传 render_identifier。原 `Kms.SecretNotFound` 仍是实际失败，不算 iframe 不支持。
替代样本需自己的非敏感元数据、原生字段诊断和独立追加来源审查；空 Description 不借旧值或 KMS 描述。
不点显示/解密、不读秘密值、不修改已有参数；诊断不计 PASS，原消耗额度及 open PASS 保留。

OTS VpcBinder 的原图裁掉绑定行时，可在维护屏障下修复采样，并保留原图及同资源原生滚动诊断。
`console_inspection="ots_vpc_binding_row"` 只绑定本场景真实 `ListVpcInfoByInstance` 响应：
`instance_name` 为父实例，`render_text=[InstanceVpcName, VpcId]`，地域与引用及 `RegionNo` 一致。
补图只能在原 networkManagement 落点滚动唯一绑定行，不绑定/解绑、不构造 URL、不改样式或视口、不新增云调用。
可见性须覆盖完整文字、祖先裁剪及 iframe 外层裁剪；原图和补图均由 Sol 独立核对。
诊断不计 PASS，旧来源调用计数不清零；需要新来源时仍独立审查追加查询授权。

OOS Template 原图默认折叠基本信息导致描述漏采时，先独立保留原图/来源及原生展开诊断；
`oos_template_metadata` 只允许自己的成功签名 ListTemplates 唯一行、精确 canonical 类型/国内
地域和原私有模板落点。保留原图，只原生展开基本信息补拍标签下名称/描述，不执行或编辑。
OPS 诊断不是正式测试；新正式点击与两张未改 PNG 仍交唯一 Sol 独立验收，不自动记 PASS。

OOS StateConfiguration 的 `oos_state_configuration_list` 保留实际编辑页落点图后，
仅点击同一 frame 原生“配置清单管理”链接，进入同地域 `inventory?tabType=detail` 列表。
只读采集唯一同 ID 行的完整 ID；不点编辑/删除/执行、不提交表单或构造导航 URL。
须先有明确的人类字段口径决定及原生诊断；仅 Description/TemplateName 可由同次成功签名
ListStateConfigurations 唯一行核对，完整 StateConfigurationId 仍须在原生补图可见。
绑定 canonical 类型、账号、地域和当前 source toolUseId；首次只读为 MaxResults10 无游标。
原图和补图均交同一 Sol 独立 inspect，不借准备来源、旧图、DOM 或采样布尔值自动记 PASS。

该 OOS QUERY 两种真实点击已验收为 execution_gap、采集器缺陷由原终态 Sol 独立定位时，
可通过原 queue-navigation 独立审 `capture_recovery.kind=oos_state_configuration_field_capture`、
console_inspection=oos_state_configuration_list、cloud_calls=0。精确绑定终态
sol_oos_input_repair_capture_handoff、完整原 source/session/wire/occurrence 和各原 JSON/PNG SHA。
仅补原缺项的新真实点击及原生完整 ID 补图；不改 packet/旧图/原 verdict、不追加 STS 或查询，
不 new-session、不复制云权限。原 FAIL/PASS、未知调用、其他类型或缺少原签名字段均拒绝。
只有唯一 Sol 独立验收新图才产生 PASS；OPS 诊断图不计正式结果。

只读 ROS navigation-only 回合若在实际点击前终态，OPS 可为原 queue-navigation
提交新的独立审查，附 retry_job_id、retry_terminal_evidence、retry_terminal_sha256。
程序核对最近导航回合的 owning-backend completed 原响应、点击工具均明确失败、
原截图 JSON actual_ui_click=false、相同完整会话、最新 Sol execution_gap/product_gap，
并一次性消费该 retry job；原审查永久归档。所有原来源、wire 次数、PASS 和输出保持不变。
这不是自动重试或新云额度；有成功/未知点击、原证漂移、活动回合仍拒绝。
来源工具卡因重载折叠时，点击程序只展开原签名 ROS toolUseId 唯一卡的原生 summary，
复核原会话后点击卡内已生成链接；不构造 URL、不改 DOM、不调用云。
普通 ROS 托管 Terraform 创建后的点击也绑定自己的 ROS 成员来源卡：必须先通过
当前签名来源及创建归属核验，再匹配同资源类型、精确物理 ID、StackId、LogicalResourceId
的 GetStackResource/ListStackResources 原响应和唯一 toolUseId/turnId。不能展开同名 QUERY、
GetStack 或清理卡抵扣。缺失或歧义不猜卡；展开来源卡不等于点击成功或验收通过。
已清理样本不能用旧引用当存活样本复测，也不能复制已消耗的创建授权。

ROS 托管 NAS MountTarget 的真实 ListStackResources 使用 `Status`；兼容旧 `ResourceStatus`
但全部已出现状态字段必须一致为 CREATE_COMPLETE。只有原签名账号/会话、原成功栈及唯一
filesystem/mount_target 两行、精确栈/地域/逻辑资源和原始复合 PhysicalResourceId 完整绑定时，
才派生自身子资源读取。引用的 identifierField 可为 PhysicalResourceId 或 MountTargetDomain，
但其值仍须与原复合物理 ID 相同；只为导航投影裸域名，不改来源或所有权。缺状态、冲突状态、
跨栈/地域、额外成员、其他 identifierField 均拒绝。修复须排空、备份、离线测试并重载原服务；
旧栈已删除时优先沿原 authority 补剩余只读清理，不再次删除/创建、清零 journal 或产生 PASS。

来源卡 DOM 标识与控制台显示标识不能混用：托管 NAS MountTarget 的原 ROS 卡
`aria-label` 是完整 `FileSystemId:MountTargetDomain`，原生控制台核验使用裸域名。
点击组装仅为该已证明成员的精确 aria-label 占位保留复合标识，href 占位仍用已审域名；
继续绑定自己的 ROS toolUseId/turnId，不借同域名 QUERY 卡。离线回归必须覆盖真实
`Runtime.tool(click)` 的 selector、source_identifier、render_identifier 及普通/Shift 两模式，
不能只测域名投影函数或产品生成链接。修复加载与只读 DOM 匹配不是导航 PASS；
已清理代次不得复活，新增创建仍须明确许可和独立操作审查。

ESS ScheduledTask 缺项直接打开可使用 `ess_scheduled_task_metadata`，前提是 OPS 已在同一真实任务行
证实原生横向滚动能完整显示 ScheduledTaskId、ScheduledTaskName、ScalingGroupId。
框架仅接受当前签名 DescribeScheduledTasks 同一原始行的三个字段与精确国内地域，保持原 render_text。
原落点 PNG 保留；补图只滚动已加载的同 ID 行，不点资源链接或操作按钮、不修改 DOM/视口。
原始和补充 PNG 均交唯一 Sol inspect，仍须实际当前新会话来源、登录/账号/地域和点击核验；
OPS 诊断不记 PASS，不新增 API 授权，不更改原失败、iframe 不支持或累计调用。

RouterInterface 子资源补图仅允许 `router_interface_vbr_child`：精确国内地域、当前签名来源中的
RouterInterfaceId、RouterType=VBR 与自身 RouterId 必须一致，不能使用 OppositeRouterId 代替父 VBR。
名称和对端 VPC 从同一成功 DescribeRouterInterfaces 原始响应的唯一同 ID 行核对，不补造引用属性。
保留原落点 PNG 后，仅点同一父 VBR 页原生“对等连接”，必要时原生折叠导航/横向滚动并恢复导航；
子 ID、自身父 VBR、名称、对端 VPC 必须在同一行完整可见。禁止构造 URL、改样式、隐藏固定列、
扩大截图或新增云调用。登录错误、遮挡、加载或来源不一致均保留失败诊断，不自动判不支持 iframe。
两张图均由 Sol 独立 inspect；此类型不允许仅凭原父页图绕过不完整子资源补图，也不自动产生 PASS。

NAS ProtocolService 完整 ID 被原生列省略时，仅允许 `nas_protocol_service_identity`：
精确 ALIYUN::NAS::ProtocolService、国内地域、自身父 FileSystemId、ProtocolServiceId 和描述，
均绑定本次成功 DescribeProtocolService 唯一原始行及原 toolUseId，不补造引用 Description。
保留原实际点击落点图，只 hover 同 ID 行的原生 ID 提示；完整 ID tooltip 与该行完整描述
都须通过全 Range/祖先裁剪/遮挡检查并出现在前台原视口补图。实际 frame location 必须仍为
同地域同父 `/protocolservice`，包括 OOPIF 缓存 URL 为空的情况；禁止构造 URL、改样式、
扩大视口、编辑或新增云调用。原图与补图均交 Sol inspect；OPS 诊断不是正式 PASS。
新来源仍经原逐操作审查，不能通过该采样路径刷新额度或重放已通过场景。

### R0036 原会话身份续核恢复

仅当原 continuation 的唯一 `cleanup` 动态工具调用在 owning-backend 完整回执中明确以
`Unsupported dynamic tool namespace: null` 失败、原任务仍暂停且所有 worker 终态时，OPS 才可
使用 `inspect-native-identity-recovery --output R0036/<新文件>.json` 生成只读提案。提案会再次
通过同目标浏览器锁核对原 Web session id、`busy=false`、`unlocked=true`、无 preview，并核对
execution-gate 中无未知、进行中或成功的 Delete attempt；原 cleanup intent 保持 `may_have_submitted`
并按未知意图保留，绝不据此清理或重放。

独立 OPS 审查必须原样绑定该提案 SHA、R0036 原 dispatch/continuation job 与 turn、批准过的
`ops-native-identity-*` grant、原账号/authority/session、wire attempts/grants 及实现文件 SHA。
本次固定呈现 `blocked_attempt=3` 与 `original_generation_attempt=2`：只允许当两者仍由同一
冻结 packet、原 scope/session、原 ownership 和同一未用身份 grant 绑定时，独立审批这一次
attempt-2 原代次身份权限向 attempt-3 传输恢复的窄桥接；旧 review 本身不能直接给 attempt 3 执行。
审核副本须保留提案所有字段，仅设 `approved=true` 并增加精确 `proposal_path` 与 `proposal_sha256`。
批准后仅可执行 `execute-native-identity-recovery --evidence <inputs相对路径>`。执行器重新做全部
核验、原 session 快照和同槽 Web 运行时代码指纹；执行闸未加载提案所绑定的精确版本时拒绝执行，
不得用当前磁盘源码代替 Web 已加载代码。执行器用独占创建并 fsync 唯一
`QUERY-identity-native-recovery-0001.json`，然后才可原子登记持久的一次性 identity permit。permit
只在原 scope/session、cleanup phase 下放行批准 grant 的精确 STS `GetCallerIdentity`，预留 wire attempt
时同事务消费；其他云调用（包括任何 Delete）一律拒绝，不回退到普通 cleanup grants。失败或未知时
intent 与 permit 保持锁存，不得重试或重新授权。执行器仅向同一个 Web session 提交固定身份请求。
它不派发 Codex GLM、不创建 Web session、不改变 active phase、不补充额度（只消费既有获批的一次身份额度）、不创建资源、不调用 Delete、
不点击、不补来源、不产生 PASS。OPS 需从原 session 和同一 wire scope 核实身份结果。原清理仍须另行
依据完整原证据处理，不能由身份续核代替。

原生 NAS DataFlow 的 `COMMODITY.INVALID_COMPONENT` 订单已独立证明拒绝、无 CPFS 残留且原网络已清理时，
允许 OPS 审查一次 `native_nas_corrections` / `ops_nas_native_bandwidth_omission_correction`。
先按原 `reconcile-rejected-creation` 登记真实原响应及独立消失证据，再使用标准
`additional_source_requests` / `ops_additional_sources_review` 和 `authorize-creation`；不改初始 admission。
此操作仅接受原完整53角色图的 `bandwidth_omission_bwo1`：省略文件系统预检/创建请求的 Bandwidth，
四个已用的文件系统网络/预检/创建 ClientToken 固定追加 `-bwo1`，健康响应仍要求 Bandwidth720。
完整原提案、冻结范围、原9次调用/累计计数、签名身份、拒绝证据和所有者清理必须逐项核对；
最终完整提案 SHA、官方依据、全依赖/请求/网络费用及反向清理均须独立审查。
这是原用户授权范围内的明确新纠错操作，不是原额度续杯；每个原拒绝代次只准一份已审操作，
换文件名/请求编号/attempt 不能重复授权。模型仍无总时限，云请求限额及费用保护不变。
终态原租约已撤销时，从历史签名 wire 核对已知 OSS 404，不恢复原 active 能力。
已批准新操作的运行上下文允许它自己的新所有者等待清理验收，不能循环要求先清理才能读取清理上下文。
参数省略只是官方依据支持的有界尝试，不证明根因或创建成功，更不产生导航 PASS。

report 把历史基线与新精确 verdict 合并到仓库 HTML。核对 can_finalize、未清理资源、所有回合终态和原始回执。finished 不是“全部通过”；报告分开列通过、未通过、不支持 iframe、无样本/未执行及覆盖盲区。

保留 NAS 图续跑先交截图、后清理：原 GLM continuation 的实际点击 submit 后，原 Sol 独立看图并提交 native_screenshot_review、block(category=cleanup)。pause/排空后使用 inspect-native-cleanup <精确点击job> --screenshot-review <inputs相对JSON> --output <inputs相对新文件>，逐项核对原图/SHA、Sol inspected、两个精确终态及原 wire，再批准该原提案并 queue-native-cleanup。只有唯一 Luna next/execute 派原 GLM cleanup-only，沿用原 attempt/会话/scope/累计额度；不新增创建或重拍 PASS。实际反向清理和全部消失证据再交原 Sol accept，HTML 才汇总通过。该队列不是云调用或验收。
