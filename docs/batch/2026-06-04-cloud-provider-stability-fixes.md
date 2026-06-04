# Cloud Provider Stability Fixes 合入说明

日期：2026-06-04

目标分支：`fix_issue_260603`

来源分支：`codex/cloud-provider-stability`

关联 issue：

- https://github.com/aliyun/iac-code/issues/77
- https://github.com/aliyun/iac-code/issues/81

## 背景

本次合入用于修复 ROS stack 操作结果判断、部署 telemetry 时机、异步事件循环阻塞，以及 Aliyun 凭证/OAuth 资源生命周期相关稳定性问题。

issue #77 描述了三个 ROS stack 问题：

- `StackStatus.is_success` 使用 `endswith("_COMPLETE")`，导致 rollback 和 delete complete 被误判为部署成功。
- `CreateStack` / `UpdateStack` API 只表示异步操作已被接收，但旧逻辑立即发送 `DEPLOYMENT_SUCCEEDED`，会高估成功率。
- ROS SDK 是同步调用，旧代码在 async 方法里直接调用 SDK，可能阻塞事件循环和 UI 更新。

issue #81 描述了两个 Aliyun 凭证/OAuth 问题：

- aliyun CLI config 中任意 profile 缺少 `name` 时，`_load_from_aliyun_cli` 会抛出 `KeyError`，导致凭证加载链路崩溃。
- `AliyunOAuthClient` 内部创建的 `httpx.Client` 没有关闭路径，browser login 和 OAuth refresh 会泄漏连接。

## 本次做了什么

### ROS stack 状态和结果判断

- 将 `StackStatus.is_success` 改为明确成功状态白名单，只包含 `CREATE_COMPLETE`、`UPDATE_COMPLETE`、`IMPORT_CREATE_COMPLETE`、`IMPORT_UPDATE_COMPLETE` 和 `CHECK_COMPLETE`。
- 保留 `DELETE_COMPLETE` 作为 `DeleteStack` 操作自身的成功终态，但不把它作为通用部署成功状态。
- 在 `BaseCloudStack` 中加入 action-aware 的 terminal/success hook，使不同 stack action 可以独立定义终态和成功语义。
- 为 ROS create、continue-create、update、delete 分别定义终态集合，避免旧 action 的 stale status 被当前 action 误消费。
- 当 terminal status 已经出现但资源列表查询失败时，允许返回空资源列表继续完成终态判断，避免 delete 等场景被资源查询错误误报失败。

### ROS 部署 telemetry 时机

- `CreateStack` / `UpdateStack` API 接收后只记录 deployment context，不再立即发送 `DEPLOYMENT_SUCCEEDED`。
- `DEPLOYMENT_SUCCEEDED` / `DEPLOYMENT_FAILED` 改到 polling 看到 action terminal status 后发送。
- terminal telemetry 按 `(stack_id, action)` 隔离上下文，避免 create/update/delete 的上下文串用。
- polling 查询失败和取消时会清理 telemetry context，取消会发送 `DEPLOYMENT_CANCELLED` 并继续向外传播 cancellation。
- telemetry 准备和发送均改为 best-effort，避免 telemetry 自身异常影响 ROS API 调用结果。
- `DeleteStack` 不发送 deployment success telemetry，只作为 stack action 结果返回成功或失败。

### ROS SDK 异步兼容性

- 将 `create_stack`、`update_stack`、`continue_create_stack`、`delete_stack`、`get_stack`、`list_stack_resources` 等同步 SDK 调用放入 `asyncio.to_thread()`。
- 保持现有 async tool 接口不变，避免阻塞事件循环。

### Aliyun CLI 凭证加载

- `_load_from_aliyun_cli` 现在会先校验 config JSON 顶层结构和 `profiles` 类型。
- 只接受 dict 且 `name` 为 str 的 profile；缺少 `name`、非 dict、`None` 等坏数据会被跳过。
- 对 `sts_expiration`、`oauth_access_token_expire`、`oauth_refresh_token_expire` 做整数解析保护，坏值会返回 `None`，不会让凭证加载链路崩溃。

### OAuth client 生命周期

