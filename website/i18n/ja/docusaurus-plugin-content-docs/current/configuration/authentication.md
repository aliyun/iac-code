---
title: 認証
description: デスクトップまたは Web の設定でモデルとクラウドへのアクセスを設定します。
---

# 認証

デスクトップを開くか、対話端末で `iac-code` を実行して Web を開きます。設定からモデルと必要なクラウド認証情報を登録します。

- [デスクトップまたは Web でプロバイダーを設定する](./llm-providers.md)
- [デスクトップまたは Web でクラウドアクセスを設定する](./alibaba-cloud-credentials.md)

Web 版、デスクトップ版、ヘッドレス実行は保存済みの認証情報を共有します。

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
