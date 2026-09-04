---
sidebar_position: 2
title: 安装和使用 IaC Code Skill
description: 将 IaC Code 添加到支持 Skill 的 Agent，并用它管理阿里云资源。
---

# 安装和使用 IaC Code Skill

IaC Code Skill 可以让兼容的 Agent 将阿里云基础设施任务交给 IaC Code。它支持规划云架构、生成或评审
ROS 与 Terraform 模板、估算费用、选择已有资源、操作 ROS 资源栈和部署资源。安装包自带经过校验的
IaC Code Runtime，不需要单独安装 IaC Code。

## 下载

[下载最新版 iac-code-skill.zip](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

这个固定地址始终指向最新稳定版 Skill。自动安装程序可以读取
[latest.json](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/latest.json)，
获取当前版本、不可变下载地址、文件大小和 SHA-256 摘要。如果需要可复现安装，请下载其中的
`skill.url` 并校验 `skill.sha256`。

## 安装

安装前请确认：

- Agent 支持通过 `SKILL.md` 定义的本地 Skill。
- 已安装 CPython 3.8～3.14。macOS 或 Linux 使用 `python3`，Windows 使用 `py -3`。
- 首次使用时环境能够访问下载地址。

官方 Runtime 支持 Apple 芯片的 macOS、Linux x86_64 和 Windows x86_64。下载前会检查操作系统和
ABI 要求。

把 ZIP 解压到 Agent 文档指定的 Skill 目录。压缩包中已经包含顶层 `iac-code/` 目录，最终结构应为：

```text
<Agent Skill 根目录>/
└── iac-code/
    ├── SKILL.md
    ├── agents/
    │   └── openai.yaml
    └── scripts/
        └── iac_code.py
```

常见宿主的安装位置：

- **Codex**：所有项目使用 `~/.agents/skills/iac-code/`，单个仓库使用
  `<仓库>/.agents/skills/iac-code/`。详见 [Codex Skills 文档](https://developers.openai.com/codex/skills#where-codex-loads-local-skills)。
- **Claude Code**：所有项目使用 `~/.claude/skills/iac-code/`，单个仓库使用
  `<仓库>/.claude/skills/iac-code/`。详见 [Claude Code Skills 文档](https://code.claude.com/docs/en/skills#where-skills-live)。

安装后重启 Agent 或新建会话。也可以在解压后的 `iac-code` 目录提前验证 Runtime。

macOS 或 Linux：

```bash
python3 scripts/iac_code.py ensure-runtime
```

Windows PowerShell：

```powershell
py -3 scripts\iac_code.py ensure-runtime
```

首次使用时，桥接脚本会下载当前平台对应的 Runtime，并校验文件大小和 SHA-256 摘要。后续任务会复用
本地已验证的副本。

## 配置模型和阿里云身份

Skill 默认使用 IaC Code 的标准配置目录 `~/.iac-code/`。如果你已经在 REPL、Web 版或桌面版中配置过
IaC Code，Skill 会复用这些设置。可以通过 `IAC_CODE_CONFIG_DIR` 指定其他配置目录。

在自动化环境中，请通过密钥管理方案注入模型设置和阿里云凭证。不要把凭证写入 `SKILL.md`、提示词、
项目文件或 Shell 历史记录。建议使用临时凭证、RAM 角色或 OAuth，并只授予任务所需权限。

配置选项和支持的环境变量详见[模型服务](../configuration/llm-providers.md)和
[阿里云凭证](../configuration/alibaba-cloud-credentials.md)。

## 选择工作方式

Skill 会根据请求选择两种模式之一：

- **普通模式**：默认模式，适合查询或变更资源、处理模板、排查问题，以及部署目标明确的资源。
- **Pipeline 模式**：当你明确要求使用，或需要候选架构、费用对比、方案确认和部署组成的引导流程时使用。

通常不需要手工选择模式，直接描述期望结果即可。只有需要方案对比流程时，才需要特别说明使用 Pipeline。

## 首次使用

在宿主 Agent 中新建会话，直接描述阿里云基础设施任务。例如：

```text
使用 iac-code 评审当前项目中的 ROS 模板，列出安全风险和修改建议，但不要修改文件。
```

在 Codex 中使用 `$iac-code`，在 Claude Code 中使用 `/iac-code`，可以显式选择 Skill。第一次请求时，Agent 会检查模型和云凭证配置、准备
Runtime 并启动任务，不需要手工启动 A2A Server。

执行过程中，IaC Code 可能暂停并请你：

- 允许或拒绝工具及部署操作（`permission`）；
- 回答问题（`ask_user_question`）；
- 选择候选架构（`candidate_selection`）；
- 检查最终方案、价格和部署参数，然后确认、调整、重新选择或取消（`deployment_confirmation`）。

回答前请检查目标资源、地域、影响和报价。最初提出部署需求，不代表已经批准后续的部署确认。任务结束后，
可以在同一 Agent 会话继续提出要求，Skill 会保留 IaC Code 对话上下文。

IaC Code 可以根据会话语言，以英语、简体中文、西班牙语、法语、德语、日语或葡萄牙语返回进度和问题。

## 更新和卸载

手工更新时，重新下载稳定版 ZIP 并完整替换 `iac-code/` 目录。随后重启 Agent 或新建会话，让它重新加载
Skill。不要只替换桥接脚本，也不要手工修改 Runtime 地址或摘要。

卸载时，从宿主 Agent 的 Skill 目录删除 `iac-code/`。已经下载的 Runtime 会保留在 IaC Code 配置目录，
避免影响其他安装和正在运行的任务。如果也要清理这些文件，请先运行 `cache list` 检查，再运行
`cache clean ... --confirm`。

## 常见问题

### 配置不完整

如果模型服务或 API Key 配置不完整，Skill 会在创建任务前返回 `llm_not_configured`。两种 Pipeline 都要求
配置阿里云凭证，缺失时会返回 `cloud_credentials_not_configured`。普通模式仍可执行不调用云 API 的任务，
但会提示当前无法进行云资源操作。

### Runtime 无法启动

运行 `ensure-runtime` 并查看错误信息，检查宿主 Python 版本、操作系统、架构、网络和代理设置。
`incompatible_host` 表示当前机器不满足 Runtime 要求，应升级宿主环境或换到支持的平台，不要安装无关的
软件包或 Runtime 规避检查。

### 任务暂停或中断

暂停通常表示 IaC Code 正在等待回答、权限、候选方案选择或部署确认，并非执行失败。请回答 Agent 展示的
当前请求。如果中断后宿主会话仍然存在，请让它继续同一个任务，以便恢复已有作业，而不是重新开始。

### 管理 Runtime 磁盘占用

在已安装的 Skill 目录中使用：

- `python3 scripts/iac_code.py cache list`：查看已经安装的 Runtime；
- `python3 scripts/iac_code.py cache clean --runtime-tag <tag> --confirm`：删除指定的历史 Runtime；
- `python3 scripts/iac_code.py cache clean --candidates --confirm`：删除候选版本。

当前 Runtime 和正在被进程使用的软件包不会被清理。Windows 请把 `python3` 替换为 `py -3`。

## 安全说明

- Runtime 只监听随机的 `127.0.0.1` 端口，每个进程都会生成新的 Bearer Token。
- 任务产物和结果文件保存在所选工作目录中；适用时位于 `.iac-code-skill-results/`。
- 就绪状态和权限摘要经过脱敏，不包含凭证值。

## 相关文档

- [IaC Code 官方 Skills 概览](./skill-overview.md)
- [IaC Code Skill 宿主集成参考](./skill-host-integration.md)
- [A2A 协议概览](./overview.md)
- [模型服务](../configuration/llm-providers.md)
- [阿里云凭证](../configuration/alibaba-cloud-credentials.md)
- [运行时配置](../configuration/runtime-configuration.md)
