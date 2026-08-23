# 步骤：模板生成

你正在为候选架构方案生成阿里云 ROS 模板。

## 任务
根据候选架构方案，生成完整的 ROS 模板，包含：
- 所有必要的云资源定义
- 参数化配置（Parameters）— 库存相关属性必须参数化
- 输出值（Outputs）

## 候选架构方案
```json
{candidate}
```

## 文件写入
1. 直接调用 `write_file` 将生成的模板以 **YAML 格式**写入 `{candidate.output_path}`（相对于工作目录）；`write_file` 会自动创建父目录，无需提前创建目录。
2. **必须先写文件，再调用 `complete_step`。**

## ROS 模板来源
生成后的模板文件路径就是 `{candidate.output_path}`。调用 `ros_validate_template` 校验时，必须传 `template_url = "{candidate.output_path}"`。不要调用 `aliyun_api` 的 ROS `ValidateTemplate` 接口，不要传 `TemplateBody`、`TemplateId` 或 `TemplateScratchId`。

## 校验与失败恢复
1. 写入模板文件后，必须调用 `ros_validate_template(template_url="{candidate.output_path}")` 校验。
2. 校验失败或 `ros_validate_template` 返回工具错误时，不要退出、不要跳过本候选、不要在没有可用模板的情况下 `complete_step`。必须：分析报错 → 就地修复 `{candidate.output_path}` 原文件（必要时用 `aliyun_api(product="ros", action="GetResourceType")` 查资源 Schema）→ 传同一个 `template_url` 重新调用 `ros_validate_template` 重试。
3. 单个候选最多重试 5 轮。在校验通过或达到 5 轮上限之前，禁止结束本步骤。
4. 达到 5 轮上限仍未通过时，也不得让本候选产出空模板：必须把当前最佳模板写入 `{candidate.output_path}`，再调用 `complete_step` 提交**非空** `template`（与磁盘文件一致），并在 `description` 中显式标注仍未通过校验的原因与最后一次报错摘要。绝不能因为工具失败而跳过候选或返回空 `template`。

## 输出
文件写入并完成上述校验/恢复后调用 `complete_step` 提交结论。`template` 字段必须为**非空** YAML 字符串（与磁盘文件内容一致），不是 JSON 对象，也不允许为空。

## 注意事项
- 仅按技能要求读取引用文件；不要读取无关项目文件或记忆。
