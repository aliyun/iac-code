---
title: クイックスタート
description: IaC Code の設定と最初のプロンプトの実行。
---

# クイックスタート

対話型 CLI を実行します：

```bash
iac-code
```

初回使用時に、LLM プロバイダーと Alibaba Cloud の認証情報を設定します：

```text
/auth
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
