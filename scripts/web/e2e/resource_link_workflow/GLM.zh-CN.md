# GLM 单资源执行指南

`context.retained_ros_capture` 是已有 ROS 测试资源的补图续跑，不是新建或 NAS DataFlow。
复用原完整会话和冻结 ROS 来源，先实点缺失 ROS open；框架保留落点图，只关闭那条原生公告后补图。
若 native_identity_pending=true，先在实点后 continue-execution(stage=identity) 一次并等待实际同账号结果；
这只消费 OPS 已另审的原 scope 身份续核，不能复制旧身份。否则不做身份查询。
随后 cleanup(ROS) 一次：原响应绑定 DeleteStack 必须先执行，不重复已用 STS；再等待、补原 cleanup
读取并 cleanup-source(ROS)。仅在原栈和自身实例已消失后，request(QUERY) 执行原先从未用过的
身份/实例读取，source(QUERY) 后实点缺失 QUERY open，最后 submit。业务实例绝不登记或清理。
不 new-session、request/source(ROS)、未经独立审查的身份续核、创建、改名或补额度。源码/图在本机
Server，即使 GLM 在 Mini 也不直接打开本机路径；只用当前 workflow-agent.py，交原 Sol 验收。

`context.managed_read_continuation` 是原 MSE 托管 TF 代次的漏执行恢复，不是新测试资源。
若 native_identity_pending=true，先 continue-execution(stage=identity) 一次，等真实同账号成功；
这只消费已单独审查的身份续核，不重复旧 STS。否则不做身份查询。
只用 continue-read(dimension=TF,stage=source)，沿原完整会话依次补 ListStackResources、
三个原 GetStackResource，再用已绑定的实例/VPC/VSwitch 详情；不重复 STS/CreateStack。
新 output 内 source、缺失场景真实 Shift click，随后一次原 cleanup，再补五项必查消失读取并提交。
旧来源/cleanup intent 是不可变历史，不能覆盖；源码、身份、未知调用问题交 OPS，不改名重建。
若 managed_read_continuation 明确 capture_source 已完整，则跳过所有 source/身份读取，直接实点缺项后清理。

若同时有 `context.navigation_recovery.capture_recovery`，优先使用同源补图流程：仅 context、snapshot、
wait、close-preview、指定 click、submit。复用封存原会话；禁止 new-session/request/source 和任何云操作。
框架只读捕获原生完整路径 tooltip、既有禁用 OSS 元数据后取消，不填写或保存；所有新图交原 Sol。

`context.existing_query_recovery` 表示已审现有资源，不是要求你补创建。只用脚本自建一次正式 Web
会话，request QUERY 一次，等待实际签名账号确认和精确查询结束，source 后仅点击缺失场景、submit。
禁止重跑旧创建/预检查/continue-execution、已通过 open 或清理；先由原 Sol 看点击截图。

OOS SecretParameter 只验自己的非敏感元数据。内部 `p-*` 来源 ID 保留，框架从同一签名原始行
绑定原生详情 Name；不要自行换 ID/Name、改 selector、构造 URL 或补借描述。禁止显示/解密、
读取秘密值或修改参数。错误页照实提交，名称映射与离线通过不是导航 PASS。

OTS VpcBinder 若批准 packet 使用 `ots_vpc_binding_row`，框架会在原点击截图之后，原生滚动
自身唯一绑定行补图。不得自行绑定/解绑、构造 URL、改样式、扩大视口或追加查询。
原图与补图均保留并提交 Sol；补图不是自动 PASS，完整绑定名、父实例名及 VpcId 必须实际可见。

模型固定 glm-5.3-prime / xhigh。你只执行当前资源编号，不领取下一资源，不研究源码，不修改 ledger/runner/Server，不自行扫描或规划。

GLM 和 Chrome 在程序选定的同一机器上（local 或 macmini），但四个 Web、来源与验收原图都在本机。用普通终端运行本回合提供的 workflow-agent.py 脚本，不调用 resource_workflow 动态工具（旧线程即使仍显示也不用）；本机桥接负责精确 target、SSH 转发和原图落盘，不自行 SSH/SFTP、构造跨机 URL 或创建另一个浏览器。context.worker_backend 是本回合冻结后端，不能根据当前 worker_host 擅自迁移旧 thread。自然语言请求、四来源维度、点击和清理标准在两种模式完全一致。

