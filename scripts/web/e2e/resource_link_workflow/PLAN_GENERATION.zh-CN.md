# 生成新的资源类型测试计划

catalog.json 是永久编号目录；plan 是某次执行的冻结清单，HTML 是可读视图。新增资源和重测未通过/未测项都生成新 plan_id，不改老 plan。

执行机器不是资源或 plan 的维度：同一批准 plan 可在启动前选择 local/macmini，GLM/Chrome 跟随选择，Web/SQLite/Sol 保持本机。不要把两台机器重复导入为两个同资源 owner，不迁移未完成资源的原 GLM thread。历史 PASS、来源、截图标准、预算和调用数不会因机器选择改变。

定向导航复测可用 plan --retest-scenes <精确场景ID...>，支持多个资源实际 fail 的 QUERY / ROS
导航分支；ROS 为已有栈只读来源，不是再次创建。程序冻结 resource.retest_scope，导入时对照
永久目录与当前任务历史，拒绝 PASS、unsupported、未测、创建场景或跨来源。其余维度原样留存，
不伪造适用性或创建检查点。本次 complete 只代表冻结导航范围完成，不代表四维度全覆盖。

已有 kind=resource_link_targeted_retest_review 草案使用 `plan-from-review --review <原草案>
--id <新计划ID> --output <新路径>`。程序从 fresh task 正检查点移出已通过项，冻结四槽首波，
保存原草案 SHA、最新目录/历史/结果页基线和排除原因；导入重验原草案、精确范围及排除项的真实
正检查点。原草案不改写，不能静默删掉失败项。ROS 只允许固定 StackId 的 GetStack /
GetStackResource / ListStackResources，仍须 OPS 审实际操作授权；不授权创建或删除已有资源。

`resource_link_remaining_failures_retest_review` 同样使用 plan-from-review：资源去重、每个来源和
编号场景独立冻结。生成 `failed_navigation_managed` 范围，可包含 QUERY/ROS/TF；必须保留原稿
SHA和完整覆盖（执行项＋有真实正检查点的排除项）。TF只读来源要求固定StackId的ROS读取，
并在实际点击前核验本次同签名/账号/会话的GetStack返回StackType=Terraform、存活终态及
该栈自己的资源读取；本机Terraform、底层QUERY或计划标签不能替代托管证明。
独立历史H编号仅在原历史基线SHA、资源key、来源、维度和原状态逐项匹配时准入；不改历史文件，
不合并其他R编号入口。新来源只证明本次导航，不补造历史托管创建PASS。
该审阅类型按冻结原稿的资源列表顺序调度，分组优先级由程序从 runs.plan 恢复；不按永久
R编号重新排序，也不让 Luna 临时选题。阻塞项保留原任务并跳过，其余满足依赖、锁和审批的项
仍可补位。Sol resolve 重验完整原冻结计划及 lineage，不丢掉该来源或改写原计划。

新审阅原稿可显式填写 `initial_wave=1`，使首份就绪 packet 即可启动，之后仍最多四槽滚动。
生成器与 lineage 校验冻结该值；不能执行中临时降低已导入计划的门槛。未填时保持旧默认
`min(4,资源数)`。四并发专项验证仍可选4，容量与首波启动门槛不是同一个概念。

该审阅协议也接纳用户点名的 `review`（待核）导航项：contract.baseline_status 与编号 target
的 baseline_status 均保持原 `review`，生成与导入时逐项对照永久目录、最新任务及原审阅 SHA。
不把待核改成 fail，不接纳 untested/PASS/unsupported，不扩大未选维度；已有精确正检查点仍排除。
此例外仅走完整 plan-from-review lineage；普通无审阅的 --retest-scenes 仍仅允许实际 fail。

只读导航旧计划仍有 blocked 任务时，可在维护暂停且所有模型/动作/槽已排空、全部所有物
已独立清理后，使用 `import-plan <新计划> --successor-review inputs/<独立交接审查>.json`。
交接与新计划导入在同一事务中完成；审查绑定旧新冻结 SHA、旧任务快照、完整场景、人类
授权和复用的唯一 Sol。只接受所选旧任务从未执行写操作、无未知调用和无创建所有权的
只读导航交接。原 blocked 任务标 `superseded`，不是 complete/PASS；旧 plan/spec/attempt、
原图/回执、预算预留及 wire 全部保留。新计划不得丢掉剩余场景或借交接复制创建额度。

