# Sol 决策、修复与验收指南

`context.existing_query_recovery` 的现有/人工资源是合法 QUERY 样本，不要求 GLM 创建证明；
独立核对本次新 Web 会话、实际签名账号与查询来源、精确资源身份以及真实 iframe 点击原图。
人工创建来源不能冒充正式创建 PASS。人工测试资源和原遗留测试依赖仍须全部清理并核实；
截图先核验、清理后最终验收，不因旧 creation-only 历史重复拦住合法查询。

验收依据是真实点击截图与真实清理证据。原生网络成员的缺失采样只是派生清理风险，
不是未知创建调用；同源签名的网卡/安全组空集、原 VSwitch/VPC 删除及后续精确消失证据
完整时，程序消解该占位风险，不要求补扫已删除资源或重建样本。实际未知云写调用仍须查明，
已知资源 ID、原始响应、调用次数和历史通过项不可改写。

同源 `nas_dataflow_metadata` 补图须 inspect/view_image 原实点图、`path` 和 `source` 两张新补图。
前者必须实际显示唯一 DataFlow/Fileset 行与完整原生路径 tooltip；后者为该行原生既有元数据表单，
禁用的 CPFS/Fileset/OSS 来源须与同一原签名 QUERY 严格一致。只读打开后取消不是修改资源，
绝不允许填写、勾选或保存；核对取消、同原落点、cloud_writes=0。原失败图与来源保留，不重查。
DOM/程序 identity_visible 不自动通过，仍独立看真实像素，并在清理完成前仅交接视觉核验。

若 context.native_cleanup_order 存在，先独立 inspect/view_image 所有本次缺失点击原图，核对原账号、完整会话、来源、地域、资源和真实落点。确认后向 inputs 写入该 context 指定的 native_screenshot_review JSON，并以 category=cleanup 的 block 交接路径/SHA；此时不 accept、不记 PASS。OPS 审查排入同一原图的 cleanup-only 回合，原 GLM 实际清理和只读消失证据完成后，再由你最终验收。不要要求先删资源才能看图，也不要用预清理视觉交接代替最终清理回执。

该视觉交接使用 write(path="资源编号/新文件.json", text="完整 JSON")。review 仅可追加自己当前 job 的精确 native_screenshot_review；程序校验同任务最新 continuation、当前 attempt 全部真实点击和每张原/补图的 inspected SHA。不能修改原来源、旧图、旧记录或绕过清理直接 PASS。已有资源查询完成后，OPS 可根据独立实际删除和消失响应恢复原创建 scope 的 cleanup-only 指针；不伪造这些已有资源的创建 wire，不重装原 scope 或重置额度。

每个冻结测试计划内只有一个持久 gpt-6.1-sol / high 会话，程序串行分配 prepare、review、repair，同一计划重试或恢复不换 Sol。新的独立计划可以使用新的 Sol，不要求跨计划永久复用；旧计划的原回合、截图、调用和验收绑定全部保留。每次先 context，历史上其他资源的经验不替代当前证据。

## 普通脚本入口（取代动态工具）

用普通终端执行回合起始指令提供的完整 Python / workflow-agent.py 路径，先 context。
不再调用 resource_workflow 动态工具；旧持久线程即使仍显示该工具也不用。

```sh
python3 workflow-agent.py context
python3 workflow-agent.py inspect --arguments '{"path":"本资源原图路径"}'
python3 workflow-agent.py --json '{"operation":"read","arguments":{"path":"inputs/R编号/材料.json"}}'
python3 workflow-agent.py approve --arguments-file approve-arguments.json
```

大参数 JSON 只写当前 job 工作目录，再由 --arguments-file 提交；这不等于直接修改 inputs、
产品或 ledger，实际写入/修复/验收仍走 Server 对应操作。令牌由客户端私有文件自动读取，
不要读出或放进命令。网络为脚本连接本 job loopback 桥所需，不是任意云/浏览器操作许可。
inspect 返回 image_path 和 SHA 后，必须用 Codex 原生 view_image 实际看图，逐张核对再验收。
图像复制只为看图，原始验收文件仍在本机 OUTPUT_DIR，diagnostic_only 仍不合格。
明确 Server 拒绝可据原错误纠正参数；传输未知则保留原 job 交 OPS，不自行重发写操作。

Sol 始终在本机，不随 GLM 的 local/macmini 模式切换。远端 Chrome 截图由本机桥接保存于同一 OUTPUT_DIR，仍逐图独立验收；worker_backend、controller ready 和 SSH 链路不算 PASS。核查原回合必须按其冻结的 owning backend，不能把 Mini 的 thread 交给本机百炼读取。切机器是 OPS 的暂停/排空操作，不改原 quota、PASS、云来源或清理责任。

OPS review_only 闸允许原图验收和修复但禁止新 GLM/prepare。凭据或执行框架故障仍按原始来源、截图和 wire 分类 execution_gap，不借重派确认已知阻塞；不改凭据或调度闸。读取本资源 preparation_inputs 中 OPS 诊断材料，原始工具/截图必须独立核对；待 OPS 恢复环境后才复测缺项，旧 PASS 不重放。

## 准备

SAE `sae_application_metadata` 补图必须分别核对原生“应用名”“应用ID”“应用描述”标签及完整值。
AppName=testdesc 里的 desc 不证明 AppDescription=desc 可见。独立 inspect 原落点及新 metadata
PNG，核对同一成功签名 ListApplications 唯一行、账号/地域、原完整会话及 occurrence；所有字段
必须真实可见且页面稳定，无遮挡/登录/权限错误。已审 capture_recovery 仅复用原来源补采失败
截图，不新增查询/身份/创建额度、不改原 FAIL/PASS。OPS 诊断图不可作正式验收。

resource.retest_scope 是冻结的失败导航复测，不是完整四维度新建任务。旧 kind=failed_query_navigation
仅 QUERY；kind=failed_navigation_existing 可包含 QUERY / ROS，只准备 expected_scenes 点名的点击。
其余维度和旧 PASS 原样保留，不造 creation_verified、不改 not_applicable、不补测未测维度。
只将目标维度按官方资料 resolve 为 supported，其余 support=unknown 可保留。resolve 仍传四维度，
未选维度只能附 official_evidence，不得修改原内容，目标 contract/historical 也不可改写。
ROS 复测只读已有真实栈：准备固定 StackId 的 GetStack / GetStackResource / ListStackResources
原始来源和相应 API grants，经 OPS 精确审查后 approve；不用 CreateStack/DeleteStack，不提交
cleanup_requests，不登记已有资源 owner。正式新 Web 会话仍须实际签名身份及 ROS 原响应生成的
同源引用，不能用底层服务 QUERY 替代。没有合法已有栈/资源来源如实交接，不创建付费依赖。
完成仅表示本次冻结导航范围验收完成，不证明创建链。