- `AliyunOAuthClient` 增加 `close()` 和 context manager 支持。
- 只有 client 自己创建的内部 `httpx.Client` 会被关闭；外部注入的 `http_client` 不会被误关闭。
- `run_browser_oauth_flow` 在 success、error、cancel、callback server 创建失败等路径都会关闭内部创建的 OAuth client，并关闭 callback server。
- `refresh_oauth_if_needed` 会关闭内部创建的 OAuth client；外部注入的 OAuth client 保持由调用方管理。
- `/auth` browser OAuth flow 中显式创建的 client 会在 `finally` 中关闭。

## 测试覆盖

新增和增强的测试覆盖了：

- rollback complete、delete complete、import rollback complete 不再被 `StackStatus.is_success` 视为通用成功。
- create/update 的 success telemetry 只在 polling 看到终态成功后发送。
- rollback terminal status 返回 tool error 并发送 failure telemetry。
- delete complete 作为 `DeleteStack` 操作成功返回，但不发送 deployment success telemetry。
- action terminal/success hook、terminal resource fallback、polling cancellation cleanup。
- create/update/continue-create/delete/status/resources 全部通过 `asyncio.to_thread()` offload。
- malformed aliyun CLI profile 不会触发 `KeyError`，坏数字字段不会崩溃。
- OAuth browser flow、refresh flow、auth flow 对内部 client 的关闭路径。
- falsey injected OAuth client / HTTP client 不会被误判为未注入，也不会被误关闭。

## 翻译处理

本次主要修改业务逻辑、测试和 Markdown 文档，没有新增需要 `_()` 提取的用户可翻译字符串。

cherry-pick 时如果出现 `src/iac_code/i18n/locales/*/LC_MESSAGES/messages.po` 冲突，应保留双方新增词条，再运行：

```bash
PATH="$HOME/.local/bin:$PATH" make translate
```

运行后需要检查：

- 双方新增 msgid/msgstr 没有丢失。
- 现有中文、英文或其他语言翻译没有被覆盖成空值。
- 只由 Babel 更新的行号、`POT-Creation-Date` 等元数据变化符合预期。

## 兼容性判断

### 公共 API

CLI 入口、tool schema、provider credential public methods、OAuth public helpers 的签名保持不变。现有调用方不需要调整参数或返回值处理。

### 行为变化

- rollback complete 不再显示为部署成功。这是 issue #77 的目标修复。
- `DeleteStack` 仍可以在 `DELETE_COMPLETE` 后返回 action success，但不会污染部署成功 telemetry。
- deployment success telemetry 从 API 接收时机延后到 polling 确认终态成功后。这会改变 telemetry 时间点和成功率统计，是预期修复。
- 同步 ROS SDK 调用被放到线程中执行，对调用方接口透明，但可以减少 event loop 阻塞。
- malformed aliyun CLI config 不再中断启动流程，而是跳过坏 profile 或返回无 CLI 凭证。
- OAuth client 关闭策略遵循 ownership：内部创建则关闭，外部注入则不关闭。

### 依赖和数据结构

没有新增依赖，没有数据库或配置 schema 迁移，没有更改用户凭证文件格式。

### 风险

主要风险集中在 ROS terminal status 覆盖是否与云端状态枚举完全一致。本次已覆盖 create、continue-create、update、delete、import create/update 及 rollback 相关终态；如果后续 ROS SDK 增加新终态，需要同步更新 action terminal status 集合和测试。

## 验证建议

合入后推荐运行：

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/tools/cloud/test_types.py tests/tools/cloud/test_base_stack.py tests/tools/cloud/aliyun/test_ros_stack.py tests/services/providers/test_aliyun.py tests/services/providers/test_aliyun_oauth.py tests/commands/test_auth_flows.py -v
PATH="$HOME/.local/bin:$PATH" make lint
PATH="$HOME/.local/bin:$PATH" make test
```

手动交互验证建议覆盖：

- 创建一个会 rollback 的 ROS stack，确认最终 tool result 是 error，不是 success。
- 创建或更新一个正常完成的 ROS stack，确认成功只在 polling 看到 complete 后返回。
- 删除 ROS stack，确认 `DELETE_COMPLETE` 作为 delete action 成功返回，但不会记录 deployment success。
- 在交互过程中发起 ROS stack 操作，确认等待状态下 UI/event queue 不被 SDK 调用长时间阻塞。
- 准备一个缺少 profile `name` 的 aliyun CLI config，确认 `iac-code` 启动和凭证加载不会崩溃。
- 走一次 OAuth browser login 或 refresh，确认流程结束后内部 HTTP client 被关闭。
