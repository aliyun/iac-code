---
sidebar_position: 1
title: IaC Code 官方 Skills 概览
description: 对比 IaC Code 官方 Skills，并根据使用方式选择合适的版本。
---

# IaC Code 官方 Skills 概览

IaC Code 提供三种官方 Skill 发行形式。它们都能让用户在 Agent 对话中管理阿里云基础设施，但发行渠道和
IaC Code Agent 的运行位置不同。

## 选择 Skill

| Skill | 运行位置 | 适用场景 |
|---|---|---|
| `iac-code` | 下载到本机且经过校验的 IaC Code Runtime | 希望使用 iac-code 项目直接发布的软件包，并自行控制安装和更新。 |
| `alibabacloud-iac-code` | 同样在本地运行的 IaC Code Runtime，针对阿里云 Agent Skills 门户打包 | 通过 Skills 门户或 `npx skills` 安装、更新阿里云 Skills。 |
| `alibabacloud-ros-agent` | 通过 ROS StartChat API 调用的阿里云云端 ROS Agent | 希望直接使用云端 ROS Agent，不在本机下载 IaC Code Runtime。 |

`iac-code` 和 `alibabacloud-iac-code` 提供相同的 IaC Code Runtime 能力。同一个 Agent 作用域中选择一种
发行方式即可；同时安装只会造成触发范围重叠，不会增加功能。

`alibabacloud-ros-agent` 是独立的云端服务集成。如果需要明确选择本地 IaC Code 或云端 ROS Agent，可以将
它与一种本地 Runtime 发行版同时安装。

## 获取独立发行版

通过固定地址下载最新稳定版：

[下载 iac-code-skill.zip](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

独立发行版适合手工管理 Skill 目录。它会在首次使用时下载 Runtime，并复用 `~/.iac-code/` 中的模型和
阿里云配置。支持的宿主和配置方法详见[安装和使用 IaC Code Skill](./skill-integration.md)。

## 获取阿里云 Skills 门户版本

在[阿里云 Agent Skills 门户](https://skills.aliyun.com/)中搜索准确的 Skill 名称，或者从官方仓库安装：

```bash
npx skills add aliyun/alibabacloud-aiops-skills --skill alibabacloud-iac-code
npx skills add aliyun/alibabacloud-aiops-skills --skill alibabacloud-ros-agent
```

也可以直接下载软件包：

- [`alibabacloud-iac-code` ZIP](https://skills.aliyun.com/api/public/skills/alibabacloud-iac-code/download) ·
  [查看源码](https://github.com/aliyun/alibabacloud-aiops-skills/tree/master/skills/developertools/ros/alibabacloud-iac-code)
- [`alibabacloud-ros-agent` ZIP](https://skills.aliyun.com/api/public/skills/alibabacloud-ros-agent/download) ·
  [查看源码](https://github.com/aliyun/alibabacloud-aiops-skills/tree/master/skills/developertools/ros/alibabacloud-ros-agent)

`npx skills` 会交互式选择支持的 Agent 和安装范围，这种方式要求 Node.js 18 或更高版本。手工下载 ZIP 时，
请将其中的顶层 Skill 目录解压到宿主支持的用户级或项目级 Skill 目录，并按需重启 Agent。

## 能力和配置差异

两种本地 Runtime 发行版都支持普通对话和 Pipeline，包括架构规划、ROS 与 Terraform 模板处理、费用估算、
资源栈操作、部署、提问、候选方案选择、权限审批和部署确认。它们需要配置模型；任务查询或变更云资源时还需
配置阿里云凭证。

云端 `alibabacloud-ros-agent` 通过 `ros:StartChat` 将会话发送给阿里云 ROS Agent。它使用宿主可用的阿里云
身份，不需要本地 IaC Code Runtime，也不需要在本地配置模型服务。请只授予所需的 RAM 权限；明确取消远程
任务时还会调用 `ros:StopChat`。

无论选择哪种发行版，批准变更或部署前都应检查目标资源、地域、影响、价格和请求的权限。不要把凭证写入
`SKILL.md`、提示词或项目文件。

## 相关文档

- [安装和使用 IaC Code Skill](./skill-integration.md)
- [IaC Code Skill 宿主集成参考](./skill-host-integration.md)
- [阿里云凭证](../configuration/alibaba-cloud-credentials.md)
- [A2A 协议概览](./overview.md)
