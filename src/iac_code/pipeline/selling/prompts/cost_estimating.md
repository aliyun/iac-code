# 步骤：成本预估

你正在为候选方案预估部署费用。使用 ROS `GetTemplateEstimateCost` API 获取费用预估。

## 模板信息
- 文件路径：`{template.file_path}`
- 地域：`{template.region}`

## 禁止事项
- **不要**自行估算费用
- **不要**搜索定价文档
- **不要**使用 aliyun_doc_search

## 输出
API 调用完成后调用 `complete_step` 提交费用预估。

补充说明：
- `cost` 字段为字符串，包含金额和计费周期（如 "¥800/月"）
- 若修复了模板，设置 `template_fixed: true` 并在 `fix_summary` 中说明
- 询价失败时 `monthly_estimate` 填 "询价失败"，`error` 说明原因

## 注意事项
- 不要读取项目文件或记忆，所需的上下文已在上方提供。
