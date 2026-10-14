# 资源链接批次报告

> 本文件是每个编号批次 worker 唯一需要填写的人工报告。复制到该批次 `OUTPUT_DIR/batch-report.md`，替换占位符；不要增加过程叙述、manifest、摘要、脱敏审计或另一份结果报告。报告上限 200 行、64 KiB；worker 最终回复最多两行。`QWEN_THREAD_ID` 为门禁兼容字段，实际填写本批 GLM 会话 ID。

TASK_ID: `<三位编号>`
QWEN_THREAD_ID: `<本批新建 GLM 会话 ID>`
CONCLUSION: `<PASS|PARTIAL|BLOCKED>`
RAW_OUTPUT_DIR: `<runner 原始输出目录>`

## 测试场景

每个实际 contract 分别填写 iframe（普通点击）和 open（Shift 点击）。格式不得改变：

第一列必须逐字使用 TASK 列出的派发资源类型：Terraform route 使用 `alicloud_*` sourceType；只有 TASK 本身列为 `ALIYUN::*` 时才使用 canonical 类型。不得将 `alicloud_*` 自动换成对应 `ALIYUN::*`，否则 guard 会拒收。以下第一列为占位符，不是要求填写 canonical 类型。

- [x] `<TASK 精确资源类型>` | `contract-id` | iframe | PASS | `<真实来源/样本>` | `<实际落点>`
- [x] `<TASK 精确资源类型>` | `contract-id` | open | PASS | `<真实来源/样本>` | `<实际落点>`
- [ ] `<TASK 精确资源类型>` | `contract-id` | iframe | FAIL | `<真实来源/样本>` | `<实际失败原因>`
- [ ] `<TASK 精确资源类型>` | `contract-id` | open | SKIP | `<无样本或不可回放原因>` | `<未执行>`

勾选规则：

- `[x]`：只用于真实执行且 PASS。
- `[ ] ... FAIL`：已真实执行但失败。
- `[ ] ... SKIP`：没有执行，必须写明无样本、不可回放或全局阻塞原因；它不计入已测、PASS 或 FAIL。
- 来源只检查了部分相关地域时，SKIP 原因必须列出实际覆盖范围，不得写“全地域无样本”。
- 不得把“打开了某个控制台页面”写成 PASS；必须核对登录态与目标资源/页面落点。

## 待修项

- `<无，或逐条写 contract-id、现象和建议修复归类>`

## 汇总

- PASS: `<场景数>`
- FAIL: `<场景数>`
- SKIP: `<场景数>`
- 全局阻塞: `<无，或唯一阻塞>`