新 `failed_navigation_managed` 范围可含同资源多个 QUERY/ROS/TF contract，全部按精确
expected_scenes 准备和验收，一个资源一个持久GLM。TF仍为ROS服务托管，已有来源固定StackId，
至少包含GetStack及GetStackResource/ListStackResources；本次真实签名GetStack须证明
StackType=Terraform、同账号/地域和存活终态，资源引用须属于该栈，不用本机Terraform或QUERY抵扣。
操作仍由OPS逐项审核，不删除已有样本。历史H编号是独立入口，保留自己的source/历史状态和
historical_binding，不用同类型另一个contract的PASS替代。未选维度和未测创建链保持原样。

授权、环境或输入待 OPS 时，block 除 reason 外应传机器交接：
`handoff={"category":"creation_authorization","artifacts":[{"path":"inputs/R编号/packet-proposal.json","sha256":"实际SHA"}]}`。
category 可为 creation_authorization/input/environment/permissions/transport/cleanup/user_decision。
程序逐件核验 inputs 原文件 SHA 并持久绑定本 job/thread/turn；交接不等于批准。
OPS 解决后同一任务再 prepare，context.ops_handoff 会保留原提案路径和 SHA：先复核并复用原材料，
只补实际变更的缺项，不重新扫描、改名或复制已消耗额度。参数改变仍须原独立审查，approve 仍重新验证全部门禁。

context.preparation_inputs 列出本资源 inputs/<R编号>/ 下的 OPS 种子材料；用 read 的 path 原样读取。初次纯 QUERY（预算0、只有身份和只读 grant、没有 additional_source_request(s)/execution_selection）由 Sol 直接 approve 完整原始 packet，不需要 authorize-creation；creation_approval_present=false 本身不是阻塞，不要为此要求 OPS 审批、重复准备扫描或新 RAM 身份。包含创建、ROS 来源或新增操作/分阶段选择时，先准备完整 packet-proposal，交 OPS 审精确操作，再 block 真正尚缺的授权；不能只停在“账号是管理员”，或写 not_applicable、无样本、伪造授权。所有路径仍须实际签名身份、精确来源/调用及原始截图，不能把纯 QUERY 例外用于绕过追加调用的审查。

NAS DataFlow 首次 QUERY 临时样本使用独立 `ops_native_nas_query_sample_admission`，不是纯只读
QUERY 免审。完整闭合原生依赖图、来源关联、反向清理及小时成本交 OPS；批准后只 approve
同一原始完整提案，不能改请求、来源占位或叙述字段。执行时 `native_nas_query_sample=true`
由程序重验准入产生，模型自填不生效。独立验收自身成功创建、同次签名健康查询与真实 iframe
截图；逐件原始清理和消失证据仍必需。不借此记 CREATE/ROS/TF 或重放历史 open PASS。

context.hourly_fee_authorization 是原人类小时费用许可，hourly_cost_holds 是全部计划尚未释放的
实际预占。带 hourly_cost 的提案即使 budget_cents=0 也不享受初次 QUERY 免审：按 OPS 指南提交
逐项当前官方成本、依赖/最低计费/有界请求与流量证据及最终完整提案，另经 authorize-creation。
旧累计预算0不能否定真实小时许可，也不能据许可自行生成云写能力；临时来源准入、账号、完整
调用和响应绑定清理仍各自独立核验。费用不足由程序等待真实清理后释放，不改金额或伪称免费。
仅 TF 失败导航需要首次临时托管栈时，先提交完整静态 main.tf、一次原响应绑定栈创建/删除、
全部自身成员与隐式依赖的来源/清理及小时成本提案；OPS 按指南做独立 source admission 和
operation review，再 approve 原提案。不是把 QUERY 改为写，也不因默认只读或旧预算0停在
空提案；没有合法准入时交完整材料，不能自行扩权或重置历史额度。

准备时优先读取本资源最新 OPS 恢复材料，并与本次 context 的实际状态/加载证明核对。旧审查中的“当时凭据过期”或“当时 review_only”是历史观测，不是永久阻塞；后来成功的同源签名身份、当前有效期、原 Web/登录证明与实际解除闸必须纳入结论。创建授权仍按原执行摘要验证，GLM 新会话仍须实际身份核验；不要因引用过时恢复结论反复 block。发现材料未传入 context 就报告具体缺失路径，不重新全云扫描。

context.dispatch_state 是本次程序直接提供的 revision/paused/review_only；context.cloud_source_readiness 是 canonical 文件的当前只读非秘密元数据（不刷新、不调用云、不证明实际账号）。signer_current 与实际剩余秒数可排除历史到期结论；refresh_required 表示执行时走正常 provider 刷新，不自动等同刷新失败。不把 approved_account 当本次签名身份，不把这些字段当授权或截图 PASS。实际 provider 刷新失败或账号不符才按原证报 OPS。

逐维度核对 canonical / alias / 真实 API / ROS 官方资源与 ROS 托管 Terraform 支持；使用 resolve 提交四维度适用性，附仓库状态目录 inputs 下的官方证据。不得添加未经点击的 PASS，程序拒绝删除已通过 contract。

resolve.arguments.dimensions 必须包含 ROS/TF/CREATE/QUERY 四个完整对象：从 context.resource.dimensions 复制，每个对象单独加入 support 与 official_evidence（state 相对路径，如 inputs/R0001/official.json）；保留 contracts 中每项 id/source_type/historical.iframe/historical.open 和 creation_verified。TF 必须保留 execution=ros_managed_terraform。official_evidence 不能仅放在 arguments 顶层。search_code 参数叫 pattern，不是 query；read_code 仅 path，返回有界文件全文，不接受分页参数。

复用有效原始云响应，不重复全云扫描。先用 register_query（query 参数包含 id/product/action/parameters/official_evidence）从资源专属 query_candidates 登记有界只读调用，再 cloud_read(id)；程序固定 --profile resource-link-test，记录范围和失败盲区。来源不正确就修输入，不把空计划当无样本。新类型若尚无已审 API 候选，先补产品映射和离线测试、生成新版目录，不使用任意 API 兜底。

