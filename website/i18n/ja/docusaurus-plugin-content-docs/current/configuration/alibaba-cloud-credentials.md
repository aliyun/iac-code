---
title: Alibaba Cloud 認証情報
description: ECS RAM ロール認証を含む Alibaba Cloud 認証情報の設定。
---

# Alibaba Cloud 認証情報

以下の手順はデスクトップと Web に共通です。両者は同じ会話画面を使用します。

## デスクトップまたは Web でクラウドアクセスを設定する

1. **設定 → クラウド認証情報**を開き、**Alibaba Cloud**を選択します。

2. 照会やデプロイに使うリージョンを選びます。例：`cn-hangzhou`。

3. 認証方法を選び、下表の該当項目を入力します。

4. AccessKey、STS、RAM ロール、ECS RAM ロールでは**クラウド認証情報を保存**をクリックします。OAuth では下のブラウザーログイン手順に従います。

5. 会話に戻り、クラウドの作業を依頼します。認証情報の保存だけではリソースは作成されず、RAM 権限も追加されません。

| 認証方法 | 入力項目 |
|---|---|
| AccessKey | `AccessKeyId` と `AccessKeySecret`。 |
| STS Token | 同じ一時認証情報の `AccessKeyId`、`AccessKeySecret`、`StsToken`。 |
| RAM ロール | 元の `AccessKeyId`、`AccessKeySecret`、対象の `RamRoleArn`、任意の `RamSessionName`。 |
| ECS RAM ロール | 任意のロール名。空欄では IaC Code を実行している ECS のメタデータから検出します。 |
| OAuth ブラウザーログイン | アカウントのサイトを選び、ブラウザーで承認します。トークンの手入力は不要です。 |

Alibaba Cloud の認証情報は、クラウドリソースの検査や管理を行う操作に必要です。

`resolve_cloud_resource_selector` と `select_cloud_resource` は、`aliyun_api` と同じ有効認証情報の判定を使用します。A2A ではリクエストまたはセッションの認証情報が優先され、ない場合は通常どおりローカル設定、環境変数、Alibaba Cloud CLI 認証情報へフォールバックします。有効な認証情報がない場合、`aliyun_api` と 2 つのリソースセレクターツールはいずれも登録されません。

## ECS RAM ロール

IaC Code を RAM ロールが割り当てられた Alibaba Cloud ECS インスタンス上で実行する場合は、**ECS RAM Role** を使用できます。IaC Code は ECS インスタンスメタデータサービス（IMDS）から一時 STS 認証情報を取得して自動更新し、AccessKey ID、AccessKey Secret、STS トークンを設定ファイルに保存しません。

Web または Desktop アプリで **設定 > クラウド認証情報** を開き、**Alibaba Cloud** を選択して、認証方式に **ECS RAM Role** を指定します。

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

1. **設定 → クラウド認証情報**を開き、**Alibaba Cloud**と **OAuth ブラウザーログイン**を選びます。

2. リージョンと、アカウントに合う**ログインサイト**（中国または国際）を選択します。

3. **ブラウザーでログイン**をクリックし、Alibaba Cloud のページでログインと承認を完了します。

4. IaC Code に戻って成功表示を待ちます。認証情報と選択したサイトはログイン処理で保存されます。

アクセストークンまたは STS 認証情報の有効期限が近づくと、OAuth 認証情報は自動更新されます。リフレッシュトークンが失効または取り消された場合は、**設定 → クラウド認証情報**から再認可してください。

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
