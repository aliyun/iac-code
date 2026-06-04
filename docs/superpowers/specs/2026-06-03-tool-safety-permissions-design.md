# Tool Safety Permissions 设计

## 背景

本设计合并处理 GitHub issues [#87](https://github.com/aliyun/iac-code/issues/87)、[#88](https://github.com/aliyun/iac-code/issues/88)、[#78](https://github.com/aliyun/iac-code/issues/78)、[#89](https://github.com/aliyun/iac-code/issues/89)。

这四个 issue 都围绕工具安全边界：

- `read_file` 和 bash 读取类命令会默认 auto-allow 任意路径读取。
- bash readonly 白名单只按命令名判断，未拦截部分可执行命令或删除文件的危险参数。
- `_pip_like_base()` 匹配过宽，`_sed_inplace_edit()` 在两个模块重复。
- `read_file` 和 `web_fetch` 都先完整读入内容再截断，存在资源耗尽风险。

## 目标

- 为模型主动调用的读取工具建立统一、可解释的读取边界。
- 默认允许读取当前项目、显式允许目录、iac-code 应用 root、当前 session 运行产物目录中的普通文件。
- 对项目外且不在受信根目录中的读取返回 `ask`。
- 对明确敏感路径优先返回 `ask` 或更严格结果，避免静默读取凭据、SSH key、环境文件等内容。
- 将 bash readonly 判断从“命令名白名单”收紧为“命令名 + 安全参数子集”。
- 合并重复 helper，降低后续安全修复漏改风险。
- 对 `read_file` 和 `web_fetch` 增加流式读取与硬上限。

## 非目标

- 不禁止所有项目外读取；合法场景可以通过用户确认或 `additional_directories` 继续支持。
- 不允许整个用户配置目录 `~/.iac-code` 默认可读，因为其中包含凭据和会话内容。
- 不重构完整 permission pipeline。
- 不改变云 API、agent loop、ACP/A2A 的权限协议。
- 不修复当前全量基线中无关的 `src/iac_code/i18n/messages.pot` 缺失问题。

## 当前行为

`ReadFileTool` 继承 `Tool.check_permissions()`。由于 `ReadFileTool.is_read_only()` 返回 `True`，权限阶段会直接返回 `allow`，不会检查 `cwd`、`additional_directories` 或敏感路径。

bash 权限引擎已有路径约束，但当前只从写类命令中提取路径，例如 `cp`、`mv`、`rm`、`mkdir`、`rmdir`、`ln`、`install`。读取类命令如 `cat /etc/passwd`、`grep -R token ~/.iac-code` 不会进入路径约束，之后又会被 readonly 白名单直接 `allow`。

`find`、`fd`、`sed`、`rg`、`sort` 等命令被视为 readonly，但部分参数会执行命令、删除文件或写回内容。当前实现只识别 `sed -i`，未覆盖 issue 中列出的危险参数。

`ReadFileTool.execute()` 使用 `readlines()` 读取完整文件。`WebFetchTool.execute()` 使用 `client.get()` 后访问 `response.text`，会先完整读取并解码 HTTP body，然后才根据 `max_length` 截断。

## 读取边界设计

新增共享读取路径判断，供 `read_file` 和 bash 读取类命令使用。判断顺序如下：

1. 命中敏感路径时返回 `ask`。
2. 位于 iac-code 应用 root 时返回 `allow`。
3. 位于当前项目 `cwd` 时返回 `allow`。
4. 位于 `ToolPermissionContext.additional_directories` 时返回 `allow`。
5. 位于当前 session 明确暴露给工具的运行产物目录时返回 `allow`。
6. 其他路径返回 `ask`。

敏感路径优先级最高。即使敏感文件位于项目内或允许根目录内，也不静默允许。敏感路径包括：

- `.ssh/`
- `.env`
- `.iac-code/.credentials.yml`
- `.iac-code/.cloud-credentials.yml`
- `.aliyun/`
- `.alibabacloud/`
- `.aws/credentials`
- 现有 `SENSITIVE_PATHS` 中已经覆盖的 shell 配置文件和平台相关凭据位置

iac-code 应用 root 直接视为受信读根。editable/source 安装时，root 是仓库或源码根；普通安装时，root 是 `iac_code` 包所在的安装根。这样 bundled skills、references、脚本和开源源码资源不会因为项目外边界而被误伤。

用户配置目录不整体加入允许根。只有明确属于当前 session、且需要作为工具产物读取的目录可以加入受信根。凭据、settings、history、memory 和历史 session 文件不因位于 `~/.iac-code` 而默认可读。

## Bash readonly 参数设计

保留现有 readonly 白名单，但对存在危险参数的命令增加安全子集判断。命中危险参数时，该命令不再返回 readonly `allow`，而是进入普通 permission 流程并最终 `ask`。

危险参数规则：

- `find`: `-delete`、`-exec`、`-execdir`、`-ok`
- `fd`: `-x`、`-X`、`--exec`、`--exec=...`
- `sed`: `-i`、`--in-place`、GNU sed `e` command、`s///e` flag，以及无法明确判定为简单读取的复杂 sed script
- `rg`: `--pre`、`--pre=...`
- `sort`: `--compress-program`、`--compress-program=...`

`_pip_like_base()` 收紧为只匹配 `pip`、`pip3`、`pip3.11` 等版本化 pip 命令，不匹配 `pipx`、`pip-audit`、`pip-compile`、`pipeline-deploy`。

`_sed_inplace_edit()` 抽到共享 helper，由 readonly 分类和 permission engine 共用，避免两个模块未来修复时发生分歧。

## 读取类 bash 路径提取

为 bash read commands 增加只读路径提取。目标不是完整解释 shell，而是在现有 parser 已识别出的 simple command 上提取常见路径参数：

- `cat`、`head`、`tail`、`less`、`more`、`wc`、`file`、`stat`、`du`
- `grep`、`egrep`、`fgrep`、`rg`、`ag`、`ack`
- `find`、`fd`
- `sed`、`sort`、`uniq`、`cut`

提取逻辑应保守处理 flags：

- `--` 后的 token 视为路径。
- 已知带参数的 flag 跳过其参数。
- 明确的模式参数不当作路径，例如 `grep pattern file` 中的 `pattern`。
- 无法可靠判定时不应静默 allow 敏感绝对路径；可以返回 `ask`。

读取路径检查在 readonly auto-allow 之前运行。这样 `cat src/app.py` 可以继续 allow，而 `cat ~/.ssh/id_rsa` 会 ask。

## `read_file` 资源限制设计

`read_file` 改为流式读取，不再调用 `readlines()`。默认硬上限：

- 最大读取字节数：10 MB。
- 最大读取行数：50,000 行。

读取时按行迭代，累计字节数和行数。达到任一上限时停止读取，并在结果头部标注内容已截断。现有行为尽量保持：

- 继续支持 `start_line` 和 `end_line`。
- 继续输出行号。
- 文件不存在、权限错误、二进制或解码失败仍返回错误。
- 空文件仍返回空文件提示。

当用户请求的 line range 在大文件后半段时，工具仍然需要扫描到目标行，但不把整份文件保存在内存里。超过硬上限后仍未到达目标范围时，返回截断提示和已读取范围，不继续无界扫描。

## `web_fetch` 资源限制设计

`web_fetch` 改为 httpx streaming。默认下载 byte cap 独立于 `max_length`，避免用户传入超大 `max_length` 导致无限下载。建议默认上限为 10 MB。

流程：

1. 校验 URL。
2. 使用 `AsyncClient.stream("GET", url)` 发起请求。
3. 调用 `raise_for_status()`。
4. 按 chunk 累计原始字节。
5. 达到 byte cap 后停止读取并关闭连接。
6. 根据响应编码或 UTF-8 fallback 解码。
7. HTML 响应继续调用 `_extract_text_from_html()`。
8. 根据 `max_length` 截断最终文本，并标注下载内容已截断。

HTTP status、URL validation、HTML 清洗和 UI 渲染的现有行为保持不变。

## 组件

`src/iac_code/tools/path_safety.py` 或等价共享模块：

- 解析和规范化候选路径。
- 判断路径是否位于允许根。
- 判断路径是否命中敏感路径。
- 计算 iac-code 应用 root。
- 返回结构化 permission result 或 path decision。

`src/iac_code/tools/read_file.py`：

- 覆盖 `check_permissions()`，使用共享读取路径判断。
- 将执行逻辑改为流式读取。

`src/iac_code/tools/bash/path_validation.py`：

- 复用共享路径判断。
- 增加读取类命令路径提取。
- 在 readonly allow 前检查读取路径边界。

`src/iac_code/tools/bash/readonly_commands.py`：

- 增加危险参数检测。
- 收紧 pip-like 判断。
- 移除本地重复 sed helper。

`src/iac_code/tools/bash/permissions.py`：

- 复用共享 sed helper。
- 确保 path/safety 检查在 readonly allow 前完成。

`src/iac_code/tools/web_fetch.py`：

- 改为 streaming 下载。
- 增加 byte cap 和截断提示。

## 错误处理

读取边界命中项目外路径时返回 `ask`，message 说明路径超出允许读取目录。

读取边界命中敏感路径时返回 `ask`，message 说明路径敏感。后续如果已有 deny rule 命中，仍由既有 permission pipeline 处理。

危险参数命中时返回 `ask`，reason 类型使用明确值，例如 `dangerous_readonly_argument`，便于测试和 UI 展示。

`read_file` 超过字节或行数上限不是错误，返回成功结果并标注截断。文件系统错误和解码错误仍返回 error。

`web_fetch` 超过下载上限不是错误，返回已读取和截断后的内容并标注截断。HTTP 和网络错误沿用现有 error 路径。

## 测试计划

读取权限：

- `read_file` 读取项目内普通文件为 `allow`。
- `read_file` 读取 `additional_directories` 内普通文件为 `allow`。
- `read_file` 读取 iac-code 应用 root 内文件为 `allow`。
- `read_file` 读取项目外普通文件为 `ask`。
- `read_file` 读取 `~/.iac-code/.credentials.yml`、`~/.ssh/id_rsa`、`.env` 为 `ask`。

bash 权限：

- `cat`、`head`、`grep` 读取项目内文件仍为 `allow`。
- `cat /etc/passwd`、`grep -R token ~/.iac-code` 为 `ask`。
- `find . -delete`、`find . -exec ...` 为 `ask`。
- `fd . -x ...` 和 `fd . --exec ...` 为 `ask`。
- `sed -i`、`sed -n '1e cmd' file`、`sed 's/.*/cmd/e' file` 为 `ask`。
- `rg --pre ...` 为 `ask`。
- `sort --compress-program=...` 为 `ask`。
- 普通 `rg pattern src`、`find . -name '*.py'`、`sort file.txt` 仍为 `allow`。
- `pip list`、`pip3 list`、`pip3.11 list` 为 readonly；`pipx list`、`pipeline-deploy list` 不因 pip-like 规则 auto-allow。

资源限制：

- `read_file` 大文件不会完整读入内存，并返回截断提示。
- `read_file` line range 保持行号和范围格式。
- `web_fetch` 使用 streaming mock 时在 byte cap 后停止读取。
- `web_fetch` HTML 响应仍清洗为文本。
- `web_fetch` `max_length` 仍限制最终返回字符数。

## 验证

先运行聚焦测试：

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/tools/bash tests/tools/test_read_file.py tests/test_tools/test_web_fetch.py tests/services/permissions/test_pipeline.py -v
```

如果聚焦测试通过，再运行全量测试：

```bash
PATH="$HOME/.local/bin:$PATH" make test
```

当前全量基线已有无关失败：`src/iac_code/i18n/messages.pot` 未跟踪且缺失，导致 `tests/test_i18n.py` 中 4 个测试失败。最终实现报告需要明确区分本次相关测试结果和该基线问题。
