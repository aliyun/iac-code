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
按顺序完成：

1. 写文件：把最终模板写入 `{candidate.output_path}`。
2. 校验：对同一路径调用 `ros_validate_template`；若校验后又修改了模板，必须重新校验。
3. 提交结论：`template` 取自**最终落盘文件**的内容，`template_sha256` 为该字符串的 sha256，`file_path` 为 `{candidate.output_path}`。
4. `description` 及结论中出现的实例规格（ECS 的 vCPU/内存、RDS 规格等）必须逐一对照最终模板的 Parameters 默认值或资源属性重新确认，不得沿用生成过程中被推翻的中间设想。

> 注意：`template` 字段为 YAML 字符串（与磁盘文件内容一致），不是 JSON 对象。
> 下游成本步骤读取真实模板文件，结论与模板不一致会让同一候选出现互相矛盾的规格。

## 注意事项
- 仅按技能要求读取引用文件；不要读取无关项目文件或记忆。
