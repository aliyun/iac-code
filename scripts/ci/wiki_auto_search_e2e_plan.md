# Wiki 自动搜索 E2E 用例设计

状态：2026-11-05。当前自动召回协议已升级为带权原文片段，逻辑路径、纯 Markdown 与共享模板知识补充见第 12–14 节，原文召回验收见第 9 节。第 7、8 节保留旧导航版本的历史记录，不代表本版复测结果；确定性契约通过不等于所有模型回答或业务流程都已通过。

## 1. 验收边界与复用入口

- 开关为 `IAC_CODE_WIKI_AUTO_SEARCH`，默认关闭。normal 会话首轮串行；参与检索的 pipeline 步骤首次执行串行，后续自由输入异步。
- 提词返回 0–8 项 `{"text":"检索词","relevance":0.8}`；每项独立取 0–1，不要求总和为 1。当前问题相关的具体 IaC 资源、API、参数和规格优先；完整厂商泛词在程序层剔除，完整资源标识符保留。空列表为 `skipped`，不搜索、不注入、不重试。
- 自动流程复用 grep 的命中原文，不额外调用 read_file 读取整页。按不同关键词组的系数之和给每行评分，同组空格／大小写变体共享系数，每行只加一次。同分优先正文，再按路径／行号稳定排序，全局最多选 10 行。
- 在原 `knowledge_search_results source="runtime"` XML 标签内按 `knowledge://` 逻辑路径分组，展示行号、命中原文和高亮；长行保留各关键词附近片段，以 … 标记省略，其余匹配行只列行号。总预算仍为至多 1200 token／6144 字节，可少于 10 行；无命中不注入。
- Wiki 快照及 side call 用量尽力保存，允许被覆盖或未进入备份；不为此增加终态合并、补备份或补偿重跑。用户消息、工具结果与业务状态不能受损。
- 沿用 `run_e2e.py` 的独立子进程、隔离配置、模型池、超时、私有日志、`summary.json` 和 JUnit／HTML 报告。
- 确定性用例由一个薄入口 `run_wiki_auto_search_contract.py` 调用进程级测试，参考 `run_output_limit_a2a.py`、`run_step_followup_contract.py`，不复制一套调度器。
- 真实模型用例扩展现有 `run_knowledge_wiki_a2a.py`，复用原 P19、handoff、Step1／Step2 的驱动和取证能力；保留现有五个 `knowledge-wiki-*` case 的含义。

## 2. 证据与时序判定

确定性测试使用生产 A2A Server、Provider 适配、AgentLoop、搜索实现、pipeline 和存储；只以本地脚本化模型端点代替外部 LLM／云接口。不替换 Wiki 搜索结果、不直接调用私有方法冒充整条链路，不绕开真实 `complete_step`。

本地模型端点用事件门闩控制“提词返回”“主模型开始／输出”“本轮终态”，验证发生顺序；不依赖固定 sleep 或“首 token 小于一秒”等机器速度断言。场景仍有 runner 总超时，门闩未满足须失败，不能超时后自动放行并判通过；不在产品中新增超时。

| 证据 | 来源与用途 |
|---|---|
| 提词调用与配置 | 实际 provider 请求／用量归属，区别于主模型请求；记录实际 effort、模型及 fallback |
| 查询归属与阶段 | query、scope、serial／async、阶段事件；按钮和 nudge 不能伪装成用户问题 |
| 主模型请求副本 | 实际发送的 messages／system；验证参考块出现的请求及绑定的原输入 |
| 搜索与阅读 | 自动搜索次数、命中文件；分别记录原文片段进入实际主模型请求与成功 read_file 的证据 |
| 业务与恢复 | 原始用户输入次数、工具结果关联、pipeline conclusion、步骤／任务终态 |

上述是内部审计／报告概念，不要求新增公开工具字段或 A2A 协议字段。阶段事件、文件名提及、答案碰巧正确，都不能单独证明结果进入请求或正文已经读过。

## 3. 首批 8 个确定性进程 E2E

以下 case ID 已注册。多个分支作为同一 case 内明确列出的参数样本执行，不机械扩成全排列；报告逐样本列出结果，不把部分通过算整项成功。

