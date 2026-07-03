# 步骤：成本预估

你正在为候选方案预估部署费用。优先通过 `ros_preview_template` 形成 Preview-Validated Pricing Parameter Set，不要使用 `ros_stack` 执行预览；PreviewStack 不是硬门禁，若完整部署参数暂时无法自动补齐，记录参数缺口后可用当前已选参数调用 `ros_estimate_template_cost` 获取费用预估。

## 模板信息
- 文件路径：`{template.file_path}`
- 地域：`{template.region}`

## ROS 模板来源
- 本步骤的模板文件路径已经确定为 `{template.file_path}`。
- 查询参数约束时调用 `ros_get_template_parameter_constraints`，必须传 `template_url = "{template.file_path}"`，可选 `parameters` 传当前已知参数字典。
- 预览模板时调用 `ros_preview_template`，必须传 `template_url = "{template.file_path}"`、`stack_name` 和 `parameters` 字典。
- 询价时调用 `ros_estimate_template_cost`，必须传 `template_url = "{template.file_path}"` 和 `parameters` 字典。
- 如果修复模板后需要校验，调用 `ros_validate_template`，必须传同一个 `template_url = "{template.file_path}"`。
- 不要调用 `aliyun_api` 的 ROS `GetTemplateParameterConstraints`、`PreviewStack`、`GetTemplateEstimateCost` 或 `ValidateTemplate` 接口；不要传 `TemplateBody`、`TemplateId` 或 `TemplateScratchId`。

## 禁止事项
- **不要**自行估算费用
- **不要**搜索定价文档
- **不要**使用 aliyun_doc_search

## 输出
API 调用完成后调用 `complete_step` 提交费用预估。

## 注意事项
- 不要读取项目文件或记忆，所需的上下文已在上方提供。
