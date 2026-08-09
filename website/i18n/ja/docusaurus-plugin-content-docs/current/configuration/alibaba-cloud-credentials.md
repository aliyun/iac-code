---
title: Alibaba Cloud 認証情報
description: ECS RAM ロール認証を含む Alibaba Cloud 認証情報の設定。
---

# Alibaba Cloud 認証情報

Alibaba Cloud の認証情報は、クラウドリソースの検査や管理を行う操作に必要です。

## ECS RAM ロール

IaC Code を RAM ロールが割り当てられた Alibaba Cloud ECS インスタンス上で実行する場合は、**ECS RAM Role** を使用できます。IaC Code は ECS インスタンスメタデータサービス（IMDS）から一時 STS 認証情報を取得して自動更新し、AccessKey ID、AccessKey Secret、STS トークンを設定ファイルに保存しません。

このモードはすべてのユーザーインターフェースから設定できます。

- REPL で `/auth` を実行し、**IaC クラウドサービスを設定**、**Alibaba Cloud**、**ECS RAM Role** の順に選択します。
- Web または Desktop アプリで **設定 > クラウド認証情報** を開き、**Alibaba Cloud** を選択して、認証方式に **ECS RAM Role** を指定します。

クラウド API 呼び出しに使用するリージョンを選択します。ECS RAM ロール名は任意です。空欄にすると、インスタンスに割り当てられたロールを IMDS から自動検出します。IaC Code に保存されたロール名は `ALIBABA_CLOUD_ECS_METADATA` より優先され、どちらも設定されていない場合は IMDS にロール名の検出を要求します。

同等の `.cloud-credentials.yml` 設定は次のとおりです。

```yaml
aliyun:
  mode: EcsRamRole
  region_id: cn-beijing
  ram_role_name: MyEcsRole # 任意。自動検出する場合は省略するか空欄にします
```

`~/.aliyun/config.json` のアクティブなプロファイルで `mode` が `EcsRamRole` に設定されている場合も、IaC Code はその設定を認識します。この場合も `ram_role_name` は任意です。

設定自体はどのマシンでも保存できますが、クラウド API 呼び出しが成功するには ECS IMDS にアクセスでき、インスタンスに一致する RAM ロールが割り当てられている必要があります。呼び出し可能な API は、ロールにアタッチされた RAM ポリシーによって決まります。

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
| `ALIBABA_CLOUD_ECS_METADATA` | 任意の ECS RAM ロール名。モードがすでに `EcsRamRole` で、保存済みのロール名がない場合に使用されます。この変数だけではモードは選択されません |
| `ALIBABA_CLOUD_ECS_METADATA_DISABLED` | `true` に設定すると、ECS インスタンスメタデータ認証情報を無効にします |
| `ALIBABA_CLOUD_IMDSV1_DISABLED` | `true` に設定すると IMDSv2 を必須とし、IMDSv1 へのフォールバックを無効にします |

実験時はテスト用または一時的な認証情報を使用してください。本番環境のシークレットをシェル履歴、スクリーンショット、ログ、Issue レポートに貼り付けないでください。