| ID／拟定 case | 输入与控制 | 必须验证 |
|---|---|---|
| C01 `wiki-auto-off-contract` | normal 和 Step1 各发送一个技术问题；开关未设置／false | 无提词、自动搜索、阶段事件或参考块；原模型／手动工具流程正常；有历史 Wiki metadata 也不能重新开启功能 |
| C02 `wiki-auto-normal-order-contract` | 首轮问 B70，暂扣关键词响应；第二轮改问 ECS 询价，暂扣该次关键词响应直到主模型已有输出 | 首轮主模型请求在提词／搜索完成后；第二轮在提词仍被扣住时可以输出。本轮后续正常模型请求可取用就绪结果；只发起一次提词，不因工具循环重复调用 |
| C03 `wiki-auto-empty-contract` | 提词端点正常返回 `[]`；样本覆盖 normal 首轮、normal 续聊和 Step1／Step2 | skipped；自动 grep、正文读取和参考块注入次数为零，不发搜索开始、不走错误兜底；主模型继续；恢复已保存 skipped 不重跑、不重置首轮 |
| C04 `wiki-auto-pipeline-order-contract` | 按真实协议完成 Step1、选择候选、进入 Step2；分别续聊并触发既有 fresh-context／resume；最后取消 | Step1／Step2 各自首次串行，续聊异步；Step2 从有效方案得到 seed，不搜索控制提示。选择／确认／权限按钮／内部 nudge 不提词；Step3 策略关闭。候选批次、空 conclusion、确认契约不变 |
| C05 `wiki-auto-late-isolation-contract` | 暂扣 q2，允许其主回答结束并开始主题不同的 q3；q3 准备完成后再释放 q2 | q2 不被主动当作 turn 结束垃圾丢弃；存活时可完成并尝试保存。q2 不抢占 q3 的参考块、不重复原输入、不再答一次、不重开任务。允许旧快照丢失，不能损坏新消息或业务状态 |
| C06 `wiki-auto-recovery-contract` | 用真实重启／历史加载路径覆盖下述子样本，不直接拼出恢复返回值 | 不重复处理原输入、不把异步升级为串行、不复活已结束或已取消的问题；每项单独取证 |
| C07 `wiki-auto-cancel-error-contract` | 串行准备时取消；异步时取消；提词失败／无效 JSON／本地搜索失败 | 取消中止所属工作，不走兜底继续回答；旧回调不能改写新问题。真正错误只走设计的一次本地兜底，不追加修复 LLM 调用；异步失败不使主回复失败；原权限等待／恢复正常 |
| C08 `wiki-auto-request-contract` | 返回带权关键词；命中正常文件与超出预算的真实 Wiki fixture；同一问题执行多个工具回合 | 请求副本只有一份当前 XML 参考块；原用户 content 不变，system 无结果块，不伪造 tool_result。实际注入带路径／行号的匹配原文，无关整页内容不注入；计数包含参考块，超预算按整行舍弃，路径与 XML 标签完整 |

### C06 恢复子样本

1. 已完成快照：同一未完成问题正常恢复，复用已有结果，提词调用不增加。
2. 原串行准备中断：恢复时丢弃未完成的 Wiki 查询，不重新提词或搜索，不回写历史 metadata，也不重复追加用户输入。
3. 原异步准备中断：恢复时同样允许丢弃，主模型不等待；服务端未死的 SSE 重连仍复用活任务，不创建第二份工作。
4. 主回答已结束，只有 pending／快照已丢失：加载历史或重启不补跑、不补答；后续输入仍按后续策略，不因缺失 metadata 重计首轮。
5. 恢复对账：同一输入的 Wiki metadata 存在／缺失／pending／ready，模型历史仍只有该次原输入；其余身份不同的新输入保留。用户再次发送相同文字也必须实际进入新一轮。
6. 开关关闭、压缩或 fresh-context：关闭不重新注入；仍存在的快照按归属使用；缺失允许退化，不强制补偿、不破坏业务恢复。

中断样本先由真实 A2A Server 产生并保存输入，杀掉进程后，通过生产 AgentLoop 的公开 continue_streaming 继续同一份会话文件，验证 query 和调度归属；不伪造 A2A 已恢复任务。真实 A2A 权限批次冷恢复在 C04 验证；SSE 样本通过实际 SubscribeToTask 重连活任务。这里只要求已有正常恢复可用，不设计跨进程 Task 复活、每个崩溃点的持久队列或辅助用量精确恢复矩阵。关键词转义、B70／B700 边界、路径安全、具体 TokenCounter 等细节以离线单元测试覆盖，不继续扩张进程用例。

