# ECS 引流改进回归

当前执行范围为下面 13 组，仅使用真实 LLM、本地 HTTP/SSE A2A 和只读云 API；不启动浏览器、ROS 前端或 ros-ai-agent。
沿用原四个文本模型、effort=low、fallback=false、场景 retries=0；不部署、购买、用券或替换云响应。

| 组 | 功能与入口 |
|---|---|
| A01 | 意图分流：九种 route-*、purchase-routing、noncloud-purchase |
| A02 | 助手可见链接、地域、prompt 与单次输出：复用 route-*，不重复执行 |
| A03 | P18 等待选案时重启 A2A，保留候选并继续进入 Step2：selection-recovery |
| A04 | P19 原四轮＋经济型/突发型 sizing；P19 各模型独立三轮 |
| A05 | P25 活动追问流程：promotional-offer-followup-a2a；最低值／推荐值提取单列非阻塞质量观察 |
| A06 | N29 轻量套餐、地域变更与年预算：n29-swas |
| A07 | N56 含盘/EIP 实际询价、Step1→Step2 衔接；参考单价下 total_cost 更新 |
| A08 | N59 保留数据盘、ESSD PL0、EIP 与实际询价：n59-price-chain |
| A09 | P34 租期、优惠券与预算：coupon-budget-a2a；不重测 ros-ai-agent |
| A10 | normal 询价、API/参数别名、周期与优惠价：pricing-normal、api-aliases |
| A11 | Step1 连续咨询、调整与新批次：ssf-step1-followup-a2a，各模型三轮 |
| A12 | Step2 咨询、比较、真实调参及取消：ssf-step2-followup-a2a，各模型三轮 |
| A13 | 同 Step 历史、跨 Step 与回滚新上下文、交接后 normal：context-boundaries、wiki-auto-handoff-a2a，各模型三轮 |

统一入口 `run_ecs_retention_matrix.py`：26 个去重 Case，基础一轮及 5 个稳定性 Case 追加两轮，四模型共 144 次。
实际请求与回合记录用于验证历史；旧 complete_step 不能代替本轮完成。跨 Step、回滚不能携带旧工具消息。
规格 API 未查到依据单列观察，不把它等同于规格数值错误。Step1 粗选型允许将具体 SKU、核数、内存和可用区留到 Step2 确认，记为 deferred；已明确写出的配置与 API 矛盾、TDX 规格族或地域／可用区范围错误仍失败。
选案恢复与上下文边界用例沿用业务的 Preview 软门槛：保留等待确认与询价结果验收，Preview 状态单列非阻塞观察，不强制 `preview_ready_for_create=true`。
权限驱动仅批准本 Case 的已有工具结果文件，其他申请明确拒绝并记录。未知权限不自动批准。
OAuth 由矩阵单一刷新线程维护在原测试配置中，子进程只复制集中更新的短期 STS。

## 历史覆盖索引

以下 30 组为此前完整覆盖的历史索引，不代表本轮还要执行浏览器、通用 Wiki 或其它渠道。
每组可以包含多条用例。当前 HEAD 的实际结果单独生成，已有入口不代表通过。
只改测试，不改业务；真实模型不降级、effort=low、场景 retries=0。不部署、购买或消耗代金券。

| 组 | 功能 | 验收入口／证据 |
|---|---|---|
| 01 | P18 恢复选案 | ssf-candidate-batch-same-process/cold-recovery |
| 02 | OpenMeta 临时失败重试 | tests/tools/cloud/aliyun/test_openmeta_retry.py，实际重试与耗尽提示 |
| 03 | 咨询分流 | ecs-retention-route-*，九种输入，真实 HTTP/SSE |
| 04 | 助手 URL 问题携带及一次发送 | route-renewal 特殊字符；浏览器打开及刷新，检查请求次数 |
| 05 | P19 购买分流 | ecs-retention-purchase-routing/noncloud-purchase，wiki-auto-p19-a2a |
| 06 | P25 活动方案追问 | promotional-offer-followup-a2a |
| 07 | 明确约束保留 | promotional-offer-followup-a2a，P19 实际实例规格证据 |
| 08 | N29 SWAS 套餐与年预算 | ecs-retention-n29-swas：杭州后改深圳，ListPlans、文档顺序、预算判断 |
| 09 | ECS 含盘询价 | ecs-retention-n56-price-chain/n59-price-chain，原始成功询价与盘明细，N59 检查实际模板保留200GiB ESSD PL0 |
| 10 | EIP 保有费 | n56/n59 链路；VPC 文档、实际查询与免收条件，人工核对 |
| 11 | 固定总价及用量另列 | plan-cost-refresh-a2a（参考价），n56/n59（真实价）分别报告 |
| 12 | total_cost 更新粗估 | plan-cost-refresh-a2a，浏览器候选／详情／历史总价与 SSE 对照 |
| 13 | 周期一致及旧字段 | coupon-budget-a2a，cost_field_compatibility 离线测试及周期人工复核 |
| 14 | 实付价投影及原价区分 | test_ecs_price_result.py；真实 pipeline/normal/handoff 询价响应对照 |
| 15 | 按量 Hour 换算 | n56/n59 720 小时、参考价 48/12 小时、pricing-normal 包月对照 |
| 16 | 询价参数别名 | ecs-retention-api-aliases；test_ecs_price_parameters.py、test_api_hooks.py |
| 17 | 代金券及预算 | coupon-budget-a2a（真实）；coupon-budget-mock-a2a（九类业务替身） |
| 18 | 旧 EIP 类型拒绝 | tests/pipeline/selling_solution_first 及相关 ROS 资源校验测试 |
| 19 | 新增 EIP 读 VPC Wiki | n56/n59；核对展示方案前 read_file 的实际正文，已有完整正文可复用 |
| 20 | Step1/2 无变化及更新 | ssf-step1/2-followup-a2a、followup-contract，浏览器 F10/F11 |
| 21 | complete_step 兼容 | step_followup_e2e；Qwen Omni ssf-a2a-image-asks-confirmation 完整图片调整 |
| 22 | 撤回答案证据 | step_followup_e2e、output-limit-* |
| 23 | 表单最新值立即应用 | 浏览器改完立即应用；参数值、请求、报价与模板对照 |
| 24 | P19 硬件与错误纠正 | wiki-auto-p19-a2a 四轮（B70 intel 分写）；ecs-retention-sizing；原始回答人工核对 |
| 25 | 阿里云正文提取 | tests/test_tools/test_web_fetch.py；真实官方文档 web_fetch smoke |
| 26 | Wiki 迁移及引用 | tests/knowledge、knowledge_paths、packaging、iac_code_web_solution；normal/pipeline/handoff |
| 27 | 加权关键词与召回 | tests/knowledge、wiki-auto-* 及真实主请求证据 |
| 28 | knowledge:// 工具及边界 | tests/tools/test_knowledge_paths.py；真实工具读取及安装包测试 |
| 29 | 最新候选批次历史重放 | 浏览器 F03/F06/F10/F11；SSE 与刷新后 DOM 对照 |
| 30 | 串行/异步/恢复 | wiki-auto-*-contract，wiki-auto-normal/pipeline/handoff-a2a，CLI/Web UI |

