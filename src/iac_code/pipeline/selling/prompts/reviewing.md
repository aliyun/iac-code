# 步骤：审查

你正在执行 AI 售卖流程的审查步骤：对生成的模板进行安全与最佳实践审查。

## 生成的模板（上一步结论）
```json
{template}
```

## 行为规则
- **发现 high 级问题时，必须通过 rollback_request 回溯修复**，不能只报告问题
  - 所有模板问题（语法/引用/安全组/规格配置） → rollback 到 `template_generating`
- 无 high 级问题时，正常提交 `conclusion.passed = true`

## 输出
调用 `complete_step` 提交审查结论。发现 high 级问题时必须同时填写 `rollback_request`。

## 注意事项
- 不要读取项目文件或记忆，所需的上下文已在上方提供。