## 4. 四文本模型的真实验收

真实用例使用真实 LLM＋本地 A2A Server；涉及方案／询价时使用测试账号的真实只读云 API。不得用脚本化关键词或搜索结果替代。全部停在部署之前，拒绝云写操作和未知权限的自动批准。

首批规划 4 组，四模型各跑一遍；重复次数与原 runner 自带重复次数都记录，不能用一次模型池调度声称全覆盖。已有场景的原始提问不修改来迎合检索。

| 拟定 case／复用驱动 | 输入或流程 | 核心检查 |
|---|---|---|
| `wiki-auto-normal-a2a`，扩展 normal 多轮驱动 | 新会话：“What are the architecture and VRAM specifications of Intel Arc Pro B70?”；续聊：“把刚才的解释整理成两行，不增加新内容。”；再问：“阿里云 ECS 按量计费询价时，DescribePrice 的 PriceUnit 应该怎么传？” | 首轮中文关键词保留 B70 等标识，从实际请求片段或 read_file 获得硬件原文证据；纯改写期望空关键词；新询价问题走异步并命中 ECS 询价文档；记录实际消费情况 |
| `wiki-auto-pipeline-a2a`，复用 Step1／Step2 续聊驱动 | “在杭州临时用 2 天部署 nginx 静态站点，2 核 2 GiB、40 GiB 系统盘，需要公网访问，按量付费，先给方案和费用估计，不部署。”选择真实候选进入 Step2，分别做解释型续聊；最后取消 | 两步首次和续聊调度正确，ECS／EIP 文档来源清楚；真实模板、预览、询价与 complete_step 正常；Step1 粗估不要求等于 Step2，核实口径和明显事实错误 |
| `wiki-auto-p19-a2a`，复用 P19 场景 | 保留原四轮：购买按量 TDX → SGX／TDX 质疑 → B70 是否在售 → 用户称 B70 为 Intel 显卡 | 首轮和后续异步分开记录；找到文件、实际阅读、事实使用分别判定；用成功云 API 记录核对规格，不用静态词条冒充实时库存 |
| `wiki-auto-handoff-a2a`，复用 handoff 场景 | 方案咨询 → 明确取消并交接 normal → 首次 normal 咨询 B70 → 一次自由续聊 | handoff／取消控制文字不提词；normal 首轮按已有 normal 输入事实计算，续聊异步；已结束 pipeline 的迟到工作不能覆盖 normal 查询或冒充当前步骤已读正文 |

### 模型与运行设置

沿用当前 `model_pool.py` 中的四文本模型：

```text
deepseek-v4-flash-0731
glm-5.2-fast-preview
glm-5.3-prime
deepseek-v4.1-flash
```

- 每组显式固定模型执行，主模型沿用 effort=low、fallback=false；提词使用同一分配模型的最低合法 effort／关闭思考，分别记录生效值，不修改共享主模型配置。
- `run_e2e.py` 隔离环境会过滤外部 `IAC_CODE_*`。不能只在父 shell export 就宣称启用：新增 case 在隔离服务端 env 显式设置开关，取证实际 runtime 值；C01 显式关闭。
- 现有真实 runner 已增加内部测试参数 `--wiki-auto-search` 及 auto-normal／auto-pipeline 驱动；新四个 case 显式开启，旧 case 默认行为不变。不新建另一套真实 runner 或公开工具参数。
- normal 只复制 LLM 配置；pipeline／P19／handoff 沿用测试账号只读凭据和原 awareness 设置。不修改原凭据源，不打印原始云用户数据。
- 初轮使用 `--jobs 1 --retries 0`，降低用例间并发额度干扰；主调用与 side call 仍按产品策略并行。失败保留首次证据，再按同模型定向复测，不换模型或覆盖旧目录掩盖失败。

### 真实结果判定

首轮从实际请求确认：有价值关键词的搜索结果进入主模型首次请求；无价值输入跳过。自动层只搜、未自动读正文也需证据。