## 仅清理恢复（purpose=cleanup）

`context.native_continuation` 的保留 NAS 图分两步：continuation 回合只补原来源、缺失的真实点击和截图，然后 submit；此时禁止 cleanup/stage=cleanup。原 Sol 先看原图，OPS 才可将同一图排入 cleanup。`cleanup_recovery.native=true` 时只做同一 scope 原反向图的 cleanup、必要的 continue-execution(stage=cleanup)、cleanup-source、submit；禁止新来源/创建/点击。仅当 context.native_identity_pending=true 时先单独 continue-execution(stage=identity)，已用身份不重复调用。原额度和已用次数不重置。

这不是新测试或再次创建。context.cleanup_recovery 点名原 GLM job 和唯一维度；复用原来由你 UI 创建、程序封存的完整会话/target/origin。不调用 new-session、request、click、evidence、source，不测试其他维度，不重放任何截图或 PASS。按以下顺序：context → snapshot 核对原会话空闲 → cleanup（点名原维度，程序切原 scope 为 cleanup）→ 等回答结束 → 必要时 continue-read（同维度、仅 stage=cleanup）→ cleanup-source → submit。continue-read 只在原删除实际成功后续查原剩余额度，未删除不能使用它启动删除。

明确门禁拒绝后，没有新增实际云读取的 wait 不会改变旧响应；保存失败并 submit 交 OPS，不能反复等待同一旧 pending。原同凭据身份已绑定但额度为零时，不重新查询身份；实际签名轮换、绑定缺失或额度不足仍必须停止，不能绕过。

调用次数仍是原累计额度，创建额度不恢复。cleanup 的动态系统契约包含原响应投影的精确删除参数，逐字使用，不能省略 RetainAllResources=false；不要无条件重调 STS，相同实际签名凭据的原身份由门禁继续核验。删除只能一次，GetStack 异步等待要留给云完成，不连续几秒耗尽只读额度。删除成功响应不是消失，保存终态栈及精确子资源查询原响应；失败/未知/额度不足如实报告，不再次删除或猜 ID。

若 `context.cleanup_recovery.read_only=true`，原删除已经成功且额度耗尽：仍调用 cleanup 作为原会话只读续查的启动入口，但程序不会再切阶段或交付旧删除文本，只发送原剩余 cleanup 读取。不得再次删除或重复已耗尽身份查询；之后必要时 continue-read(stage=cleanup)，再保存 cleanup-source。它不是新来源或导航复测。PendingDelete/仍存在必须诚实提交，不能用原栈删除成功代替子资源消失。

## 创建阶段与清理阶段

若程序 context.native_nas_query_sample=true，QUERY 含已独立批准的首次 NAS 临时样本依赖图，
不是一般只读查询。逐字执行原 QUERY request，DryRun 成功不当作创建；只从自身签名健康查询
保存 source，点击指定未通过 iframe，再显式 cleanup 和 cleanup-source。自身 DataFlow/父 FS/
Fileset/OSS 关联不一致则保全原证并安全清理，不借既有资源抵扣，不新增名称或创建额度。

若原清理机器契约明确交付 OPS 独立批准的一次身份续核，先单独执行其精确 STS 调用，
等待真实 AccountId 与原账号一致，再调用原响应绑定删除。它是另行审查的一次只读能力，
不恢复原已用尽的身份额度，也不新增创建/删除；没有此实际机器契约时仍按原耗尽规则停止。

普通 dispatch 的 request 处于 execute 阶段。即便产品在创建回答里误尝试提前删除并被拒，不能把它说成清理已执行或账号缺权限；先保存创建原始来源与实际截图，再显式调用 cleanup 才切到清理阶段。cleanup-source 没有真实 cleanup 调用会被程序拒绝。截图必须在删除前采集，删除后“资源不存在”不是导航 PASS。

