# iac-code

**Language**: [English](../README.md) | [中文](README.zh.md) | [Español](README.es.md) | [Français](README.fr.md) | [Deutsch](README.de.md) | 日本語 | [Português](README.pt.md)

自然言語インタラクションを通じて、Alibaba Cloud のリソースオーケストレーションテンプレート（ROS / Terraform）を生成・管理する AI 駆動の Infrastructure as Code（IaC）アシスタントです。

> **ドキュメント**：[https://aliyun.github.io/iac-code/](https://aliyun.github.io/iac-code/)

## インストール

```bash
pip install iac-code
```

## 使い方

初回使用時は、インタラクティブモードで `/auth` を入力して LLM プロバイダーと IaC クラウドサービスを設定してください。

### インタラクティブモード

直接実行してインタラクティブ REPL に入ります：

```bash
iac-code
```

### ノンインタラクティブモード

`--prompt` でワンショットプロンプトを渡します：

```bash
iac-code --prompt "VPC と 2 つの ECS インスタンスを作成"
```

stdin からの読み取りもサポートされています：

```bash
echo "OSS バケットを作成" | iac-code --prompt -
```