后续分别报告“关键词正确”“命中文件”“结果赶上哪次请求”“实际读正文”“事实正确”。异步结果未赶上本轮，或晚到快照未保留，不单独判失败；错把旧结果当新结果、误阻塞主回复、重复原输入、破坏 complete_step 必须失败。遗漏有价值实体／纯改写无意义提词、实际事实错误仍作为各自行为问题报告，不能用异步可晚到掩盖。

## 5. CLI／Web 展示与恢复

复用现有 REPL 进程驱动和 Web API 契约入口，扩展少量 Wiki 摘要断言，不另建前端或变更 ROS：

- CLI 原会话显示首轮准备／后续后台搜索；历史重放显示已有完成摘要，不重复用户问题，不把阶段进度当助手正文。
- Web SSE 与历史 API 对同一 query 只投影一张摘要；完成记录静态重建，已无活任务的 pending 不无限转圈。
- 主回答结束后，不为晚到结果重发 MessageEnd 或恢复终态 A2A task。记录已丢失允许没有摘要，普通对话继续可用。
- Web API 契约可沿用 `--skip-browser`，但不能宣称 DOM 已验收。UI 验收再用现有本地浏览器／DOM 驱动检查摘要与实际 read_file 展示，不登录 ROS 或生产账号。

## 6. 注册顺序与最小完成标准

1. 先落确定性场景和门闩模型 fixture；实现缺失时用例应失败，不 skip／xfail，不注册空脚本或伪造成功。
2. 开发后 C01／C02／C03／C08 纳入 fast，C04–C07 纳入 full；runner 输出逐子样本和 summary。更新 `tests/scripts/test_ci_run_e2e.py` 验证目录、参数、隔离和报告，不为参数拼装另做一套重复测试。
3. 四个真实 case 纳入 live-readonly，统一注册到 `run_e2e.py`；每模型执行四组。保留旧五个知识库 case，不用新用例替换旧验收。
4. 复用 CLI／Web 驱动验证展示和历史投影，再做真实模型问题样本的同模型定向复测。

新增入口如下；真实验收须提供测试账号配置：

```bash
uv run --no-sync python scripts/ci/run_e2e.py \
  --case wiki-auto-normal-order-contract --case wiki-auto-pipeline-order-contract \
  --case wiki-auto-late-isolation-contract --case wiki-auto-recovery-contract \
  --jobs 1 --retries 0

for model in deepseek-v4-flash-0731 glm-5.2-fast-preview glm-5.3-prime deepseek-v4.1-flash; do
  uv run --no-sync python scripts/ci/run_e2e.py \
    --case wiki-auto-normal-a2a --case wiki-auto-pipeline-a2a \
    --case wiki-auto-p19-a2a --case wiki-auto-handoff-a2a \
    --text-model "$model" --credential-source-dir /path/to/test-config \
    --run-dir "/tmp/wiki-auto-$model" --jobs 1 --retries 0
done
```

私有报告保留事件、请求快照与 answers，公共报告只导出检查项、计数和必要脱敏结论。区分确定性替身与真实模型、A2A 进程链路与直接单元测试，不将搜索命中写成已读正文，也不将案例设计写成已执行通过。

## 7. 本轮验收记录

- Python 3.10–3.13 定向兼容回归各 82 项通过，末轮核心各 26 项补测通过；Python 3.14 全量 21,517 passed、172 skipped（已有条件跳过），make lint／ty 通过。本机未执行原生 Windows CI。
- fast 17 项；full 85 项。完整离线首轮发现新增测试一处未声明 UTF-8，修正后重新跑全量通过。未删除、skip 或放宽检查。
- CLI：新会话进度、退出后 --continue 静态恢复；Web：生产本地服务、实际 Chrome DOM、展开来源、服务冷重启后历史只重建一次。脚本为 run_wiki_auto_search_ui.py。
- 真实矩阵的关键发现：handoff 的旧 pipeline 输入不能算作 normal 历史，已修复并增加进程回归；GLM Prime 提词曾返回过长组合查询，改为要求简短独立词，已从 no_matches 改为命中，但 Step2 漏读正文仍在。P19 缺少规格 API 依据与实际数值错误分开报告。
- 三组历史场景使用原输入各复测 5 次：DeepSeek v4 的经济型／突发型咨询、DeepSeek v4 的 P19、GLM Prime 的 P19。复测保留原模型、low、fallback=false、awareness=true，整例分别为 4/5、0/5、1/5；残留失败不记为通过。DeepSeek v4 handoff 修复后原场景通过；GLM Fast 因原始澄清节点未交接，复测仍失败。v2 四模型 normal 首轮均读到正文，整例 3/4；DeepSeek v4.1 第三轮加载 Skill，保留 no-Skill 检查失败。
- 恢复和取消只保留小型辅助快照；未增加跨进程任务复活、终态合并、补备份或持久队列。代码保持未提交，不推送、不部署。