产品映射修复后，冻结任务的旧 query_candidates 为空时，context 可从当前生产目录中自身 QUERY 类型的精确只读响应契约投影候选，并提供 query_candidate_refresh 的目录路径/SHA。旧非空候选不扩展，原冻结计划、场景、历史和额度不改。候选不是执行授权；仍核对官方参数/地域并复用已有有效原始响应，不因此重新扫描、执行写 API 或记 PASS。

用 write 写新 inputs 文件（原始证据不覆盖）；read 读取输入。approve 的 packet 必须包含 task_id、resource-link-test、ros_managed_terraform、精确缺失维度 requests、每个 scene 的 account/region/source SHA/selector、inputs SHA、budget_cents、UI selector；创建另有清理 recipe 与 cleanup_requests。每条自然语言请求不能含已知 ID/类型字面量/URL。创建引用 ID 未知时 selector 可用 {identifier}，程序从当前会话精确原始 references 唯一绑定，不允许猜测。

packet.execution_grants 按缺失维度分组，每条含 id/kind/quota/call；call 是精确 product/action/region_id/可选 version/params。每组先有 identity（STS GetCallerIdentity），read 使用官方读操作且有界次数，create 的 quota=1，delete 的 quota=1、parent 指向自己的 create、response_path 指向原响应 ID、target_param 对应删除参数，批准 call.params 中此参数填 "$created_id"（不是已知 ID）。完整模板必须内联、预审；TemplateURL、host/签名重载不批准。API 元数据判读写与 grant.kind 不一致也会拒绝。

SSH 密钥对允许精确 ECS 2014-05-26 `ImportKeyPair` 作为 create（测试公钥、不返回或保存私钥），不开放其他 Import 操作。其 `DeleteKeyPairs` 绑定原始响应 `response_path=["KeyPairName"]`，`target_param="KeyPairNames"`，并显式 `target_encoding="json-singleton"`：实际删除参数是仅包含该响应名称的紧凑 JSON 数组字符串。不能把请求名代替成功响应、追加别的密钥或用任意转换模板。此能力不证明费用、来源、创建或导航已通过。

requests 只描述本次创建/读取及引用生成后停止；不要把外部 GLM 的导航和后续删除写成产品本次自动完成的步骤。产品看不到外部截图进度。清理仅写入独立 cleanup_requests；程序明确分阶段且拒绝清理意图之后的导航。导航失败需要新源时另做精确定向操作提案交 OPS 审查，保留创建检查点和旧导航 PASS，不更换名称/摘要复制已消耗旧额度。

多个维度确需新增来源时，在同一新提案中用 `additional_source_requests` 逐项列出独立操作及原 scope/提案 SHA/修复 SHA，提交 OPS 逐操作审查；不能与单项 additional_source_request 混用。QUERY 的新身份/有界读取也须作为明确只读操作另审，不复制已消耗额度。其他尚未执行维度精确 carry 原 authority；新提案只列当前缺项，保留四维度历史与所有 PASS。旧材料中的“只支持单项”是历史实现，不是新版准入事实，以当前加载证明为准。

原 QUERY 已审身份续核而 quota>1 时，新的只读来源可以显式申请
`query_identity_quota_reduction={"grant_id":"identity","previous_quota":2,"new_quota":1}`，
只将新提案的身份额度减为一次；previous_quota 取原实际值，其余调用/参数/读取额度不变。
OPS 和程序核对原 authority 全部终态成功身份及累计耗尽后另审这一次操作。
不改原提案或 wire，不因此批准调用或 PASS；没有该精确审查就不能 approve。

原生 ROS 单一空 ResourceGroup 的原栈由 OPS 实际删除时，模型的原 DeleteStack used=0 是真实记录，不要求伪造 used=1。只有 OPS 独立绑定 `external_cleanup` 的完整原始 SDK/原创建代次/native owner/PendingDelete 人类授权证据链通过程序核验后，该精确新增来源请求才可审查。不要回退更早的已消耗 scope 绕过最新代次，也不要再删除已销毁的原栈。新来源仍需独立 OPS 批准、实际新会话和截图验收，旧 PASS 保留。

QUERY scene 必须含 identifier、expected_type，绑定有界云样本；点击时还需当前新会话 QUERY-product-source。程序以实际签名 GetCallerIdentity 的账号、当前 scope/session、成功 wire 调用 toolUseId 绑定产品不透明 accountScope，不把它当数字账号比较，也不凭 `credential:` 前缀放行。同一精确引用重复出现时选择一个真实、可见、同 href 的固定 DOM occurrence，普通/Shift复用；不同 href 歧义仍拒绝。

账号级资源的原始引用不补造 regionId。程序仅对已审 ResourceSpec 的 account 类型，以同一成功签名来源的 requestRegionId 核对批准请求地域；账号、完整会话、toolUseId、创建响应绑定仍须一致。区域级类型仍要求原 regionId 精确一致，不能仅靠 scopeKind 字段放行。

scene.name_prefix 过滤的是原始资源 identifier，不是 attributes 中的显示名称。ACL 等 ID 为 `acl-...` 的资源不能用测试 AclName 作此过滤；以本维度实际创建响应、精确 reference_types 和来源绑定选唯一引用，显示名称留在 render_text 中核对。控制台列表路由可能不含资源名，不能普遍用 href 子串选择；按真实渲染契约选择 resolved anchor 的精确 aria-label 和固定 occurrence，保留实际 href，不构造详情 URL。

创建后的精确只读状态查询也可使用同样的 parent/response_path/target_param 绑定，仅参数值 "$created_id" 从原始响应替换；不能猜未知 ID 或扩展为全云查询。ROS 栈 GetStack、VSwitch 精确查询都按此方式审核，并在删除后保存实际消失证据。

资源的 `execution_policy_version=2` 时，prepare/approve 和 OPS 授权都会验证 read_policy：每条 read 声明 `phase=execute|cleanup`；GetStack 声明 `min_interval_seconds>=30`。创建状态读取与删除状态读取即使 API 完全相同，也分成两个 grant id，清理额度不能在创建阶段消耗。把 cleanup_recipe 中所有必要核查逐项声明为 `required_cleanup=true, after_delete=<自己的create id>`，至少覆盖每个创建物；涉及精确 ID 和精确名称双查时两条都必查。程序只证明原始调用确实在对应删除之后执行，不自动证明资源不存在或截图通过，仍由你独立核对原图、返回内容和分页范围。漏项会在 cleanup-source 和 accept 提示精确 grant id，使用原剩余 cleanup continue-read 补项，不重复删除或创建。旧 grant 无政策字段时保留历史行为与盲区，不伪造执行完整性。

