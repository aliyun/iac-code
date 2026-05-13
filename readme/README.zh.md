# iac-code

**Language**: [English](../README.md) | 中文 | [Español](README.es.md) | [Français](README.fr.md) | [Deutsch](README.de.md) | [日本語](README.ja.md) | [Português](README.pt.md)

AI 驱动的基础设施即代码（IaC）助手，通过自然语言交互生成和管理阿里云基础设施资源编排模板（ROS / Terraform）。

> **文档**：[https://aliyun.github.io/iac-code/](https://aliyun.github.io/iac-code/zh-Hans/)

## 安装

```bash
pip install iac-code
```

## 使用

首次使用需要先配置 LLM 提供商和 IaC 云服务，在交互模式中输入 `/auth` 完成配置。

### 交互模式

直接运行进入交互式 REPL：

```bash
iac-code
```

### 非交互模式

通过 `--prompt` 传入单次提示：

```bash
iac-code --prompt "创建一个 VPC 和两台 ECS 实例"
```

也支持从 stdin 读取输入：

```bash
echo "创建一个 OSS Bucket" | iac-code --prompt -
```
