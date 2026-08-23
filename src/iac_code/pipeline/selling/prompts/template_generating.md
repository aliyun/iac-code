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

> 注意：`template` 字段为 YAML 字符串（与磁盘文件内容一致），不是 JSON 对象。

### `template` 字段格式约束
`template` 必须是可直接结构化解析的裸 IaC 文本，与磁盘文件内容逐字一致：
- 禁止使用 markdown 代码围栏（```` ``` ````、```` ```yaml ````）包裹模板；
- 禁止在模板前后输出说明性段落、结论或非模板注释；
- 模板正文以 ROS 模板的首个键（如 `ROSTemplateFormatVersion`）开始，以模板最后一行结束。

模板自身需要的 YAML 注释（`#`）属于模板内容，可以保留。

## 注意事项
- 仅按技能要求读取引用文件；不要读取无关项目文件或记忆。