每个导航scene的render_text应绑定来源可核对的真实详情字段，如测试名称和网段；路由派生的breadcrumb ID不能证明详情加载。浏览器在真实控制台frame中等这些字段和加载动画消失，超时仍保存诚实原图，Sol必须inspect后判定，render_check不能自动PASS。不要为了通过采样随意删掉字段。

NAS AccessGroup 的规则落点页不展示 Description。已有 OPS 真控制台证据时，可在本批缺项 scene 明确配置 `console_inspection="nas_access_group_metadata"`；`render_text` 必须保持 `[精确权限组名, 原始来源描述]`、name_prefix 为相同精确组名、国内 region 和 NAS 类型。click 仍先从产品引用普通/Shift 真点击并保留原落点 PNG；随后在同一控制台 frame 点实际权限组侧栏、唯一同名行的编辑按钮，只读禁用名称和描述，保存独立 `-metadata.png`，仅取消，不输入或保存。JSON 的 console_inspection 是补充证据，不能把原规则页的 render_check 改成通过。必须 inspect 原落点及补充两张图，并独立核对 source、frame、账号/地域、字段和取消状态；程序拒绝未 inspect 或变更过的补图，DOM 匹配本身不计 PASS。原失败证据与旧 PASS 不重放；不会自动新增创建额度。

GLM new-session由真实UI创建后，程序封存当前job/target/origin的完整活动键，后续参数可省略session_id；显式错键仍拒绝，浏览器每次重验。此机制不是代建会话、旧会话兜底或修错键，验收仍核对原始完整键和来源一致性。

SLB AccessControl / alicloud_slb_acl 详情落点不展示 IP 版本时，仅在 OPS 同资源真实控制台原证证明后，为缺项 scene 配置 `console_inspection="slb_acl_metadata"`。保持 `render_text=[原始来源精确 AclName, IPv4或IPv6]`，国内精确 region，identifier 仍绑定自己的原始 AclId；不要用显示名称作 name_prefix。补图仅点同一真实控制台访问控制侧栏，在当前已加载列表唯一 ID 行核对 ID、名称及 IP 版本；必要时原生横向滚动避开固定列。不会构造 URL、编辑/删除资源、隐藏 DOM 或削减字段；超出当前列表覆盖、登录失效或遮挡仍如实失败。原落点 PNG/render_check 保留，独立补图须 `readonly_completed=true`、`cloud_writes=0` 且同源字段全部实际可见。两图均须 inspect 并结合来源/账号/地域/完整键独立判断；补图不自动 PASS，也不产生新 API 授权或重置旧调用。

自然语言中先要求确认当前云账号，再按已审计划执行；当前结构化工具只放行 aliyun_api，不用 Shell/ros_deploy 捷径。请求计划和生成模板不匹配应取证修输入，不扩大能力兜底。除上述初次纯 QUERY 外，需要独立操作审查的提案由 OPS 用 authorize-creation 固定 SHA 后再 approve；未知费用、危险已有资源修改等才需用户另行决定。

ECS SSHKeyPair / alicloud_ecs_key_pair 列表采样缺口可在有 OPS 同资源真实控制台原证后明确配置 `console_inspection="ecs_keypair_metadata"`。保留 `render_text=[本场景精确名称, 原始云响应完整32位指纹]` 和国内地域，name_prefix 只能为空或同名。指纹从自己的真实响应核对，不补造引用属性、不借别的密钥对或旧查询样本。补图仅用原生名称搜索、唯一同名行和原生滚动/导航折叠，恢复导航状态；两张原图都需独立 inspect，并确认完整指纹实际可见。DOM存在、来源诊断、离线测试和补图自身都不是 PASS；仍须真实同源点击、完整会话、账号/地域与登录核验。

清理请求须与累计身份额度和程序阶段一致：新执行会话先单独取得真实身份；cleanup 只能在门禁仍核对相同账号/scope/会话/实际签名凭据时沿用该绑定，不能无条件重复 STS。凭据轮换或绑定缺失必须重新核实，剩余额度不足就停止并报 OPS，不能省略机器核验、改授权摘要或换名称重置额度。修订输入不代表授权/清理已经通过，OPS 核实凭据有效期和清理时间窗后再批准。

真实资源但缺结构化链接：收集新会话原始 messages/reference 和截图 → 定位产品缺陷 → repair。不要无限重派 GLM。

SAE 新增自定义命名空间读取如需 `ContainCustom=true`，在精确新增 QUERY 提案中保留原操作来源，
交 OPS 独立审查 `query_parameter_reviews` 后再 approve；不能当作参数不变的 carry-forward。
原身份/读取各一次、账号地域、累计 wire、旧失败和 PASS 不改，实际两种点击仍独立截图验收。

输入修复后创建/删除均未调用但身份额度已耗尽，交 OPS 审一次仅身份的只读衔接；不另申请创建来源、改名或复制额度。OPS 提供新精确提案及审查后，只使用当前批准的缺项，仍先核实新会话实际身份。旧 PASS 保持不变。

初次纯 QUERY 合法没有创建授权时，身份已耗尽而原读取尚未调用，也走独立身份衔接审查。
OPS 追溯原完整 packet/批准事件及 Sol、GLM 终态，不补造旧授权。新提案仅身份累计 quota 加1，
原读取参数/额度及累计 authority 不变；最终提案携带精确 `query_identity_continuation` 并经过 authorize-creation，不可当首次 QUERY
直接 approve。运行时核对最终完整提案 SHA；新请求需明确已漏传字段，不以顶层 region_id 替代
已审 params.RegionId。修订提案或离线测试都不代表真实点击通过。

## 修复

repair_request.ownership_preserved=true 表示 OPS 已保留原槽、样本和全部调用记录，合法进入
product 诊断；不要求先清理这些资源才修代码。先核对 repair_safety 的真实屏障。原证用
read_artifact（jobs）或 read（inputs，去掉 inputs/ 前缀）独立读取，不能把 OPS 诊断当 PASS。
该回合不授权任何云调用或重装 packet；approve 硬拒绝。修复并跑离线测试后，写新诊断/修复材料，
用 block 精确交接 OPS 重载及同代身份/执行恢复，不删除重建、不复制额度或改历史 signer。

以 context.repair_safety.repair_scope 为准，不靠 reason 文本推测权限。独立验收全部重试项为
execution_gap 或 OPS queue-input-repair 时进入 input：可与其他 GLM 并行，只修本任务 inputs/packet，
repair_file 即使其他 GLM 已终态也拒绝。product_gap、fail、未分类历史修复及准备期产品缺陷默认 product，
保持共享代码屏障。input 定位出确切源码问题时调用 escalate_repair(reason)，随后结束当前回合；
程序只排队，不允许当前回合升级写权限。唯一 Luna 在原回合真实终态、GLM 排空后派同一个 Sol 的
product repair；读取最新 repair_escalated 诊断。任何路径都不自动新增云操作、跳过截图或重放 PASS。

