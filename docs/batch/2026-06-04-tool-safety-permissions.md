# Tool Safety Permissions 合入说明

日期：2026-06-04

目标分支：`fix_issue_260603`

来源分支：`codex/tool-safety-permissions`

来源提交：`3295b04 fix: harden tool safety permissions`

目标提交：`28d7525 fix: harden tool safety permissions`

关联 issue：

- https://github.com/aliyun/iac-code/issues/78
- https://github.com/aliyun/iac-code/issues/87
- https://github.com/aliyun/iac-code/issues/88
- https://github.com/aliyun/iac-code/issues/89

## 背景

本次合入集中加固 agent tool permission 的路径安全边界，目标是在默认权限模式下避免只读工具、bash 只读命令、web fetch、以及宽泛 allow rule 绕过用户确认读取或写入敏感路径和项目根外路径。

合入前的主要风险包括：

- `read_file`、`list_files`、`glob`、`grep` 等只读工具会因为 `is_read_only()` 被自动允许，未统一检查 `cwd`、`additional_directories` 和敏感路径。
- bash 只读命令如 `cat /etc/passwd`、`grep -R token ~/.iac-code`、`fd --base-directory /etc` 等可能因为只读白名单或宽泛 allow rule 被静默允许。
- `write_file`、`edit_file` 等写工具可以被 bare allow rule 静默允许写入项目根外路径。
- `glob` 和 `grep` fallback 存在 symlink 逃逸风险。
- `web_fetch` 在截断前可能加载过大的响应体。

## 本次做了什么

### 共享路径安全判断

- 新增 `src/iac_code/tools/path_safety.py`，集中实现读路径和写路径判断。
- 读路径默认允许：
  - 当前项目 `cwd`。
  - 配置里的 `permissions.additional_directories`。
  - iac-code 自身源码/安装 root。
  - 当前 session 的可信读取目录。
- 写路径默认允许：
  - 当前项目 `cwd`。
  - 配置里的 `permissions.additional_directories`。
- 敏感路径会返回 `safety_check` ask，例如 `.ssh`、`.env`、`.iac-code` credential 文件等。
- macOS 和 Windows 下的敏感路径匹配按大小写不敏感处理。

### session 可信读取目录

- 新增 `trusted_read_directories` 到 `ToolPermissionContext`。
- 当前 session 的 `tool-results/<session_id>` 和 `image-cache/<session_id>` 会自动加入可信读取目录。
- session id 会做路径形状校验，避免构造出任意目录。

### 只读工具加固

- `read_file` 使用共享读路径检查，并将大文件读取改为流式 capped read。
- `list_files`、`glob`、`grep` 在自动 allow 前执行共享读路径检查。
- `glob` 额外拒绝绝对 pattern 和包含 `..` 的 pattern，并检查每个匹配结果的 resolved path，避免通过 symlink 逃逸到 allowed root 之外。
- `grep` Python fallback 会跳过 resolved path 不在 search root 下的 symlink 文件，避免隐藏 `rg` 后读到根外文件。

### 写工具加固

- `write_file`、`edit_file` 在工具级 allow rule 生效前执行共享写路径检查。
- bare allow rule 只表示允许该工具类型，不再表示允许写任意文件系统路径。
- 用户如需静默操作项目外目录，应显式配置 `permissions.additional_directories`。

### bash 权限加固

- 新增 bash argv read-path extraction，覆盖 `cat`、`head`、`tail`、`grep`、`rg`、`fd`、`find`、`ls`、`jq`、`diff` 等常见只读命令。
- 对危险只读参数返回 sticky ask，例如：
  - `find -delete`。
  - `fd --exec` / `fd -x` / `fd -X`。
  - `sed -i`、`sed -e .../e`。
  - `rg --pre`。
  - `sort --output`。
- 对 `cp`、`mv`、`ln`、`install` 识别 path-bearing `--target-directory` / `-t` 参数，包括 separated、attached 和 clustered short option 形式，例如 `cp -pt/etc file.txt`。
- 对 bash redirects 也检查读/写路径，避免 `cat < /etc/passwd` 等绕过。
- compound command 中出现 `cd` 后的相对读路径会要求确认，避免基于旧 cwd 做错误判断。

### 权限 pipeline 语义

- `safety_check`、`path_constraint`、危险只读参数、复杂命令、parse error 等 ask 作为 sticky ask，不会被 broad allow rule 覆盖。
- `bypass_permissions` 仍保持全放行语义。这是用户显式选择关闭权限询问的模式，不强制 sticky ask。
- bare ask rule 会强制 auto-allowed 工具弹出确认，例如 `read_file` 和 `web_fetch`。

### web_fetch 响应上限

- `web_fetch` 改为 streaming read，并在读取时执行 hard cap。
- 保留文本截断上限，避免先加载完整大响应再截断。