## 8. Runner 流程与判定口径修正（2026-11-04）

仅修改真实验收 runner 与其离线测试，业务代码不变。原四轮 P19、模型、low、fallback=false、retries=0 均保留。

- 购买首轮接受候选选择或明确 ask_user_question 澄清；实际发布候选时核验规格，不能因首轮在澄清而制造规格／数量错误。新方案可在后续原始输入后出现，先前成功的规格 API 记录可复用。
- 缺规格 API 依据继续失败，状态单列 missing_api_evidence；有真实依据但展示数值不符为 mismatch。TDX 规格族／地域／可用区独立检查，缺 API 不重复制造一项范围错误。错误规格、错误位置、缺数量仍会失败。
- handoff 取消未成功仍失败，随后 normal 咨询不执行，记为 not_exercised，而不再误报“normal 未读文档”。负向离线用例核对只发送前两轮且整体仍失败。
- normal 纯知识查询／格式整理保留无 Skill 检查；第三轮云询价允许合法加载 Skill，整个会话仍须保持 normal。sizing 检查相关 ECS Wiki 正文，指定文件是否读取只单独记录，不声称已自动证明全部 CPU 解释正确。
- Step2 的真实正文阅读要求、B70 的正文／事实要求和云只读约束保持；模型仍可能在这些项失败。
- 候选位置读取同一候选的清单、拓扑及 cost_assumptions，资源行明确写错可用区仍失败。首次 DeepSeek v4 P19 因忽略 cost_assumptions 误报位置错误，原记录保留；修正解析后另开目录重跑。

本轮真实复测使用原模型与原输入，7 组相关场景共执行 8 次（含上述位置解析修正后的 1 次补测），无整例重试。各组合最后一次结果如下；单次通过不代表稳定性已证明。

| 模型 | 场景 | 结果与实际行为 |
| --- | --- | --- |
| DeepSeek v4 | P19 | 通过；首轮澄清、第二轮发布候选，规格 API、TDX 位置及 B70 检查通过。首次运行仍缺规格 API 依据，不由位置解析修正消除。 |
| DeepSeek v4 | handoff | 通过；明确取消后实际交接 normal，随后读取 B70 正文。 |
| DeepSeek v4 | sizing | 未通过；未读 ECS Wiki 正文，转查官方资料。人工核对经济型无 CPU 积分、突发型使用积分的核心解释正确。 |
| DeepSeek v4.1 | normal | 通过；前两轮未加载 Skill，第三轮云询价合法加载 Skill。 |
| GLM Fast | handoff | 通过；本轮首轮直接出候选，再取消交接。未复现旧澄清路径的取消失败，不宣称模型取消问题已稳定修复。 |
| GLM Fast | P19 | 未通过；先澄清、第二轮出候选的合法流程已接受，但 c9i/g8i/r9i.xlarge 实际 4 核均写成 2 核；末轮未读 B70 正文、未补全显存规格。 |
| GLM Prime | P19 | 未通过；缺成功的 DescribeInstanceTypes 核验依据，未据此断言核数／内存错误；其余检查通过。 |

离线回归 121 项通过，runner 专项 15 项在 Python 3.10–3.14 均通过，ruff／ty 通过。本轮业务源码与开始时逐文件哈希一致；Step2 漏读正文未在本轮修复或重测。

## 9. 带权原文召回（2026-11-04）