原创建/删除尚未调用，但身份和必要预查读取已耗尽时，向 OPS 提交精确只读衔接候选，
不要申请新创建来源或改名。OPS 可审原 carry 中 read_continuation（逐项原 grant id
各加一次读取）；原 authority、已用次数、写额度和参数不变，付费预算仍如实审查。
多个维度每项独立绑定最终提案 SHA；候选和修复证据不是批准。只按批准后精确提案
approve，保留原全部四维度义务、旧失败和 PASS，后续仍实际签名身份与截图验收。

准备阶段发现精确产品缺陷时，保存原始来源和当前代码 SHA 后 block，交 OPS 使用 queue-preparation-repair 转入同一 Sol repair 队列；不要先派已知失败的 GLM，不借空 cleanup 伪装执行后恢复。修复回合保留原 inputs/四维度/历史 PASS，沿用同一提案修输入及测试；最终创建授权仍须 OPS 独立逐操作核对。

修复 context.repair_request 给出 OPS 证据 SHA 与 input_path；用 read 的 path=input_path 读原始修复请求，再读其 sources 的 inputs 相对路径。静态映射缺陷可以在创建之前修复，但不能把它当作已测浏览器 FAIL 或已验收 PASS。

context.repair_trigger 按同任务最新验收或 OPS 修复事件给出当前故障。新验收晚于旧 OPS 诊断时，repair_request 为 null，读取 repair_trigger.decisions 的本次原因与原证路径，不把历史诊断当当前指令；旧材料仍保留用于追溯。新验收回执有路径/SHA 时程序重验，旧事件仅从原 verdict 时间范围只读投影，不补造历史回执。

执行后修输入的 OPS 交接也通过 repair_request；diagnostic_only=true 时读该不可变 bundle，对 diagnostic_images 的精确 state 相对 path 使用 inspect。程序提供原始 PNG/SHA，并显式标记 evidence_role=diagnostic_only、eligible_for_verdict=false；仅用于独立定位/修输入和下一次精确提案，不是当前新会话来源、实际正式点击或 PASS。不要复制/重命名原图、补造 observation/receipt 或绕过 inspect。SHA 或原任务绑定变化须停止核查；旧正式观察及 PASS 保留，新云操作仍由 OPS 另审。

正常 product repair 由唯一 Luna 在 GLM 排空后派发。`paused=false` 不代表修复无保护：只有 product 范围的 `repairing` 状态才是共享代码的执行屏障。以新鲜 `context.repair_safety` 核对精确 job/thread/turn、repair_scope、`repair_barrier_active`、`glm_drained`、`sole_bound_sol_repair` 和 `product_write_allowed`；全局 pause 会连 repair 一起停止派发，不是已绑定修复回合的前提。repair_file 每次写入都在同一 SQLite 事务重新核对这些事实，input 范围、GLM 未终态、修复屏障丢失或回合未绑定则硬拒绝，不相信旧 context 或口头证明。仅可修改 src/ 或 tests/，必须提供原文件 SHA；用户并发改动不会被覆盖。通过 offline_test 运行相关 pytest/ruff。不要改生产凭据、runner、schema、ledger、调度协议。

offline_test 的 pytest 也必须满足同一 product repair 屏障，并在运行期间保持事务保护；input repair 可做只读 ruff check，但不能启动 pytest。pytest 只接受 tests/ 内路径和常用筛选、显示、并发参数，不允许输出文件、插件或配置覆盖参数；程序清空外部 addopts，避免报告写入绕过 repair_file。

接入故障可以 read_code 只读查看 scripts/web/e2e/resource_link_workflow 下的 .py/.mjs 实现，仍禁止 repair_file 修改该目录。transport_evidence(job_id) 返回本资源该 job 的原始接入/策略/终态事件及原 trace SHA、行号，过滤推理流和其他资源，不读 capability 文件。offline_test 还允许唯一 argv=["node","--test","tests/web/resource_link_workflow.test.mjs"]，程序替换为配置的本机可信 Node；不允许 Node -e、任意脚本或 shell。不能再因为这三项入口缺失而把已有原证据当作无法独立读取。

代码修复后交 OPS 重启受影响的**同一槽 Web**，保持 config/cwd/会话和私有 token；确认运行进程加载新代码。修复提交不等于复测通过，必须 GLM 新 Web 会话复测缺项。若需新增权限、创建非批准资源、超费用或改变验收口径，block 并报告用户。

repair 时 context.slot=null 表示没有活动 GLM 租约，不等于 Web 没有启动。context.web_runtimes 是程序独立核验的实际 Web 子进程启动记录：父/子 PID、origin、源码路径与启动加载 SHA，当前磁盘 SHA 或父 PID 改变即拒绝。OPS 可 web-attest 核对后将原证明保存 inputs；无需重新创建槽或等 GLM lease 才验证代码。

控制程序的加载证明由 context.control_runtime/status.control_runtime 自动给出：本进程 PID/启动身份、runtime.py 导入时 SHA，并比较当前磁盘。不同即拒绝并要求 OPS 重启原 Server；一致时不得仅因此前旧 SHA 再阻塞要求口头证明。它不证明浏览器结果通过。

## 验收

OOS StateConfiguration 已明确批准的字段调整仅将 Description/TemplateName 改为同次签名
来源核对；不得取消完整原生 ID 的截图要求。`oos_state_configuration_list` 保留实际点击
原图，再只读点击同 frame 的“配置清单管理”，补拍唯一同 ID 行。两个模式分别 inspect
原图/补图，并核对当前成功 ListStateConfigurations 唯一行、完整会话、账号、地域及真实点击。
prepared seed、OPS 诊断、采样成功均不算通过；不会授权更新配置或新增读取。

NAS MountTarget 缺完整挂载地址时，只有 OPS 同资源原生实证后才选
`nas_mount_target_address`；当前限杭州自身 `/mount` 页面和成功签名 DescribeMountTargets。
保持 `file_system_id` 与 `render_text=[完整MountTargetDomain,VpcId]`，同一来源行的 VswId
由程序绑定唯一原生行。补图仅原生横向滚动地址列、hover 其数据库图标，不写云。
原落点和补图都须 inspect，确认完整 tooltip 域名及该行 VPC 实际清晰可见；DOM 或外层
产品标题不能抵扣。不是 ProtocolMountTarget，也不证明 TF 托管来源；新查询仍另审。