## 配置语义

工具 allow 和路径 allow 分离：

```yaml
permissions:
  mode: default
  additional_directories:
    - /tmp/iac-code-perm-manual/outside
  allow:
    - "bash(cp:*)"
    - "bash(mv:*)"
    - "grep"
    - "glob"
```

上面的配置表示：

- `additional_directories` 授权 `/tmp/iac-code-perm-manual/outside` 作为可访问路径根。
- `allow` 授权对应工具或命令形态。
- 两者同时满足时，默认模式下才会静默执行项目外路径操作。

`bash(cp:*)` 这类 broad allow rule 不会单独授予整个文件系统访问权。

## 冲突处理

cherry-pick `3295b04` 到 `fix_issue_260603` 时，唯一冲突出现在：

- `src/iac_code/tools/grep.py`

冲突原因是目标分支已经包含 issue #83 的 `grep` 修复：

- ripgrep path glob 语义。
- Python fallback 的 path-aware glob matching。
- ripgrep 输出路径 absolutize。

本次解析保留了目标分支的 glob/rg 行为，同时叠加 tool-safety 分支的：

- `check_read_path` permission 检查。
- Python fallback symlink resolved path 过滤。

冲突解决后已运行：

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/test_tools/test_grep.py tests/tools/test_grep.py -q
```

结果：`45 passed`。

## 翻译处理

本次新增了少量 `_()` 包裹的权限提示字符串，因此合入后需要运行：

```bash
PATH="$HOME/.local/bin:$PATH" make translate
```

如果后续合并时 `src/iac_code/i18n/locales/*/LC_MESSAGES/messages.po` 出现冲突，应保留双方新增 msgid/msgstr，再运行 `make translate` 重新生成 `messages.pot` 和各语言 `messages.po` / `messages.mo`。

检查重点：

- 双方新增 msgid 没有丢失。
- 已有翻译没有被覆盖成空值。
- Babel 更新的行号和 `POT-Creation-Date` 等元数据变化符合预期。

## 兼容性判断

### CLI 和配置

- 没有新增 CLI 参数。
- 既有 `permissions.additional_directories` 字段继续使用，语义变得更一致：bash、读工具和写工具都会尊重它。
- `--permission-mode bypass_permissions` 保持全放行，不会因为 path constraint 强制 ask。
- `--allowed-tools` 和配置里的 allow rule 仍可用于工具/命令授权，但不再隐式授权任意路径。

### Tool schema

- `read_file`、`list_files`、`glob`、`grep`、`write_file`、`edit_file` 的输入 schema 保持兼容。
- `ToolPermissionContext` 新增 `trusted_read_directories` 字段，带默认空列表；现有构造调用不需要修改。

### 行为变化

- 默认模式下，项目根外读取或写入会 ask，除非路径在 `additional_directories`、iac-code root 或 session trusted read dirs 中。
- 敏感路径读取或写入会 ask。
- `glob` 和 `grep` 不再通过 symlink 返回或读取 allowed root 外的文件。
- 宽泛 bash allow rule 不再覆盖危险参数、复杂命令、parse error 或路径边界。
- `web_fetch` 对超大响应更早截断，减少内存风险。

### 风险

主要风险是默认模式下过去静默执行的项目外路径访问现在会弹权限确认。对于确实需要长期访问的目录，应迁移到 `permissions.additional_directories`。

## 验证

推荐合入后运行：

```bash
PATH="$HOME/.local/bin:$PATH" make translate
PATH="$HOME/.local/bin:$PATH" uv run pytest \
  tests/tools/test_list_files.py \
  tests/test_tools/test_glob.py \
  tests/test_tools/test_grep.py \
  tests/tools/test_write_file.py \
  tests/tools/test_edit_file.py \
  tests/tools/test_read_file.py \
  tests/tools/test_path_safety.py \
  tests/tools/bash \
  tests/services/permissions \
  tests/test_tools/test_web_fetch.py \
  tests/agent/test_permission_scenarios.py \
  -q
PATH="$HOME/.local/bin:$PATH" make lint
PATH="$HOME/.local/bin:$PATH" make test
```

手动交互验证建议覆盖：

- default 模式下 `read_file` / `list_files` / `glob` / `grep` 访问项目根外路径应 ask。
- 配置 `additional_directories` 后，对应外部目录可被静默访问。
- `bash(cp:*)` 不能单独绕过项目根外写入限制；加上 `additional_directories` 后才可静默执行。
- `glob` 通过 symlink 指向根外目录时应 ask 或过滤，不应静默泄露根外文件。
- 隐藏 `rg` 后，`grep` Python fallback 不应通过 symlink 文件读到根外内容。
- `bypass_permissions` 下仍直接执行，不弹 ask。