context 和 cleanup-source 的 `dimension_transition.remaining_dimensions` 是下一维度的程序阶段核查：仅 `previous_cleanup_execution_complete=true` 才继续该批准维度，request 会再次核对实际删除、ROS 根栈 DELETE_COMPLETE、全部必查读取及不可变清理来源；false 时按精确 blocker 补原剩余读取或提交。它不证明子资源消失，不接受清理、不产生 PASS。用户已经明确允许 ResourceGroup PendingDelete 不阻塞进度时，保留真实 PendingDelete/absent=false，清理来源已经合法保存且下一维度机器核查通过，就继续其余批准缺项；不要为等待该子组物理消失反复读取、重开已冻结来源或提前 submit。每个新 owner 仍由 OPS 绑定自身实际原证及人类授权，Sol 独立验收，不能自行把它标删除。没有该用户授权或其他未知残留仍报告 OPS。

`context.resource_group_cleanup_user_decisions` 提供程序逐件重验后的本任务原始用户决定、原证/SHA 与原 owner。根据真实原话理解本任务进度例外，不沿用过时“必须等物理消失”结论。该列表不接受任何新 owner；每个新 owner 仍如实保存自身原始响应，交 OPS 独立绑定用户决定后由 Sol 验收。

程序在已审批请求外明确标注本次仅创建/读取/生成引用后停止，旧请求里的“采样后清理”不代表产品能观察你的点击。先 source，再本维度未通过的普通/Shift click 及必要 creation evidence，最后单独 cleanup。cleanup 意图写入后 click 被程序拒绝，不能补拍已删除样本；故障 evidence、原始来源和安全 cleanup 仍可用，不因截图失败留下云资源，不改变模型时限或云调用额度。

## 唯一工具入口

context.resource.retest_scope.kind=failed_navigation_existing 时仅执行 expected_scenes 的冻结导航。
ROS 使用批准的已有栈只读请求，不是创建：request → 等回答/引用稳定 → source → 指定 click → submit。
不要 cleanup、创建或登记已有对象；其余未测维度和旧 PASS 不执行。ROS 来源不能换成查询 API 来源，
程序仍逐实际签名响应核验，截图由 Sol 独立验收。本模式不产生 creation_verified。

kind=failed_navigation_managed 同样只执行冻结的失败导航，允许同一资源的 QUERY、ROS、TF
多个来源由你这个持久资源会话分别执行。TF 必须是批准的 ROS 服务托管 Terraform 栈：
同维度真实 GetStack 返回 StackType=Terraform，再读取该栈资源并生成引用；不能使用
本机 Terraform、底层查询或 ROS 原生栈代替。来源不可互借，每个 contract/mode 独立点击，
已通过的另一打开方式不重放。H 开头的历史入口保持原 contract，不换成同类型 QUERY 入口。
本模式只读，不创建、不 cleanup，不增加 creation_verified；缺少存活样本则保留原证据提交。

packet 有 execution_selection 时仅执行 context.expected_scenes 和 packet.requests 点名的本阶段维度。all_expected_scenes 中的其他项是仍待解决的全局缺项，不是本回合任务，不尝试其 request/source/cleanup/click/evidence，不复制旧结果或生成新 verdict。仍是同一资源原 GLM，每阶段新 Web 会话；创建/删除/读取累计额度沿用原 authority，不因阶段或 attempt 增加。

context.execution_admission 是当前程序审批/lease/原输入 SHA/创建授权的实际状态；context.dispatch_state 是当前恢复闸。已派发 job 的 approved operational packet 不包含旧提案 hold/未审批叙述；原提案和完整 SHA 保留给 Sol 独立审查。以当前 admission 为准，不能把历史“尚未审批/全局hold”描述当作新阻塞，也不能把 admission 当作云身份、资源创建或截图 PASS。先真实签名确认身份，再按固定请求执行。

## 脚本调用

使用回合起始指令给出的完整 Python/脚本路径；下例省略为相对路径，工作目录必须是当前 job。

```sh
python3 workflow-agent.py context
python3 workflow-agent.py request --arguments '{"dimension":"QUERY"}'
python3 workflow-agent.py --json '{"operation":"snapshot","arguments":{}}'
```