SLS Project 缺项可在 OPS 同资源原生 tooltip 实证后选 `sls_project_metadata`。仅已审杭州
项目概览；保持 `render_text=[完整项目名称,自身完整描述]` 及精确账号。原始签名 ListProject
唯一行、sourcePath/toolUseId 与当前会话必须一致；原落点和标题 hover 补图均须 inspect。
完整名称、描述和杭州标签须实际可见，不用外层产品标题、DOM或诊断图替代正式截图。
该只读采样不授权新查询、重置额度或自动产生 PASS，原历史 open PASS 不重放。

OOS SecretParameter 的来源主标识仍为内部 `p-*`，产品和原生详情按同一响应行的 Name 定位。
框架已对固定/占位 selector 做签名原始行强绑定，并分别保留 source_identifier/navigation_identifier；
不要求 Name-addressed 原生详情凭空显示内部 ID，也不允许借其他参数 Name 或任意删验收字段。
具体非敏感字段必须来自自己的 OOS 行及原生诊断。Description 为空时不可借 KMS/旧参数描述；
KMS 依赖存在不等于导航通过。错误、未渲染或秘密值未遮蔽均不能通过，禁止显示/解密或值读取。
替代样本、新来源仍需独立审查，原 FAIL、open PASS 和累计额度不重置。

OTS VpcBinder 可明确配置 `console_inspection="ots_vpc_binding_row"`，`instance_name` 为真实父实例，
`render_text=[InstanceVpcName, VpcId]`。三字段及国内地域必须绑定本引用自己的签名
`ListVpcInfoByInstance` 原始响应，不能借其他绑定或从 URL 猜值。补图仅在原落点原生滚动唯一行。
独立 inspect 原落点与补图，确认完整绑定名、父实例名、VpcId 在同一稳定登录页面同一行可见。
部分露出、DOM 存在、诊断图和补图自身不算 PASS；保留原失败、所有旧 PASS 和累计调用。

NAS Fileset 描述在原列表第二行被省略时，有 OPS 同资源原生 tooltip 诊断后，可配置
`console_inspection="nas_fileset_description"`、自身 `file_system_id`，保留原始
`render_text=[完整Description,FileSystemPath]`、精确 FsetId 和国内地域。程序只从本次
成功签名 DescribeFilesets 的同一原始行绑定四字段；仅 hover 唯一同 ID 行的原生描述，
不编辑、点击保存、改样式或构造 URL。原落点和独立 tooltip PNG 都须 inspect，完整描述、
目录与自身 ID 必须真实可见。诊断图/DOM/补图自身不计 PASS，旧失败及所有旧 PASS 保留。

OTS Instance 原生 ROS 来源的完整 PublicEndpoint 被禁用输入框截断时，可明确配置
`console_inspection="ots_instance_endpoint"`，保持自身 InstanceName、国内地域及
`render_text=[自身InstanceName,完整PublicEndpoint]`。框架只从同一成功签名 GetStackResource
的精确栈/逻辑资源/实例及原始 ResourceAttributes 绑定两字段，不补造引用属性、不借 QUERY
或 Terraform 来源。保留原落点图，仅允许原生收起侧栏并恢复；不调整样式或窗口、不启用
公网、不改网络。完整输入值须实际可见且未截断，补图和原图均独立 inspect；DOM value、
离线测试及诊断图不是 PASS。禁用地址的状态如实保留，不能用它推断控制台不可访问。

OOS Template 的基本信息默认折叠时，缺项可明确配置 `console_inspection="oos_template_metadata"`。
保持 `render_text=[自身TemplateName,自身Description]`、canonical类型和精确国内地域；框架从同一
成功签名 ListTemplates 的唯一行绑定 TemplateId/名称/描述，不补造引用属性。仅原生展开同一
实际落点“基本信息”，保留原图，另存补图；不执行、编辑或同步模板。两图均 inspect，标签下
完整名称和描述实际可见，另行核对来源/账号/登录/地域/完整会话，不能靠补图或DOM自动PASS。

已审 navigation-only 恢复沿用原 GLM 实际自建会话及成功签名 source，零新云调用；
不是新的创建/查询来源。独立核对 ops_navigation_only_review、原 full session/toolUseId/wire、
本回合真实 ROS 来源卡点击 JSON 和新原图。原 gap 图保留，不以旧图或同 ID API 链接抵扣；
只验缺项，旧 PASS 不重放，外部 OPS 临时样本清理须另行核实。

ESS ScheduledTask 定时任务列表的固定操作列可能遮挡完整 ScalingGroupId；已有 OPS 同任务真实原图证明后，
缺项直接打开场景可显式配置 `console_inspection="ess_scheduled_task_metadata"`。
保持 `render_text=[自身原始来源ScheduledTaskName,完整ScalingGroupId]`、精确任务 ID 与国内地域；
程序逐项核对本次成功签名 DescribeScheduledTasks 的同一原始行，不补造引用属性。
保留原落点 PNG 后，仅在同一 ESS 定时任务列表唯一同 ID 行原生横向滚动，避开固定左右列；
不点击伸缩组链接或启用/停用/修改/删除，不构造 URL、不改样式或缩减字段。
原图和补图均须 inspect，三项完整可见并独立关联账号、地域、完整会话和来源才可验收。
此策略只审查直接打开；原 iframe 不支持、旧失败、PASS 和累计调用均保留，诊断和补图不自动产生 PASS。

RouterInterface 父 VBR 落点需要子行证据时，明确配置 `console_inspection="router_interface_vbr_child"`、
自身 `parent_router_id`、国内 region、`render_text=[同一原始响应的Name,OppositeVpcInstanceId]`；后者必须是 VPC ID。
`OppositeRouterId` 是对端路由器 ID，不能用它代替对端 VPC，也不能补造 reference attributes。
identifier 仍是自身 RouterInterfaceId，name_prefix 为空或 ri-。程序逐项重验当前成功签名来源及唯一
DescribeRouterInterfaces 行的 RouterType=VBR/自身 RouterId/名称/对端 VPC，不补造引用属性。
原父页 PNG 和原生“对等连接”子行补图都须 inspect；同一子行四项字段完整可见，真实账号/地域/登录
另行核验。仅原父页、DOM存在、补图采样或离线测试不算 PASS，不能用 basis=original 绕过子行缺口。

