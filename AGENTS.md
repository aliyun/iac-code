# AGENTS.md

本文件适用于整个仓库。

## 项目概览

- `iac-code` 是一个 Python 3.12+ 的基础设施即代码助手，主要面向阿里云 ROS / Terraform 模板生成与管理。
- 源码采用 `src/` 布局，主包位于 `src/iac_code/`，测试位于 `tests/`。
- CLI 入口在 `pyproject.toml` 中声明为 `iac-code = "iac_code.cli.main:app"`。

## 常用命令

- 安装依赖与 hooks：`make install`
- 运行测试：`make test`
- 运行覆盖率：`make coverage`
- 运行 lint/type check：`make lint`
- 格式化：`make format`
- 更新翻译文件：`make translate`
- 本地启动 CLI：`make run`
- Debug 模式启动 CLI：`make dev`

优先使用 `uv` 和 `Makefile` 中已有目标。新增依赖时更新 `pyproject.toml` 和 `uv.lock`，不要绕过项目的依赖管理方式。

## 代码规范

- 目标版本是 Python 3.12，使用现代类型标注与标准库能力。
- Ruff 配置在 `pyproject.toml`：行宽 120，启用 `E/F/I/N/W` 规则。
- 保持改动聚焦；不要在任务之外重构、重命名或移动文件。
- 遵循现有模块边界：
  - `agent/` 放代理循环、系统提示和消息类型。
  - `commands/` 放 REPL 命令。
  - `providers/` 放 LLM provider 适配。
  - `services/` 放会话、上下文、凭证、遥测等业务服务。
  - `tools/` 放代理可调用工具。
  - `skills/` 放技能加载、渲染、发现和内置技能资源。

## 测试要求

- 对行为改动优先补充或更新 `tests/` 下的 pytest 用例。
- 测试不得依赖真实 LLM、真实阿里云账号、真实网络调用或用户本机配置。
- 涉及环境变量和凭证读取时，使用 `tmp_path`、`patch.dict` 或 mock 隔离状态。
- 改动范围较小时至少运行相关测试；共享逻辑、CLI、provider、凭证或工具执行路径改动后运行 `make test`，必要时运行 `make lint`。

## 配置与凭证

- 运行时配置目录是 `~/.iac-code/`，包含 `.credentials.yml`、`.cloud-credentials.yml`、`settings.yml` 和输入历史。
- 不要提交、打印或硬编码真实 API key、AccessKey、Secret、token、cookie 或用户配置文件内容。
- 阿里云凭证相关测试必须使用假值，并避免触发真实云 API。

## i18n 与内置技能

- 修改面向用户的可翻译文案后，检查是否需要运行 `make translate`。
- `src/iac_code/skills/bundled/iac_aliyun/` 中的 Markdown 和脚本是内置技能资源；修改时保持模板、参数说明和转换脚本之间的一致性。
- 生成的翻译、构建和覆盖率产物不要无故提交，除非当前任务明确要求。

## Git 协作

- 工作区可能已有他人改动；不要还原未由你产生的变更。
- 不要使用 `git reset --hard`、强推或其他破坏性操作，除非用户明确要求。
- 提交前确认 `git status` 只包含本次任务相关文件。
