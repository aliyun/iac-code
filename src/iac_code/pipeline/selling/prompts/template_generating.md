# 步骤：模板生成

你正在为候选架构方案生成阿里云 ROS 模板。

## 任务
根据候选架构方案，生成完整的 ROS 模板，包含：
- 所有必要的云资源定义
- 参数化配置（Parameters）— 库存相关属性必须参数化
- 输出值（Outputs）

「完整」以技能引用的「元素级完备性」清单为准，不是「校验通过即完整」。

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
文件写入完成后，先按「元素级完备性」清单逐项自检并补齐缺失元素，再调用 `complete_step` 提交结论。

> 注意：`template` 字段为 YAML 字符串（与磁盘文件内容一致），不是 JSON 对象。

## 注意事项
- 仅按技能要求读取引用文件；不要读取无关项目文件或记忆。