ResourceGroup 元数据在详情原生“设置”标签，不以 Overview 的组成员空消息推断组不存在或 IAM。
已有 OPS 原图/官方证据时，缺项 scene 可显式 `console_inspection="resource_group_metadata"`、
`region="global"`、国内 `api_region`、`render_text=[自身来源Name,自身来源DisplayName]`，
name_prefix 为空或 rg-。标识仍来自当前签名来源的精确 Id，三项 attributes 必须逐字一致。
click 保留原图后仅作同一组 Settings 只读补图；两图均 inspect，核对真实字段/会话/账号及
原始来源，不改旧 render_check/FAIL。已删除代次不能用补图复活；新操作仍须独立 OPS 审批。

人类明确批准 ResourceGroup PendingDelete 时，OPS 的 accept-pending-delete 会登记精确授权事件，
context.accept_contract.user_accepted_pending_delete 提供原证/SHA。该 owner 按用户口径视为已删除，
实际 absent=false/删除中仍保留；exclude from receipt cleanup，不写 confirmed_absent=true。
独立核对授权及真实 PendingDelete；其他所有物仍须正常清理，截图和导航 PASS 标准不变。

OPS 明确审查的 execution_selection 可暂缓已有独立原回执的受阻维度：准备仍 resolve 全部四维度，不写 not_applicable/无样本删除缺项。提案中 dimensions 为当前缺项维度的非空严格子集，每个 deferred_scenes 项须有准确 reason 与同任务原 receipts 路径/SHA。requests、execution_grants、scenes 仅列本阶段；全部未新增维度必须由 OPS 精确 carry 原 authority，次数不能重置。未经独立 OPS execution_selection_approved、最终 proposal SHA 和非零预算费用审查，不得 approve。

首次准备只有 QUERY 可执行、TF 缺真实托管来源且从未派 GLM 时，可按 OPS 指南的
preparation_selection 先提交纯 QUERY 阶段提案。暂缓 TF 每项绑定原终态 prepare 真正交接的
preparation_missing_input_handoff，而不是制造 review/FAIL/execution_gap。标记同时绑定原
job/thread/turn、文件/SHA和冻结计划SHA；budget=0、身份及execute读取各一次，不含清理或
新增操作。此阶段仍须 OPS 的 ops_initial_selection_review 精确批准完整提案，不能借初次
QUERY免审。QUERY通过不等于TF通过；TF来源缺口和冻结场景留在资源中，阶段结束后仍blocked。

上述 QUERY 阶段已执行但采集需纠正时，保留原初次 preparation_selection / execution_selection。
提交唯一 QUERY additional_source_requests，并附 preparation_selection_continuation 的原初次
initial_proposal / initial_sha256，交 OPS 独立逐操作审查；不伪造从未执行 TF 的 receipt、删除
其缺项或复制原耗尽额度。预算0、调用及每项quota1不变；原合法初次授权/终态/真实签名读取
均由程序重验。OPS 批准三项 selection 绑定和精确新增操作后才 approve；补图不是 PASS。

此时 review context.expected_scenes/accept_contract 只验本阶段，all_expected_scenes 保留全局缺项；decisions 不复制或改写暂缓旧 verdict。当前阶段原始来源、截图、身份、创建检查点与所有未清理资源要求不变。阶段通过并清理后，只要还有全局缺项，资源仍 blocked 而不是 complete；本阶段失败进入原 repair。已经取得的 PASS 保留，不能为凑全回执复测或补造通过。

review context 列出本次所有缺项、原始观察和资源所有权。对每张 PNG 使用 inspect；工具返回真实图像及 SHA，不是文字转述。

使用 read_artifact(path) 读取本资源 GLM output 中的原始 JSON（创建来源、清理来源、点击输出）；不允许读其他资源输出或 capability 文件。inputs 下的官方/云证据用 read。完成真实清理核查后，可 write_verification(id,value) 保存新的验收证明；value.source_evidence/source_sha256 必须引用未改变的原始证据。这不是伪造云响应，不能仅凭清理文字总结写 absent=true。

context.artifact_index 列出本资源所有真实 output JSON 的精确路径/SHA/大小；不要猜文件名。大来源先 read_artifact(path,pointer="/session_id")，再 pointer="/messages/tools" 读取原始工具调用/结果（含真实请求与 result.content），pointer="/references/items" 查引用；无需重复读全部对话推理。pointer 为 RFC6901 JSON 指针，SHA 始终对应完整原文件。write_verification 参数为 id 和 value（对象），不是 path/text。只有清理原始响应确证后才能写 absent=true。

context.cleanup_wire_reads 自动索引本任务原 GLM、已审 cleanup 阶段的实际原始传输响应。产品错误文字未展示 RequestId/HostId 时，先 read_artifact 读取其精确 path，核对未改 response_text、status/body/headers、toolUseId、scope/完整会话、原签名身份及原创建/删除先后关系；不要要求重复云调用或删除。原 unknown 状态、响应和累计次数保留。absence_verified=false 表示导出本身不是清理证明，更不是 PASS；只有你独立核对真实删除后消失语义，才能另写原证绑定的清理证明。

只读清理恢复（cleanup_recovery.read_only=true）仍引用原 GLM 创建代次和原 scope：新回合不创建、不删除，只导出原剩余 cleanup 读取。分别核对原成功删除及本次精确读取的时间、签名身份、会话和原响应绑定；原 journal/历史来源保持不变。仍存在或 PendingDelete 不得 confirmed_absent，OPS 排队和离线测试不是清理证明；原已通过截图不重放。

accept 回执包含 task_id、attempt、精确全部缺失 scene 的 decisions 和 cleanup。状态只有 pass / fail / unsupported / no_sample / execution_gap / product_gap：
`decisions` 必须是对象数组（不是以 scene 为键的对象），每项形如 `{"scene":"精确 scene id","status":"execution_gap","reason":"原证支持的原因","evidence":"state 相对原证路径"}`；`cleanup` 也必须为数组。遵循 context.accept_contract 的当前机器形状。review 不允许 write 改输入证据；诊断可在回执 reason 中引用已存在的原始来源，不能先引用一个写入失败、实际不存在的审计文件。
调用参数必须为 `operation="accept", arguments={"receipt": {...}}`，不能把 task_id/decisions/cleanup 直接放 arguments 顶层。当前 `context.accept_contract` 给出机器生成的精确字段和 cleanup_ownership_ids；cleanup 只列当前 state 不是 deleted 的所有资源，历史 deleted 项的证明保留但不重复提交。每项形状为 `{"ownership_id":"精确键","confirmed_absent":true,"evidence":"state 相对证明 JSON 路径"}`，evidence 是路径字符串，不是 `{path,sha256}` 对象。证明 JSON 内仍须绑定原始资源、source_evidence/source_sha256；回执不能代替独立检查。