## 执行与证据

`run_ecs_retention_matrix.py` 调用统一 `run_e2e.py`，按上方 13 组和三轮规则执行，
每模型最多两个并发。无需 `--allow-cloud-write`。真实接口的空列表、失败和未执行不当成字段验证成功。
N56/N59 的少量粗估差异可以接受，须基于配置、周期、优惠及 EIP 条件解释；不要求精确相等。
流量用量可合理假设，无需逐字使用某个标签，但必须披露并从固定总价排除。

P19 第二轮的 `g8i/c8i/r8i` 列表保留为原始用户背景，不以“必须否定 c8i/r8i 支持 TDX”作为验收条件。
复核聚焦当前候选的 TDX 支持条件、SGX/TDX 区别、可核实的 CPU 事实及无变化续聊；未单独证实的支持范围不据此判错。
质疑轮可暂停提问；此时读取服务端当前提问对应的 Step1 快照，核对候选批次及完整结论保留，不要求尚未发生的 `complete_step` 无变化事件。
候选明确标注 TDX 支持待核实／待验证时，未匹配已核实支持表的可用区记为非阻塞模型质量观察项；保留 `plan_facts` 和 `placement_evidence` 原始失败结果。泛泛的“以实际为准”、声称已确认支持，以及规格、数量、候选保留等其他失败仍阻塞。

OAuth 保留于操作者原测试配置目录，通过 `cloud_test_credentials.py` 集中刷新，副本只含短期 STS。
日志、会话、询价、截图和账号数据保存在私有运行目录；公开概要只含检查、数值与错误类别。
每条保留首次失败，不自动重跑。人工复核未完成时不能把自动通过称作整组通过。

可见回答包括普通文本，以及正式 `input_required` 事件中 `ask_user_question.question` 的内容；未展示的工具参数与思考不算回答。P25 的已有方案可以保留，也可以通过带有效可见助手链接的 `rejected` 完成交接，不能误推进到部署。

历史检查必须使用实际 provider 请求消息。矩阵入口显式启用仅本地的 `SPAN_ONLY` 内容记录；缺少消息记录应标为证据缺失，不能据此断言历史为空或丢失。

流程结果与分支覆盖分开报告：未生成候选、未触发 `params` 兼容别名、未使用提问载体，或旧候选身份无法区分时，相关分支单列 not-exercised。此前单测／历史记录回放可作补充证据，不能冒充本轮真实 LLM 已覆盖。ROS `params` 兼容不改变工具 schema，暴露字段仍为 `parameters`。

P25 的最低值／推荐值语义提取作为非阻塞模型质量评测，保留原始约束和失败状态，不算已解决；流程 E2E 不再以此项判失败。方案展示、续聊状态、误推进、活动咨询链接等验收保持阻塞。确定性的约束保存、传递与验证仍由离线测试覆盖。

```bash
IAC_CODE_E2E_CLOUD_SOURCE=/private/test-config \
uv run --python 3.14 python scripts/ci/run_ecs_retention_matrix.py \
  --credential-source-dir /private/test-config --run-dir /private/regression-results
```

历史前端验收曾使用浏览器，本轮不执行。旧验收要求：不用 HTTP API 或组件测试代替 DOM 验收，使用 ros-test 测试账号，
本地前端＋本地 ROS relay＋当前 worktree A2A；禁止切生产账号。浏览器相关记录作为独立结果并附截图。
N40 只观察耗时，不将未验证的延迟改善列为通过。失败区分 product-code、runner、model、environment；
前置条件未成立时明确标为 blocked/not-exercised，不伪造下游通过。

只读API及正式响应投影可独立运行 `run_ecs_retention_cloud_probe.py --allow-real-cloud --run-dir <private-dir>`（配置目录由 `IAC_CODE_CONFIG_DIR` 指定）。外部浏览器可分别连接 `prepare_retention_browser.mjs` 和 `prepare_retention_wiki_web.py` 创建的隔离服务；两者只准备服务，不伪造DOM验收结果。