返回 image_path 时，用 Codex 原生 view_image 读取该路径。客户端核验并保存原始图片字节 SHA，
不能仅看路径、JSON 或模型总结就称截图通过。diagnostic_only 图片仍不能作正式验收。
只运行该客户端和原生看图；不直接运行云 CLI、浏览器脚本、网络/SSH 或改产品/状态文件。
脚本失败若 outcome 为 unknown_or_not_started 或提示 reconcile，保留原 job 交 OPS，不重复写操作。
令牌由私有文件自动读取，不读出、不复制到命令或日志。脚本没有资源总时限。

脚本 operation / arguments：

| 操作 | 参数 / 作用 |
| --- | --- |
| context | 读取编号、缺失场景、官方输入、批准 packet 和专属 slot |
| snapshot | 专属 target 的 UI / 完整活动 session key；不是显示 UUID |
| wait | seconds 默认30，单次1–30秒，可重复；不占浏览器锁、不调用云、不设整个测试时限 |
| new-session | 在真实 Web UI 点击新会话；必须由你自己做 |
| request | dimension: ROS / TF / CREATE / QUERY；只发送批准的固定自然语言 |
| continue-read | dimension、stage: source / cleanup；原写操作已成功且额度全部耗尽后，仅继续同 scope 的剩余只读核查，不安装新能力、不创建或删除 |
| source | dimension；程序绑定本回合完整会话键，保存 messages/references，不构造引用 |
| cleanup-source | dimension；程序绑定同一键，另存清理原始输出，不覆盖创建来源 |
| click | scene；程序绑定同一键，只点精确可见引用，按场景普通/Shift |
| close-preview | 无参数；关闭本回合当前预览，再执行同资源 Shift；不关闭其他槽 |
| evidence | scene；创建状态或失败截图，不把它称为实际点击 |
| cleanup | dimension；只发送批准的清理文本，不能删除别人的资源 |
| submit | report；短报告，标注执行遗漏/环境问题，不自报验收计数 |

当前 Web 是 http://127.0.0.1:16100 起的租约 origin；CDP9225。不能用 Desktop、IAB、别的标签、国际站、page.goto 或构造资源 URL。浏览器关闭/target 丢失直接报告，不自找当前标签。

先必要读取再立即 Web。GLM 没有固定回合或资源总时限；正常的云异步等待可以继续，调度者不能因运行较久中断或重派。明确故障时保存证据并报告，不重复创建或点击已有产物。普通/Shift 必须同账号/地域/真实资源/同源会话；本次 attempt 只建一个 Web 会话，每个维度要有自己的原始 source。create 场景用 evidence，导航场景用 click。

回答还在进行、引用不可见、会话键不完整时不能运行点击。新建会话后从 .session-item.is-active[data-session-id] 读取完整键；ws~ 编码必须原样传递。

Web 回答结束不等于云异步创建或删除完成。回答仍是 CREATE_IN_PROGRESS / DELETE_IN_PROGRESS 时，用 wait(seconds=30) 留给云完成，再调用 continue-read（原维度、stage=source 或 cleanup），只续查原栈和子资源。回答 busy 时也用 wait 后再 snapshot，不忙轮询。不能立即 source 冻结半成品、click 未生成资源，或提前清理尚未完成的创建。等待可重复、不设资源截止时间；读取仍遵守原次数。真实失败则保存原证据，安全清理后报告；不再创建。source/cleanup-source 在栈尚进行中会明确提示此入口，不能把这个提示当作点击失败。

新版 packet 的 read_policy 由程序执行：创建/清理读取分开，过快 GetStack 在真正调用云前等待，不增加整个回合时限。cleanup-source 报 `required post-delete read ... <grant id>` 时，这是明确执行缺项，不是资源不支持或点击失败；按提示继续当前维度 cleanup 的剩余只读项。不要重复 cleanup、删除、创建或换名称。必查项全部实际执行后仍须保存原响应供 Sol 判断消失，不自报 PASS。