- 提取版本升级为 4；每词带独立关联性系数，当前需求相关的具体 IaC 资源、规格、API、参数优先。程序按完整词过滤阿里云／Alibaba Cloud／Aliyun，不误删完整资源标识符；旧字符串关键词保持兼容。
- 中英文边界空格及大小写变体共用匹配规则和一份系数，同组重复返回取最大系数；一行同组多次出现也只计一次。按行加总不同组系数，全局选至多 10 行，同分优先正文。
- 保留 XML 标签；按绝对路径分组展示行号、匹配原文、高亮及省略标记，未展示的已知匹配行只列有限数量行号。命中片段复用 grep 结果，不再额外读取整页；参考块保持 1200 token／6144 字节上限，空命中不注入。
- 系统规则解释新格式，片段充分时可直接使用，需要完整条件时再 read_file。仅临时请求副本增加参考块；CLI／Web 的关键词字符串列表、首轮串行、后续异步、迟到隔离和恢复策略不变。
- runner 在当前轮真实主模型请求中匹配编号原文，分别记录 excerpt_pages 与实际 read_file；Step2 只统计 materialize_selected_candidate 的请求。仅有快照、路径或答案相似不算已注入。

验证结果：

- Python 3.14 全量：21,543 passed、172 原有条件 skipped；138 项相关回归通过；Python 3.10–3.13 核心各 68 项通过；ruff／ty 通过。未执行原生 Windows CI。
- scripts/ci 的 wiki-auto-request-contract 通过，覆盖正常原文注入、超预算整行舍弃、真实后续 read_file、历史原输入不被扩写和权重关键词的生产 A2A 链路。
- 固定 20 类输入，四模型各 3 轮，共 240 次真实提取及本地检索：带权格式 240/240、黑名单过滤 240/240、不需检索的输入正确跳过 60/60；需检索样本召回 179/180。完整自动词项覆盖检查 232/240；原始未通过项保留，包括复合词遗漏、外语描述未拆分，以及合法同义词／父级名称省略触发的覆盖标记，不宣称全部提取质量已稳定。

| 模型 | sizing 原问题（3 轮） | normal 原三轮 | Step1／Step2 原流程 |
| --- | --- | --- | --- |
| DeepSeek v4 | 通过 | 通过 | 通过 |
| GLM Fast | 通过 | 通过 | 通过 |
| GLM Prime | 通过 | 通过 | 通过 |
| DeepSeek v4.1 | 通过 | 通过 | 未通过；两次不同的本地只读搜索权限等待 |

真实整例首轮 11/12 通过，全部 low、fallback=false、整例 retries=0，未创建云资源。DeepSeek v4.1 首次在 Step1 解释续聊从仓库根目录发起 glob，服务停在权限等待；另开目录以原模型／原输入补测，已进入 Step2 并验证 ECS／VPC 编号原文注入，随后 grep 本地工具结果缓存目录又需权限。未批准这些请求、未扩大授权范围；Step2 的预览／最终询价完整流程仍未通过，不能用片段注入成功代替。

人工复核另记：DeepSeek v4 sizing 第三轮开头出现自相矛盾的措辞，后文表格与结论正确区分 e 无积分、t 使用积分；自动来源检查不证明全部额外数值、推荐或解释正确。当前改动不修改这些模型回答来迎合测试。所有首次失败和补测分别留存；按用户要求本地提交，未推送或部署。


## 10. Skill 直接引用共享产品目录（2026-11-04）

- `references/cloud-products` 整目录链接到 `knowledge/clouds/aliyun/products`，删除八份迁移说明；Skill 直接读取 `<产品>/<产品>.md`，归属不明时才查导航，不再经迁移页跳转。
- 安装包、sdist 和 Desktop 继续物化目录；复制后调整跨目录链接，产品专题与通用知识、模板规则均可直接读取。兼容 Windows 多层 Git 链接占位文件，不改运行时权限逻辑。
- 补充安装后单次读取正文、跨 Wiki 链接、Windows 链接链和 runner 来源识别回归；本节仅涉及目录与打包验证，不代表新增四模型真实复测。

验证：Python 3.14 相关回归 280 项通过，runner 补测 18 项通过；Python 3.10 打包／Wiki／runner 回归 56 项通过，ruff／ty 通过。实际 wheel 与 sdist 各验证 8 处产品目录、168 份正文及 304 条本地链接；从实际 wheel 在独立工作目录以 normal／Safe Mode 完成 12 次正文和关联资料读取，无额外授权。

## 11. 产品规则归回各产品 Wiki（2026-11-04）

