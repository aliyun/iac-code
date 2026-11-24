---
title: CLI 概要
description: ターミナルから IaC Code を起動し、適切なワークフローを選択。
---

# CLI 概要

ターミナルで `iac-code` を実行すると、ローカル Web アプリが起動してブラウザーが開きます。初回は設定で LLM プロバイダーと Alibaba Cloud 認証情報を登録します。

```bash
iac-code
iac-code web --no-open
```

Web またはデスクトップで会話を作成する際に、通常チャットか Pipeline を選択します。自動化には単一プロンプトまたは stream-json サブプロセスを使用します。

```bash
iac-code --prompt "VPC テンプレートを生成する"
iac-code --input-format stream-json --output-format stream-json
```

詳細は[コマンドラインオプション](./command-line-options.md)と[非対話型の自動化](../automation/non-interactive-mode.md)を参照してください。対話端末がない場合は `iac-code web --no-open` で Web を明示的に起動します。

既存のターミナル操作との互換性については[従来のターミナル起動](./interactive-mode.md)を参照してください。
