---
title: クイックスタート
description: IaC Code の設定と最初のプロンプトの実行。
---

# クイックスタート

ターミナルで `iac-code` を実行すると、ローカル Web アプリが起動し、ブラウザが開きます。初回は設定画面で LLM プロバイダーと Alibaba Cloud の認証情報を設定してください。

```bash
iac-code
```

次にインフラを指示します：

```text
Create a VPC and two ECS instances
```

ワンショットプロンプトの場合は、非対話モードを使用します：

```bash
iac-code --prompt "Create an OSS Bucket"
```

標準入力からプロンプトを読み取ることもできます：

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```