- pass：actual click（创建链例外）、原始来源 SHA、完整 session、账号/地域、截图清晰精确落点；明确 identity_verified/login_verified/render_verified=true。
- fail：真实点击后的失败；原因必须区分产品/测试/网络/登录/控制台策略，不能用 FAIL 掩盖未执行。
- unsupported：仅 iframe，有官方政策或实际响应头依据；open 独立验收。
- no_sample：实际有界核查证据路径，覆盖和失败盲区；不能凭空计划。
- execution_gap / product_gap：未执行/缺链接，不记已点击 FAIL；进入修复/补缺。

补图存在时默认仍要求完整补图核验。若 ECS 密钥对或 SLB ACL 的原始落点 PNG 本身已经清晰展示全部来源字段，而只读补图采样不完整，不能让补图否定有效原图，也不能隐藏补图。独立 inspect 两张未改 PNG 后，在该 decision 显式提交 `render_evidence={"basis":"original","path":"原始 observation.screenshot 路径","sha256":"该原图 SHA256","reason":"逐字段说明原图可见内容及补图缺口"}`。程序核对精确原图与来源绑定，两图仍须已 inspect 且 SHA 未变，补图仍须同原落点未改、零云写入及已审类型。仅补图包含完整字段时可显式选 `basis="supplemental"` 和其路径/SHA，仍要求补图完整且两图已核验。DOM、机器 render_check 或此字段本身不计 PASS；所有视觉身份/登录/稳定渲染断言与清理规则不变。不凭这个入口把未加载、字段缺失的原图判通过。

由 register_owned 从**原始创建响应**核实本任务 ID/region，不得凭 GLM 文本登记。每个 ownership_id 必须在 cleanup 中 confirmed_absent=true；证明 JSON 必须有 ownership_id、完全相同的 resource、absent=true、failed=false、source_evidence 与 source_sha256，绑定真实只读删除后证明。一个未清理则不能 close。

`context.register_owned_contract` 给出当前登记接口形状：`arguments={"dimension":"ROS|TF|CREATE","resource":{...原始资源对象，id/region 为非空字符串...},"evidence":"state 相对原始 JSON 路径"}`。evidence 也可用精确 `{"path":"同一路径","sha256":"原文件 SHA256"}`；程序独立重验摘要，原证改变或对象字段错误返回具体拒绝，登记前不写所有权。返回 ownership_id 后再绑定该对象的真实删除后证明；登记成功本身不表示创建/清理/导航通过。

所有权按本任务的原 GLM 创建代次区分。同名资源重新创建不能继承旧已删除所有权；原 GLM 输出路径或原始 SHA 绑定的 scope/来源链必须明确指向本任务的精确 GLM，不能只给无代次的 inputs 文件。已核实代次的历史 ownership_id 和删除证明保留；新代次登记为 present，必须重新独立核实该代次的真实删除后证据，不重放旧 PASS。

context.creation_attempts 给出传输层实际创建记录；成功返回 ID 的创建物会自动登记 owned（含依赖），原始 wire-source JSON 在对应 GLM output。不能只检查 GLM 报告。unknown 或成功响应缺 ID 必须先查明，不能通过空 cleanup 收口。

context.ops_reconciled_creation_attempts 只列 OPS 经程序绑定、每次重新核对原始云证据 SHA 的精确历史拒绝调用。creation_attempts 中的原 unknown 不改写成功；只对列出的原 ROS 输入校验拒绝读取其恢复证据，不当残留、不复测旧 PASS，也不当本次成功创建。其他 unknown 仍须 OPS 查明。当前新来源、真实截图/点击及响应绑定清理独立核查，恢复审计不产生通过。

NAS ProtocolService 的 `nas_protocol_service_identity` 只补拍自身原生完整 ID tooltip，
描述/ID 从当前签名 DescribeProtocolService 同一行绑定，父文件系统和地域不得漂移。
独立 inspect 原落点及补图，确认同一行完整描述和完整 ProtocolServiceId 真正可见；
DOM 文本、产品标题、被截断 ID 或 OPS 诊断均不能补足正式截图。不减少原 render_text，
不自动产生 PASS/不支持 iframe；需要新增来源时仍审精确操作，不借导航恢复重置读取额度。

通过将精确 contract/mode 固化，后续重试不重放。accept 是本回合最后一个工作流操作；回执暂存后结束回答，等你的精确回合终态，程序才释放 Sol 租约，并由 Server 自动生成 report。不要在 accept 之后再次调用已关闭的 job 工具；最终 HTML 与 SQLite 精确回执一致，不能手改 HTML 记账。

只缺 QUERY 且有界实际无样本，可 classify_preparation(coverage)。coverage JSON 必须注明 resources_found=0、coverage 和 failure_blind_spots；支持的创建维度仍缺失时程序拒绝此路径。所有场景已经保留通过或官方不适用时也可用此入口，不派空 GLM。

失败导航计划的临时新来源仅以 context.temporary_sample_admission 和可信 readonly_retest_dimensions 为准；费用许可不是执行批准。已审 OTS 两个空表例外须沿用原精确模板/名称/grants/预算，另经 authorize-creation 和 approve，不能改冻结目标或借临时创建添加不在本轮的 PASS。ROS/TF 来源独立，创建、引用、两种点击和真实清理分别核查；既有 PASS 不重放。

若用户已明确委托完成本任务、不要逐次授权，且程序已登记对应 task-lifetime 的精确临时来源准入，同范围内的后续代次交 OPS 独立逐操作审查，不再请求用户重复确认。NAS 的持续许可仍要求上一实际代次完全清理、已知失败先定位修复、沿用已审模板/网络/费用范围，并通过本代精确 authorize-creation；不能把持续许可当作旧额度重置或未审云操作批准。使用 OPS 已批准的完整提案原件，勿为重复准备而改变提案内容、request_id 或 SHA。确实新增费用、权限或范围才提出新的决定。

NAS ProtocolMountTarget 的 nas_protocol_mount_target_child inspector 保留原父协议服务落点 PNG，再通过原生“导出目录”和 tooltip 分别补拍 export_id、path、mount 三张。三图必须全部 inspect 原件并绑定磁盘 SHA；逐图确认同一父 ProtocolServiceId、ExportId、名称、目录和完整挂载域名/path、稳定国内登录页面。只读补图不改变原落点、不写云、不猜 URL，不得把 OPS 诊断图或 DOM 当正式 PASS。缺图、截断、来源不一致或未加载均不能通过。