source 和 cleanup-source 也必须等 snapshot.busy=false 后才能保存；原始来源只保存一次且不可覆盖。new-session 将你自己通过 UI 新建的完整活动键封存到当前 job/target/origin；source、cleanup-source、click、evidence、close-preview **省略 session_id 参数**，程序传递该真实完整键并在浏览器重验，不需要手抄长编码。显式传键仍须逐字一致，错误值硬拒绝，绝不自动修正或回退到其他会话。重复展示同一真实资源不算多个样本，程序仍独立核实实际点击的 href 和固定 DOM occurrence。

click 在真实落点有界等待资源标识、已审云详情字段和加载动画消失再保存原图；导航栏ID不算详情，render_check 并非PASS。若超时仍保存实际像素，勿重放/覆盖，按实情submit后交Sol定位。所有创建物仍须安全清理并保存原始确认。

明确批准 NAS metadata 采样的 scene，click 自动在保留原始普通/Shift 落点图后，通过同一真实控制台的固定只读 UI 查看精确权限组元数据并取消弹窗，另存 -metadata.png。不要自行点击保存/删除、修改字段或调用替代接口；补图与原图交 Sol 验收，不声明自动通过，不重复 click。

产品要求额外权限确认时，不选“永久允许”、不绕过权限面板；保存 snapshot 并报告精确请求交 OPS 处理。普通点击可能打开登录辅助窗口，该窗口不算 iframe 的落点证据。

明确批准 ECS KeyPair metadata scene 时，click 自动保留落点原图，再按自身来源名称用原生列表搜索，核对唯一同名行完整指纹，必要时原生折叠/滚动并补拍后恢复。不要自行选择其他密钥对、绑定/解绑/删除、隐藏浮层或改样式。完整指纹被遮挡或来源不匹配仍报真实缺口；补图不自动通过，不重复 click，不补创建额度。

明确批准 SLB ACL metadata scene 时，click 自动保留真实详情落点原图，再点固定访问控制侧栏、核对当前列表唯一同 ID 行名称/IP版本，原生横向滚动并另存补图。它只读，不点击编辑、创建、删除，不隐藏浮层或改写原图；字段不全、登录失效或列表未覆盖即诚实报缺口。补图不等于自动通过，也不允许重复 click、扩大读取或调用次数。

程序执行门禁与账号权限不同：即使 test-guima 是管理员，也只能执行当前 session、当前维度预审的精确 API。每个维度先按批准文本确认当前云账号；创建/查询/清理按此维度走完再开始下一个。清理只允许原始创建响应派生的 ID；不要在创建请求内提前清理，也不要尝试 Shell/MCP/ros_deploy 等替代入口。test_execution_gate_denied 属于需要 Sol/OPS 核对输入的执行拒绝，不等于资源不支持或产品点击 FAIL；保留原始证据，不再次创建。

cleanup-source 同时返回 `original_cleanup_responses`：本维度、原完整会话已经实际执行的删除和清理读取原响应，保留时间、toolUseId、状态、次数与签名身份绑定。产品最后一句总结或重复删除拒绝不覆盖原成功删除；核对这些已有记录，不再次删除。读取仍返回 PendingDelete/存在、未知响应或未查明身份时如实保留。此导出本身不证明消失、不记 PASS；独立 Sol 仍验收原响应和截图。

完成后提交报告即可。submit 不等于验收，不允许声称已记账。就算单项失败也继续其他已批准且安全的缺失项；任何未知创建/删除结果要报告并保留原始输出，不再次创建。

明确批准 OOS Template metadata 时，click 保留原落点图后仅原生展开同一详情“基本信息”，
另存来源绑定的名称/描述补图。不执行、更新或同步模板，不重复 click；原图和补图交 Sol 验收，
只读展开、机器字段匹配和补图自身不算 PASS。

明确批准 RouterInterface 子行采样时，click 保留原父 VBR 落点图，然后仅点同源原生“对等连接”，
按本次签名来源核对同一行的子 ID、自身父 VBR、名称和对端 VPC，必要时原生折叠导航/横向滚动补图。
不要自行构造 URL、改样式、隐藏固定列或重复 click；登录失效、字段遮挡或来源不一致如实提交。
两图交 Sol 独立验收，补图和机器标记不等于通过，不增加云操作或重放历史 PASS。