- 七个原有章节归回 ECS、RDS、Redis、VPC、GA 各自的产品主文档，删除聚合文件及其所有业务引用；模板参数化仍按对应产品和通用模板规范执行。
- 迁移清单全部指向产品 Wiki，继续逐章节校验原文哈希；ECS 的跨 Skill 模板链接按新位置调整。产品目录整体链接方式不变。
- Python 3.14 相关回归 425 项通过；Python 3.10 的原文保留、安装包读取与 API 示例权限回归 95 项通过；ruff／ty 通过。本轮未追加真实模型调用。

## 12. 只读 knowledge:// 逻辑路径（2026-11-04）

- 共享读路径解析层将逻辑地址解析到运行时包内知识根目录，三个工具在系统路径规范化前识别此地址；原物理路径继续兼容。命名空间禁止目录穿越、控制字符和真实符号链接越界，写权限检查拒绝逻辑地址。
- Wiki 内部链接、Skill 产品引用、工具返回和自动召回标题统一使用逻辑地址；外部官方文档与 Skill 模板参考仍保持原类型。未新增工具或请求字段，权限范围不因别名扩大。
- 快照仍保存原有相对路径，原 schema 与关键词提取策略不变；读取恢复快照时按当前安装位置生成稳定来源地址，不重复原输入或检索任务。
- 新增三个工具连续复用、normal／Safe Mode、真实 rg／Python fallback、越界与旧物理路径、安装目录迁移后恢复回归；已有 A2A request contract 的 native 场景改为实际读取逻辑地址，budget 场景继续验证物理路径兼容。

验证：核心实现全量离线 21,565 passed、172 原有条件 skipped；Python 3.10–3.13 核心相关各 197 项通过，make lint 通过。模板引用收尾后另跑 395 项回归及 Python 3.10 打包／链接 41 项，均通过。scripts/ci 的 wiki-auto-request-contract 两个样本通过；从干净源码构建的实际 wheel 在 normal／Safe Mode 下完成 12 次工具调用，29 个 Wiki 文件的 46 条内部／Skill 链接均有效。跨 Skill 模板规范改用现有 references/... 根目录，删除不再需要的打包链接改写函数。

## 13. 纯 Markdown 与独立审计记录（2026-11-04）

- 29 份知识页删除 YAML frontmatter，展示标题取自一级标题，保留正文事实、适用条件和官方引用。自动召回不再解析 YAML 字段，同分仍优先正文；已有会话快照保持可读，不新增工具字段或运行时元数据索引。
- 来源路径、旧修订号和逐节校验继续使用知识目录外的 `tests/knowledge/legacy_migration.json`，原有 5 份资料的官方 URL 与核验日期并入该清单。行政记录不进入安装包知识目录，普通 grep 和自动召回不会命中它们。
- 回归覆盖标题提取、真实 rg／Python fallback 的行政字段零命中、原文章节完整性、内部链接、旧快照恢复及原输入不重复。知识库重排后的行号以当前正文为准。

验证：全量离线 21,568 passed、172 原有条件 skipped，make lint 通过；Python 3.10–3.13 知识／逻辑路径／打包相关各 84 项通过。知识与 A2A 恢复／请求用例 82 项通过。实际 wheel 的 29 份纯 Markdown、46 条内部／Skill 链接和 normal／Safe Mode 共 10 次工具调用通过，行政字段 grep 为 No matches，审计清单未打包。

scripts/ci 首次请求契约中，预算样本在较短临时路径下未真正超预算，原丢弃行断言失败；仅在测试正文中加入第二个相距较远的关键词窗口，使样本稳定超出字节预算，保留原验收断言。修正后两个请求子样本通过，Python 3.10 同一用例 2 项通过，lint 再次通过；未修改生产预算或检索逻辑。本轮未追加真实模型调用。

## 14. 模板知识不依赖 Skill 读取上下文（2026-11-05）

