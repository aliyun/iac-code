---
title: 認証
description: 認証フローで LLM とクラウドの認証情報を設定。
---

# 認証

ターミナルで `iac-code` を実行すると、ローカル Web アプリが起動し、ブラウザが開きます。初回は設定画面で LLM プロバイダーと Alibaba Cloud の認証情報を設定してください。

ターミナル UI は `iac-code tui` で引き続き利用できますが、更新とメンテナンスは終了しています。今後は Web 版またはデスクトップ版をご利用ください。

対話モードで `/auth` を使用して、モデルプロバイダーのアクセスと Alibaba Cloud のアクセスの両方を設定します。

```bash
iac-code tui
```

```text
/auth
```

認証フローでは、プロバイダーの選択、API キーの入力、モデルの選択、Alibaba Cloud の認証情報のセットアップをガイドします。

ランタイム設定はユーザー設定ディレクトリに保存されます：

```text
~/.iac-code/
```

重要なファイル：

| ファイル | 用途 |
|---|---|
| `.credentials.yml` | LLM プロバイダーの認証情報 |
| `.cloud-credentials.yml` | Alibaba Cloud の認証情報 |
| `settings.yml` | ランタイム設定 |