明确批准 ResourceGroup metadata scene 时，click 自动保留 Overview 原图，再仅点同一详情
原生“设置”标签另存 metadata 图。不要自行点同名侧栏、编辑、保存、删除或改变 URL；
字段缺失、未加载、遮挡或失去登录均报真实缺口。补图不是自动 PASS，不重放旧通过项。
# 已审原会话导航恢复

若 navigation_recovery.capture_recovery.kind=oos_state_configuration_field_capture，只恢复原
OOS QUERY 的两个采集缺项。沿用原完整会话、签名 source 和固定 occurrence，关闭同槽预览后
普通/Shift 各真实点击一次。程序保留新落点图并点击原生“配置清单管理”补拍完整同 ID 行；
不 new-session/request/source/continue-read/cleanup，不查询或充值身份，不提交编辑表单。
两模式的原图和补图全部交 Sol，旧错误图保留，不能自行声明 PASS。

若 navigation_recovery.capture_recovery.kind=sae_application_field_capture，只补原 SAE QUERY
截图采集缺项。先 snapshot 核对原完整会话；有预览时用 close-preview 关闭同槽预览，再按
expected_scenes 普通/Shift 各实点一次，两次之间也关闭同槽预览。程序固定原 occurrence，
自动按独立应用名/ID/描述字段原生滚动另存 metadata PNG，不自行滚动脚本或改页面。
原 packet 不改、新 output 不覆盖原图；不 new-session/request/source/continue-read/cleanup，
不重新 STS/查询，不把历史 TF PASS 或 OPS 诊断图记入本次通过。补图失败保留原证交 Sol。

purpose=navigation 时，context.navigation_recovery 是程序审定的原自建会话和成功签名来源。
只允许 context/snapshot/wait/click/evidence/submit：不新建会话、不发送请求、不导出来源、
不调用云、不清理。仅补 expected_scenes 缺项，程序选择原 ROS 来源卡并另存本回合原图；
不复制原 intent/截图、不借 API 链接、不猜 URL，不声称 PASS。真实验收仍由唯一 Sol 完成。

OTS 临时空表例外仅由可信 context.temporary_sample_admission 与 readonly_retest_dimensions 提供；原人类费用许可本身不准执行。只执行批准的原生 ROS/ROS 托管 Terraform 精确模板和原响应绑定清理，不写数据、不建索引、不创建实例、不改网络/IAM，不改名或新 attempt 复制额度，不重放旧 PASS。

批准 NAS nas_protocol_mount_target_child 场景时 click 自动保留原父服务落点，并只点精确父行原生“导出目录”、hover 原生完整 ID/路径/挂载地址 tooltip，必要时原生水平滚动。export_id、path、mount 三张未改补图都交 Sol；不要自己构造 URL、调整样式/viewport、写云或反复 click 代替采集修复。补图不是自动 PASS。

批准 `sls_project_metadata` 时，click 保留原实际落点图，再仅 hover 自身项目概览原生标题，
保存完整名称、描述及杭州标签的 tooltip 补图。不切项目、不编辑、不构造URL或重复查询；
原图和补图交 Sol 独立验收，补图本身不算 PASS。

批准 `nas_mount_target_address` 时，click 保留挂载点原落点图，仅原生滚动自身来源绑定行
的地址单元格并 hover 数据库图标，补拍完整域名 tooltip 及同一行 VPC。不点复制、挂载、
卸载或编辑，不改样式/视口，不重复 click 或查询；两张原图交 Sol，补图不自动通过。

批准 NAS nas_fileset_description 场景时，click 保留原列表落点图，再仅 hover 本次签名来源唯一 FsetId 行的完整描述，保存原生 tooltip 补图。完整描述、目录和自身 ID 必须真实可见；不要编辑、删除、改样式或重复 click，原图和补图均交 Sol 独立验收。

批准 `nas_protocol_service_identity` 时，click 保留协议服务原落点图，仅 hover 本次签名
来源唯一 ProtocolServiceId 行的 ID，保存原生完整 ID tooltip 与原行描述补图。禁止改 URL、
样式/视口、编辑、删除或重复 click；补图失败如实保留，不以 DOM/标题代替像素、不声明 PASS。
