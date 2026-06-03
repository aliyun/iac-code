---
title: Alibaba Cloud 認証情報
description: Alibaba Cloud の AccessKey または STS 認証情報の設定。
---

# Alibaba Cloud 認証情報

Alibaba Cloud の認証情報は、クラウドリソースの検査や管理を行う操作に必要です。

## OAuth ブラウザログイン

推奨される対話型セットアップ手順は `/auth` です。

```text
/auth
```

**IaC クラウドサービスを設定**、**Alibaba Cloud**、**OAuth Login (Browser)** の順に選択します。IaC Code はブラウザの認可フローを開き、ローカル callback を待ち受け、PKCE で認可コードを交換して、OAuth に基づく一時認証情報を IaC Code 設定ディレクトリ内の `.cloud-credentials.yml` に保存します。

セットアップ中に、中国または国際版の OAuth サイトを選択できます。IaC Code は選択したサイトを refresh token と一緒に保存し、以降の更新で同じ endpoint を使用します。

access token または STS 認証情報の有効期限が近づくと、OAuth 認証情報は自動的に更新されます。refresh token の有効期限が切れた場合、または取り消された場合は、もう一度 `/auth` を実行して OAuth Login (Browser) を選択してください。

## 環境変数

サポートされる環境変数：

| 変数 | 説明 |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | STS トークン。設定すると認証モードが STS に切り替わります |
| `ALIBABA_CLOUD_REGION_ID` | デフォルトリージョン |

実験時はテスト用または一時的な認証情報を使用してください。本番環境のシークレットをシェル履歴、スクリーンショット、ログ、Issue レポートに貼り付けないでください。