## 新增与选择

1. 从主流常见服务选资源，先确认费用、安全删除、依赖和官方支持；禁止把有高风险凭据输出的资源直接作为默认候选。
2. 新类型用 catalog-add --spec <新资源JSON> 自动追加永久 R 编号，不复用、不排序重编号。填写唯一 key、service、name、分组、优先级、四维度和独立 contract；查询和创建 API 不合并。
3. 每维度 support=unknown/supported/not_applicable；not_applicable 要 official_basis。TF.execution 必须 ros_managed_terraform。所有新 contract 的两分支均 untested，不能借共享 API 查询 PASS。
4. 依赖填 catalog 内已有 R ID；本次 plan 也包含依赖或用已验收前置条件代替。互斥锁用于共享 VPC/账号级控制台偏好、删除影响或排他资源。
5. queries 数组给每项一次有界只读核查的 id/product/action/parameters；action 必须 List/Get/Describe，程序固定 profile，不给 GLM 重新扫描。
6. 使用 CLI plan 按 R 编号生成文件，import-plan 校验四维度、永久身份、唯一 owner、依赖无环和冻结 plan_id。

```sh
uv run python -m scripts.web.e2e.resource_link_workflow plan --id common-network-v2 --resources R0001 R0002 --output evidence/common-network-v2.json
uv run python -m scripts.web.e2e.resource_link_workflow import-plan evidence/common-network-v2.json --budget-cents 100
```

100 是示例上限（单位分），不是默认用户批准。选择历史未测类型时仍保留其旧结果；同一 R 不能同时属于两个未完成 run。需要修改已导入范围，用新计划，不原地改 database。

四槽并发验证可在 plan 命令加 `--initial-wave 4`。首个 GLM 预留前，程序必须凑齐四个已批准、锁不冲突的 packet 和四个空闲槽；随后按空闲槽滚动派发。计划必须有至少四个无依赖且互不冲突的资源，否则校验拒绝，避免首波门槛造成依赖死锁。此选项只是启动门槛，不证明实际四并发；验收须核对真实 turn 时间、资源/槽/会话绑定和后两项补位事件。

## 输入合法性

实际官方 route/query plan、原始 API 响应、ROS 栈/资源列表、托管 Terraform 输出都由 Sol 准备，SHA 固定。metadata_missing / route_catalog L1 / ROS 不适用只是输入问题或某维度事实，不能推导没有云资源。
计划只有未知来源时先修输入；有真实资源则必须 UI 测试。API 创建与 ROS/Terraform 创建必须有清理 recipe、所有权和预算。依赖资源创建也纳入成本和所有权，不能隐性创建。

执行 packet 另有按四维度拆分的 execution_grants：精确 API/全部参数与内联模板、一次创建、由原始创建响应绑定的删除与状态查询。OPS 审查后用 authorize-creation 冻结独立授权，不要求另建受限身份；现有管理员账号如实记录，程序门禁而非 prompt 限制实际操作。所有请求先确认实际签名账号，地域、依赖、数量、预算不能用新重试扩大。

第二轮及采用新版读取协议的计划，在每个资源对象固定 `execution_policy_version=2`。每条 read grant 必须有 `read_policy.phase=execute|cleanup`，同一查询在两阶段使用不同 grant id 和独立已审额度。GetStack 的 `min_interval_seconds` 至少30；程序在实际传输前异步等待，不占 SQLite 锁、不提前扣次数，也不计入网络请求 timeout，更不是 GLM 总时限。清理必查项明确 `required_cleanup=true, after_delete=<本组create grant id>`；必须逐项在该原始响应绑定删除成功之后实际执行。每条创建至少有一个必查项；仅写 cleanup_recipe 文字不能替代这些机器字段。所需精确 ID、精确名称等检查全部逐项列为必查，不能拿其中一项替代另一项。旧计划和累计调用记录不迁移为新额度。

plan 命令会从同一 --state 最新已验收 task.spec 合并精确 positive checkpoint；不可把旧失败删掉变成通过。运行中只有程序验收路径更新 task.spec，冻结原始 plan 仍留档。最终 report 合并历史基线与新验收，不覆盖别的入口。

矩阵从 catalog 生成，新增编号后用 CLI matrix 重新导出；结果用 CLI report，正式终态收口时也自动生成。旧批次 055–067/069 空计划审计不当 PASS 导入。
