# iac-code

**Language**: English | [中文](readme/README.zh.md) | [Español](readme/README.es.md) | [Français](readme/README.fr.md) | [Deutsch](readme/README.de.md) | [日本語](readme/README.ja.md) | [Português](readme/README.pt.md)

AI-powered Infrastructure as Code (IaC) assistant that generates and manages Alibaba Cloud resource orchestration templates (ROS / Terraform) through natural language interaction.

> **Documentation**: [https://aliyun.github.io/iac-code/](https://aliyun.github.io/iac-code/)

## Installation

```bash
pip install iac-code
```

## Usage

On first use, configure the LLM provider and IaC cloud service by entering `/auth` in interactive mode.

### Interactive Mode

Run directly to enter the interactive REPL:

```bash
iac-code
```

### Non-Interactive Mode

Pass a one-shot prompt via `--prompt`:

```bash
iac-code --prompt "Create a VPC and two ECS instances"
```

Reading from stdin is also supported:

```bash
echo "Create an OSS Bucket" | iac-code --prompt -
```
