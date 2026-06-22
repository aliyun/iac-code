# 步骤：成本预估

你正在为候选方案预估部署费用。优先通过 `aliyun_api(product="ros", action="PreviewStack")` 形成 Preview-Validated Pricing Parameter Set，不要使用 `ros_stack` 执行 `PreviewStack`；PreviewStack 不是硬门禁，若完整部署参数暂时无法自动补齐，记录参数缺口后可用当前已选参数调用 ROS `GetTemplateEstimateCost` API 获取费用预估。

## 模板信息
- 文件路径：`{template.file_path}`
- 地域：`{template.region}`

## 禁止事项
- **不要**自行估算费用
- **不要**搜索定价文档
- **不要**使用 aliyun_doc_search

## 输出
API 调用完成后调用 `complete_step` 提交费用预估。

## 注意事项
- 不要读取项目文件或记忆，所需的上下文已在上方提供。
