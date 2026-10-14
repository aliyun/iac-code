# 调度可靠性验证

离线回归用于验证底座，不是实际云测试、截图验收或真实模型四并发的证据。
2026-10-09 的修复不重新创建云资源，不重放已通过项；旧回执、所有权、额度、wire 和失败均保留。

## 已实现的程序行为

| 原问题 | 程序行为 | 离线覆盖 |
| --- | --- | --- |
| 执行遗漏也排空全部 GLM | 验收 execution_gap → input 修复；product_gap/fail/旧未分类 → product 屏障 | 范围来自验证后的 receipt，不依赖 prompt/reason |
| 修输入时可能越权改源码 | input 永不允许 repair_file；escalate_repair 结束当前回合，再等待精确终态及 GLM 排空 | 并行修输入、拒绝写入、升级回合不能继续 approve |
| 排空时 Sol 空等 | 待产品修复排空时允许只读准备其他任务，仍禁止派发新 GLM | 在跑 GLM + queued product repair + prepare-ahead |
| wait 无具体交接，单项阻塞拖住整体 | status/next 返回 progress、精确 active_jobs、waiting_on、稳定 attention id 与原提案路径/SHA | 重启、无关事件、未知提交、首波不足、其他任务继续 |
| 派前失败反复选择同一坏槽 | 未预留 job 前封存 action、持久隔离精确空闲槽；OPS 原槽 fresh preflight 后 slot-recover | 旧 action 不重复、其他槽补位、预检失败不能解闸、首波仍需四槽 |
| OPS 审完又从头准备 | block 的交接持久化到 SQLite；下轮同任务 context.ops_handoff 保留原证 | 原提案 SHA 不变；未终态拒绝 retry-blocked |
| complete 标签掩盖残留 | finished/can_finalize 均要求没有未终态 job、未清理 owner、占用槽或未消费 action | 残留 owner/槽不允许收口 |
| 新结果页被缓存旧代码覆盖 | 结果生成器加入 import-time 加载 SHA 核验 | 磁盘/加载不符要求重载原 Server |
| 滚动补位或重启重复派发 | 单 issued action、事务重核、原任务槽与精确终态仍为唯一依据 | 六资源四槽、两份提前准备、独立验收后第五/第六补位、同 SQLite 恢复 |

纯输入修复仍须原授权/累计调用/完整模板/原证 SHA 通过 approve；上述改动没有自动批准创建、
续额度、删除、不支持判定或 PASS。未知提交始终保持原 job/thread/turn，不能凭等待时间重派。
GLM 无资源回合截止时间；本轮没有加定时任务，也没有新建 Luna/Sol。

## 离线运行

```sh
uv run pytest -q tests/web/test_resource_link_workflow*.py
node --test tests/web/resource_link_workflow*.test.mjs
uv run ruff check scripts/web/e2e/resource_link_workflow tests/web/test_resource_link_workflow*.py
```

调度故障注入入口：`tests/web/test_resource_link_workflow_scheduling.py`。
模拟器使用真实 SQLite、action/capability/receipt 状态机与合成 PNG，不连接模型或云。
其他离线文件分别核验签名来源、授权、代次、清理、费用、阶段及图像证据约束。

## 仍须真实运行验证

| 要求 | 验证方式 |
| --- | --- |
| 真实四 GLM 重叠及第五/六自动补位 | 精确后端 turn 起止时间、四 origin/target、自建产品会话；配置四槽不算 |
| Luna Goal 连续推进 | 实际 Goal 状态与 SQLite action/job/event，不能把对话承诺当 active |
| 故障时无需重复准备/人工补丁 | 原交接 → OPS 审查 → 同 Sol 复核 → 缺项执行，保留恢复次数和原因 |
| 云资源验收及清理 | 唯一 Sol 看原截图并核验同源原始响应；PendingDelete 只按具体人类授权收口 |

此前真实六资源已经收口，但经历过长时间阻塞；不能用本轮离线通过把那次运行改称流畅。
再次真实验证必须来自用户批准的后续资源计划，保留全部既有 PASS，只执行其实际缺项。