- ROS 模板、Terraform 模板、模板参数三篇正文集中迁入 `clouds/aliyun/products/ros/`，新增 `ros.md` 导航。ECS、Terraform 的关联文档及 Skill 引用统一使用 `knowledge://`，不再要求当前已加载某个 Skill 或拼接相对路径。
- 原 Skill 文件保留兼容链接；wheel／sdist 中将入口物化为完整正文，覆盖 Windows 多层链接占位文件。迁移前版本和正文哈希存入现有迁移清单；知识正文只修改关联路径和打包脚本定位说明。
- `tf2ros.py` 脚本仍属于 Skill；共享文档明确从 Skill 提供的目录执行。Desktop 打包时转换为现有 `iac-code-tf2ros` 命令，保留原脚本入口和其他部署协议。
- 新增无 Skill 上下文的 ECS → ROS／Terraform → 参数规范读取链路，覆盖 normal／Safe Mode。安装包、Skill 兼容入口、内部链接和正文保留校验继续执行；本轮未追加真实模型或云 API 调用。

已验证：Python 3.10–3.13 知识／路径／打包相关各 90 项通过；相关 Skill／Desktop／Wiki 回归 540 项通过，Python 3.10 的 Desktop／Skill 31 项通过。实际 wheel 与 sdist 的 33 份 Wiki、51 条内部链接及共 36 次 normal／Safe Mode 工具调用通过；scripts/ci 请求契约两个样本通过。

最终全量离线：21,574 passed、172 原有条件 skipped，make lint 与构建脚本 Ruff 检查通过。源码目录的 Windows Git 链接占位文件也纳入兼容校验，主文档与 Skill 的正常阅读路径均为直接的 knowledge:// 地址。

## 15. 统一模板文档入口（2026-11-09）

- 删除 bundled Skill 与 selling 共享目录中三篇模板文档的六个符号链接，相关引用统一使用 `knowledge://clouds/aliyun/products/ros/`。
- wheel／sdist 删除生成三篇旧入口的打包逻辑，Desktop 沿用同一资源复制流程。参数推荐流程、方案文件与产品目录入口继续按现有规则打包。
- 正文哈希、Wiki 内部链接、normal／Safe Mode、安装后读取和 Windows 目录链接占位文件校验继续执行，新增旧入口不存在的断言。

验证：Python 3.10、3.14 相关测试各 390 项通过，Ruff 与 ty 通过。实际 wheel／sdist 各含 33 份 Wiki、51 条有效内部链接，三篇文档的旧 `references/<文件名>` 入口均不存在；两个 Python 版本对两个产物共 40 次 normal／Safe Mode 的 read_file、grep、glob 调用通过。

## 16. 清理旧产品目录入口（2026-11-09）

- 删除 bundled Skill 与 selling 共享目录的 `cloud-products` 符号链接，以及 wheel／sdist 对该旧入口的物化逻辑。Desktop 校验直接检查共享 Wiki 产品页。
- 参数推荐流程、优惠券指南和方案文件仍有 Skill 引用，继续从各自的 `references/` 入口读取。源码、安装包与测试的产品知识读取统一使用 `knowledge://`。

验证：Python 3.10、3.14 相关测试各 480 项通过，Ruff 与 ty 通过。实际 wheel／sdist 均没有 `cloud-products` 目录，三篇模板文档各保留 Wiki 正文一份；33 份 Wiki、51 条内部链接及两个 Python 版本共 40 次 normal／Safe Mode 工具调用通过。源码全部 93 条 `knowledge://` 链接及剩余本地 Markdown 链接均有效。

## 17. 优惠券与预定义方案归入 Wiki（2026-11-09）

- 优惠券查询、字段和抵扣规则迁入 `clouds/aliyun/products/bss/coupons.md`，部署续聊的状态处理保留在 Step 1 Skill。动态补充提示词仅同步指南引用地址。
- iac-code Web 方案说明与 YAML 基线迁入 `clouds/aliyun/solutions/`。Skill 通过 `knowledge://` 读取基线并写入工作目录，删除旧方案链接；YAML 正文保持原样。
- 参数推荐文档、evals、动态注入机制和运行脚本保留现状。E2E runner 更新基线位置，并兼容历史证据中的旧读取地址。

验证：Python 3.10、3.14 相关测试各 1,145 项通过，Ruff 与 ty 通过；scripts/ci 的 `wiki-auto-request-contract` 两个请求样本通过。实际 wheel／sdist 各含 36 份 Wiki、55 条内部链接，优惠券指南、方案说明和 YAML 各保留一份；两个 Python 版本共 48 次 normal／Safe Mode 读、写、搜索调用通过，YAML 哈希与迁移前一致。源码全部 103 条 `knowledge://` 链接及剩余本地 Markdown 链接有效。
