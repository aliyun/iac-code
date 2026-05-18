<p align="center">
  <img src="website/static/img/logo-with-front.png" alt="iac-code" width="200">
</p>
<p align="center">
  <em>AI-powered Infrastructure as Code assistant for Alibaba Cloud (ROS / Terraform) through natural language interaction.</em>
</p>
<p align="center">
  <a href="https://github.com/aliyun/iac-code/actions/workflows/test.yml"><img src="https://github.com/aliyun/iac-code/actions/workflows/test.yml/badge.svg" alt="Test"></a>
  <a href="https://pypi.org/project/iac-code"><img src="https://img.shields.io/pypi/v/iac-code?color=%2334D058&label=pypi%20package" alt="PyPI Package"></a>
  <a href="https://pypi.org/project/iac-code"><img src="https://img.shields.io/pypi/pyversions/iac-code?color=%2334D058&label=python" alt="Python"></a>
</p>
<p align="center">
  <strong>Language</strong>: English | <a href="readme/README.zh.md">中文</a> | <a href="readme/README.es.md">Español</a> | <a href="readme/README.fr.md">Français</a> | <a href="readme/README.de.md">Deutsch</a> | <a href="readme/README.ja.md">日本語</a> | <a href="readme/README.pt.md">Português</a>
</p>

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