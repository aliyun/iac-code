---
title: CLI 概要
description: ターミナルから IaC Code を起動し、適切なワークフローを選択。
---

# CLI 概要

ターミナルで `iac-code` を実行すると、ローカル Web アプリが起動し、ブラウザが開きます。初回は設定画面で LLM プロバイダーと Alibaba Cloud の認証情報を設定してください。

```bash
iac-code
iac-code web --no-open
```

ターミナル UI は `iac-code tui` で引き続き利用できますが、更新とメンテナンスは終了しています。今後は Web 版またはデスクトップ版をご利用ください。

```bash
iac-code tui
iac-code tui --help
```

`iac-code` は既定で Web アプリを開きます。ターミナル用のオプションは `iac-code tui --resume <id>` のように `tui` の後に指定してください。headless の `--prompt` と stream-json のオプションは従来どおりルートコマンドに指定します。対話型ターミナルがない場合は `iac-code web --no-open` で明示的に起動してください。

```bash
iac-code --prompt "Create an OSS Bucket"
iac-code --input-format stream-json --output-format stream-json
```
