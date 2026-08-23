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

## 输出
文件写入完成后调用 `complete_step` 提交结论。

结论必须携带模板本身，不能只描述已完成的工作：
- `template`：非空，与 `{candidate.output_path}` 文件的最终内容逐字节一致
- `template_sha256`：`template` 内容按 UTF-8 编码的 sha256 十六进制摘要
- `file_path`：实际写入并已通过 `ros_validate_template` 校验的路径

> 注意：`template` 字段为 YAML 字符串（与磁盘文件内容一致），不是 JSON 对象。

## 注意事项
- 仅按技能要求读取引用文件；不要读取无关项目文件或记忆。
