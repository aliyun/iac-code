---
sidebar_position: 7
title: 安装和使用 IaC Code Skill
description: 下载并安装 IaC Code Skill，让外部 Agent 获得阿里云基础设施管理能力。
---

# 安装和使用 IaC Code Skill

IaC Code Skill 面向支持 Skill 的外部 Agent。安装后，宿主 Agent 可以把云架构规划、ROS 或
Terraform 模板生成与审查、成本估算、资源选择、资源栈操作和部署等任务委派给 IaC Code。
Skill 会通过纯 Python 标准库桥接脚本启动本地认证的 A2A Runtime；不需要通过 pip 安装
IaC Code，也不应改用 Headless 命令。

## 下载 Skill

### 最新稳定版

直接下载最新稳定版：

[下载 iac-code-skill.zip](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

该固定地址始终指向已经提升到 stable 频道的 Skill 包，适合浏览器下载和手工安装。发布新
版本时地址保持不变，不需要修改下载链接。

需要获取版本号、文件大小、SHA-256 和不可变版本地址的安装器，可以读取稳定频道元数据：

[查看 latest.json](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/latest.json)

其中：

- `skillVersion` 是当前稳定版 Skill 版本。
- `skill.url` 是对应版本不可变的 ZIP 下载地址。
- `skill.sha256` 和 `skill.size` 用于校验下载文件。
- `manifest.url` 指向该版本不可变的发布清单。

自动化安装需要严格校验或可重复构建时，应先读取 `latest.json`，再下载 `skill.url` 并校验
`skill.sha256`。不要自行拼接版本地址。

## 安装 Skill

### 前提条件

- 宿主 Agent 支持由 `SKILL.md` 定义的本地 Skill。
- 已安装 CPython 3.8～3.14。macOS/Linux 使用 `python3`，Windows 优先使用 `py -3`。
- 可以访问上述 OSS 地址，以下载 Skill ZIP 和首次运行所需的 Runtime。
- 已准备模型服务配置；需要查询或管理云资源时，还需准备最小权限的阿里云身份。

正式发布的 Skill Runtime 支持以下平台：

| 操作系统 | 架构 |
|---|---|
| macOS | Apple Silicon（arm64） |
| Linux | x86_64 |
| Windows | x86_64 |

最低操作系统和 Linux glibc 版本以 Skill 固定的 Runtime manifest 为准。桥接脚本会在
下载前检查平台，平台不受支持时会直接返回错误，不会下载其他平台或 ABI 的产物。

### 解压到宿主 Agent 的 Skill 目录

下载 ZIP 后，将其直接解压到宿主 Agent 的 Skill 根目录。不同 Agent 的 Skill 根目录可能
不同，请以宿主产品文档为准。解压后的最终结构必须是：

```text
<Agent Skill 根目录>/
└── iac-code/
    ├── SKILL.md
    ├── agents/
    │   └── openai.yaml
    └── scripts/
        └── iac_code.py
```

ZIP 已经包含顶层 `iac-code/` 目录。不要再手工增加一层同名目录。安装或更新完成后，重新
启动宿主 Agent 或新建会话，让它重新发现 Skill。

### 校验安装

进入解压后的 `iac-code` 目录，在 macOS 或 Linux 上运行：

```bash
python3 scripts/iac_code.py ensure-runtime
```

Windows PowerShell 运行：

```powershell
py -3 scripts\iac_code.py ensure-runtime
```

首次运行会下载当前平台的 Runtime、校验大小和 SHA-256，并输出包含 `skillVersion`、
`runtimeTag` 和安装位置的 JSON。已经缓存且校验通过时会直接复用，不会重复下载。

## 配置模型和阿里云身份

Skill Runtime 与其他 IaC Code 运行方式使用相同的配置目录，默认为 `~/.iac-code/`。如果
已经使用 REPL、Web 或 Desktop 完成配置，Skill 可以复用这些设置；也可以通过
`IAC_CODE_CONFIG_DIR` 指向另一个配置目录。

在自动化环境中，可通过 Secret 管理方案提供以下环境变量：

| 类别 | 环境变量 | 说明 |
|---|---|---|
| 模型 | `IAC_CODE_PROVIDER` | 模型提供商 |
| 模型 | `IAC_CODE_MODEL` | 模型名称 |
| 模型 | `IAC_CODE_API_KEY` | 模型服务 API Key |
| 模型 | `IAC_CODE_BASE_URL` | 可选的兼容端点覆盖 |
| 阿里云 | `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| 阿里云 | `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey Secret |
| 阿里云 | `ALIBABA_CLOUD_SECURITY_TOKEN` | 使用 STS 时的安全令牌 |
| 阿里云 | `ALIBABA_CLOUD_REGION_ID` | 默认地域 |

不要把真实密钥写入 `SKILL.md`、宿主 Agent 提示词、项目文件或命令历史。优先使用临时凭证、
RAM Role 或 OAuth，并只授予任务实际需要的云 API 权限。完整说明参见
[LLM 提供商](../configuration/llm-providers.md)和
[阿里云凭证](../configuration/alibaba-cloud-credentials.md)。

## 首次使用

安装并配置后，在宿主 Agent 中新建会话，直接描述阿里云基础设施任务。例如：

```text
使用 iac-code 检查当前项目中的 ROS 模板，列出安全风险和修改建议，不要修改文件。
```

支持显式 Skill 语法的宿主也可以使用 `$iac-code` 指定该 Skill。宿主 Agent 应读取
`SKILL.md`，把完整请求写入工作区内的 UTF-8 文件，并通过桥接脚本创建和跟进同一个任务；
用户不需要自己启动 A2A Server。

预期流程如下：

1. 桥接脚本检查模型和阿里云配置是否就绪。
2. 首次运行时下载并校验 Skill 固定的 IaC Code Runtime。
3. Runtime 仅在 `127.0.0.1` 随机端口启动，并生成本次进程专用的 Bearer Token。
4. 宿主 Agent 展示 IaC Code 返回的进度、问题、候选方案和权限请求。
5. 任务完成后，宿主 Agent 返回最终结果和工作区内生成的文件。

## 更新和卸载

手工更新时重新下载固定地址 `skill/stable/iac-code-skill.zip`，然后完整替换宿主 Skill 目录
中的 `iac-code/`。自动更新程序可以读取 `latest.json` 比较 `skillVersion`，并通过其中的
不可变地址和 SHA-256 下载、校验新包。每个正式 Skill 都固定到经过校验的 Runtime，不能
只替换 `scripts/iac_code.py` 或手工修改其中的 Runtime URL 和摘要。

卸载时删除宿主 Agent Skill 根目录中的 `iac-code/` 即可。Runtime 缓存不会随 Skill 目录
一起删除；只有用户明确要求清理时，才执行后文的 `cache list` 和 `cache clean`。

## Runtime 缓存

首次使用下载的 Runtime 会缓存在
`<IAC_CODE_CONFIG_DIR 或 ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/`，后续调用自动复用。
普通使用无需管理该目录。需要查看占用空间或清理历史版本时，使用：

- `python3 scripts/iac_code.py cache list` — 查看已安装的 Runtime 与候选包。
- `python3 scripts/iac_code.py cache clean [--runtime-tag <tag>] [--candidates] --confirm` — 清理 Runtime 缓存或候选包；必须显式传 `--confirm`。

当前使用的 Runtime 和正在运行的进程会受到保护，不会被清理。Skill 包格式和运行约束由
源码仓库中的 `skill-runtime/skill-package-contract.json` 定义，普通用户无需操作该文件。

## 常见问题

### 提示配置不完整

Skill 会在创建任务前检查配置，但不会读取或返回密钥明文：

| 情况 | 结果 |
|---|---|
| LLM provider 或 API Key 不完整 | 返回 `llm_not_configured`，拒绝创建任务 |
| selling Pipeline 且阿里云凭证不完整 | 返回 `cloud_credentials_not_configured`，拒绝创建任务 |
| normal 模式且阿里云凭证不完整 | 可继续执行不调用云 API 的任务，但会给出预检警告 |

### 为什么执行过程中会暂停

IaC Code 在需要确认权限、补充信息或选择方案时会暂停，宿主 Agent 会直接向用户展示：

- 工具或部署权限请求（`permission`）。
- 选择题或补充信息（`ask_user_question`）。
- Pipeline 候选方案选择（`candidate_selection`）。

确认前请核对目标资源、地域、预期影响和价格。拒绝操作不会被宿主 Agent 绕过；允许单次
操作在协议中表示为 `allow_once`。

> **宿主 Agent 集成说明**
>
> 桥接结果出现 `inputRequired` 时，宿主 Agent 应展示当前请求并等待应答。
> `boundaryReached` 只表示到达一个展示或交互边界，不代表任务已经完成；宿主 Agent 应
> 展示本次更新并继续跟进同一个任务。

## 安全说明

- Runtime 只监听 `127.0.0.1` 上的随机端口，每次启动生成独立的随机 Bearer token，桥接脚本的每个请求都携带该 token。
- 桥接脚本把产物和结果限制在 job 工作区内，结果写入工作区的 `.iac-code-skill-results/`。
- 预检与权限展示字段均经脱敏处理；密钥、凭证等敏感值不会出现在展示字段中。

## 相关文档

- [A2A 协议概述](./overview.md)
- [A2A 协议参考](./protocol-reference.md)
- [LLM 提供商](../configuration/llm-providers.md)
- [阿里云凭证](../configuration/alibaba-cloud-credentials.md)
- [运行时配置](../configuration/runtime-configuration.md)
