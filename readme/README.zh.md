<p align="center">
  <img src="../website/static/img/logo-with-front.png" alt="iac-code" width="200">
</p>
<p align="center">
  <em>AI 驱动的基础设施即代码助手，通过自然语言交互生成和管理阿里云资源编排模板（ROS / Terraform）。</em>
</p>
<p align="center">
  <a href="https://github.com/aliyun/iac-code/actions/workflows/test.yml"><img src="https://github.com/aliyun/iac-code/actions/workflows/test.yml/badge.svg" alt="Test"></a>
  <a href="https://pypi.org/project/iac-code"><img src="https://img.shields.io/pypi/v/iac-code?color=%2334D058&label=pypi%20package" alt="PyPI Package"></a>
  <a href="https://pypi.org/project/iac-code"><img src="https://img.shields.io/pypi/pyversions/iac-code?color=%2334D058&label=python" alt="Python"></a>
</p>
<p align="center">
  <strong>Language</strong>: <a href="../README.md">English</a> | 中文 | <a href="README.es.md">Español</a> | <a href="README.fr.md">Français</a> | <a href="README.de.md">Deutsch</a> | <a href="README.ja.md">日本語</a> | <a href="README.pt.md">Português</a>
</p>

> **文档**：[https://aliyun.github.io/iac-code/](https://aliyun.github.io/iac-code/zh-Hans/)
<p align="center">
  <img src="../website/static/img/demo_zh.gif" alt="iac-code 演示" width="100%">
</p>

## 安装

IaC Code 需要 Python 3.10 或更高版本。支持 macOS、Linux 和 Windows。

> **Windows 说明**：在 Windows 上需要安装 [Git for Windows](https://gitforwindows.org/) 以提供工具执行所需的 bash 环境。如果 Git Bash 已安装但不在 PATH 中，请设置 `IAC_CODE_GIT_BASH_PATH` 环境变量。

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

## 联系我们

| [钉钉](https://qr.dingtalk.com/action/joingroup?code=v1,k1,ubm/77U7qRh/STFZUNBP26X4PNg2z6+uhiPcLGtDNfU=&_dt_no_comment=1&origin=11) | [Discord](https://discord.gg/qECFuFBwF) |
| :----------------------------------------------------------: | :----------------------------------------------------------: |
| [<img src="../website/static/img/qrcode-dingtalk.jpg" width="120" height="120" alt="DingTalk">](https://qr.dingtalk.com/action/joingroup?code=v1,k1,ubm/77U7qRh/STFZUNBP26X4PNg2z6+uhiPcLGtDNfU=&_dt_no_comment=1&origin=11) | [<img src="../website/static/img/qrcode-discord.jpg" width="120" height="120" alt="Discord">](https://discord.gg/qECFuFBwF) |
