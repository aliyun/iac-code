---
sidebar_position: 7
title: IaC Code Skill のインストールと使用
description: IaC Code Skill をダウンロードしてインストールし、外部エージェントから Alibaba Cloud インフラストラクチャを管理します。
---

# IaC Code Skill のインストールと使用

IaC Code Skill は、Skill に対応する外部エージェント向けです。インストールすると、ホストエージェントはクラウドアーキテクチャの設計、ROS または Terraform テンプレートの生成とレビュー、コスト見積もり、リソース選択、スタック操作、デプロイを IaC Code に委任できます。Skill は Python 標準ライブラリだけで構成されたブリッジを使い、ローカルで認証された A2A Runtime を起動します。IaC Code を pip でインストールする必要はなく、Headless コマンドにフォールバックすることもありません。

## Skill のダウンロード

### 最新の安定版

最新の安定版を直接ダウンロードします。

[iac-code-skill.zip をダウンロード](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

この固定 URL は、stable チャンネルに昇格された Skill パッケージを常に指します。ブラウザーからのダウンロードや手動インストールに利用でき、新しいバージョンが公開されても URL は変わりません。

バージョン、ファイルサイズ、SHA-256、変更されないバージョン別 URL が必要なインストーラーは、stable チャンネルのメタデータを参照できます。

[latest.json を表示](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/latest.json)

このファイルには次の情報が含まれます。

- `skillVersion`：現在の安定版 Skill のバージョン。
- `skill.url`：そのバージョンに固定された ZIP の URL。
- `skill.sha256` と `skill.size`：ダウンロードの検証に使う値。
- `manifest.url`：そのバージョンに固定されたリリースマニフェスト。

厳密な検証や再現可能な自動インストールが必要な場合は、`latest.json` を読み取り、`skill.url` からダウンロードして `skill.sha256` を検証してください。バージョン URL を独自に組み立てないでください。

## Skill のインストール

### 前提条件

- ホストエージェントが `SKILL.md` で定義されたローカル Skill に対応していること。
- CPython 3.8～3.14 がインストールされていること。macOS/Linux では `python3`、Windows では `py -3` の使用を推奨します。
- 上記 OSS URL にアクセスでき、Skill ZIP と初回実行時に必要な Runtime をダウンロードできること。
- モデルサービスが設定済みであること。クラウドリソースを照会または管理する場合は、最小権限の Alibaba Cloud ID も必要です。

公式 Skill Runtime は次のプラットフォームをサポートします。

| OS | アーキテクチャ |
|---|---|
| macOS | Apple Silicon（arm64） |
| Linux | x86_64 |
| Windows | x86_64 |

最低 OS バージョンと Linux の glibc バージョンは、Skill に固定された Runtime manifest で定義されます。ブリッジはダウンロード前に互換性を確認します。未対応の環境では、別のプラットフォームや ABI の成果物をダウンロードせず、エラーを返します。

### ホストエージェントの Skill ディレクトリに展開する

ZIP をホストエージェントの Skill ルートへ直接展開します。Skill ルートは製品ごとに異なるため、ホスト製品のドキュメントを参照してください。最終的な構成は次のようになります。

```text
<Agent Skill ルート>/
└── iac-code/
    ├── SKILL.md
    ├── agents/
    │   └── openai.yaml
    └── scripts/
        └── iac_code.py
```

ZIP には最上位の `iac-code/` ディレクトリがすでに含まれています。同名のディレクトリを重ねて作成しないでください。インストールまたは更新後、ホストエージェントを再起動するか新しいセッションを開き、Skill を再検出させます。

### インストールを確認する

展開した `iac-code` ディレクトリで、macOS または Linux では次を実行します。

```bash
python3 scripts/iac_code.py ensure-runtime
```

Windows PowerShell では次を実行します。

```powershell
py -3 scripts\iac_code.py ensure-runtime
```

初回実行時に現在のプラットフォーム用 Runtime をダウンロードし、サイズと SHA-256 を検証したうえで、`skillVersion`、`runtimeTag`、インストール先を含む JSON を出力します。検証済みの Runtime がキャッシュにあれば再利用し、再ダウンロードしません。

## モデルと Alibaba Cloud ID の設定

Skill Runtime は、他の IaC Code 実行モードと同じ設定ディレクトリを使用します。既定は `~/.iac-code/` です。REPL、Web、Desktop のいずれかで IaC Code を設定済みであれば、その設定を再利用できます。別の設定ディレクトリを使う場合は `IAC_CODE_CONFIG_DIR` を指定します。

自動化環境では、Secret 管理機能を使って次の環境変数を提供します。

| 分類 | 環境変数 | 説明 |
|---|---|---|
| モデル | `IAC_CODE_PROVIDER` | モデルプロバイダー |
| モデル | `IAC_CODE_MODEL` | モデル名 |
| モデル | `IAC_CODE_API_KEY` | モデルサービスの API Key |
| モデル | `IAC_CODE_BASE_URL` | 任意の互換エンドポイント上書き |
| Alibaba Cloud | `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| Alibaba Cloud | `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey Secret |
| Alibaba Cloud | `ALIBABA_CLOUD_SECURITY_TOKEN` | STS 認証情報の Security Token |
| Alibaba Cloud | `ALIBABA_CLOUD_REGION_ID` | 既定のリージョン |

実際の認証情報を `SKILL.md`、ホストエージェントのプロンプト、プロジェクトファイル、シェル履歴に記録しないでください。一時認証情報、RAM Role、OAuth を優先し、タスクに必要なクラウド API 権限だけを付与します。詳しくは [LLM プロバイダー](../configuration/llm-providers.md) と [Alibaba Cloud 認証情報](../configuration/alibaba-cloud-credentials.md)を参照してください。

## 最初の利用

インストールと設定が完了したら、ホストエージェントで新しいセッションを開き、Alibaba Cloud インフラストラクチャのタスクをそのまま記述します。例：

```text
iac-code を使用して、このプロジェクトの ROS テンプレートをレビューしてください。ファイルは変更せず、セキュリティリスクと修正案を一覧にしてください。
```

明示的な Skill 構文に対応するホストでは、`$iac-code` でこの Skill を選択できます。ホストエージェントは `SKILL.md` を読み取り、完全なリクエストをワークスペース内の UTF-8 ファイルに書き込み、ブリッジを使って同じタスクを作成して追跡します。ユーザーが A2A Server を手動で起動する必要はありません。

想定される流れ：

1. ブリッジがモデルと Alibaba Cloud の設定状態を確認します。
2. 初回実行時に、Skill に固定された IaC Code Runtime をダウンロードして検証します。
3. Runtime は `127.0.0.1` のランダムなポートだけで待ち受け、プロセス固有の Bearer Token を生成します。
4. ホストエージェントが、IaC Code から返された進捗、質問、候補プラン、権限リクエストを表示します。
5. タスクが完了すると、ホストエージェントが最終結果とワークスペースで生成されたファイルを返します。

## 更新とアンインストール

手動で更新する場合は `skill/stable/iac-code-skill.zip` を再度ダウンロードし、ホストの Skill ルートにある `iac-code/` ディレクトリ全体を置き換えます。自動更新では `latest.json` の `skillVersion` を比較し、変更されない URL と SHA-256 を使って新しいパッケージをダウンロード、検証できます。公式 Skill はそれぞれ検証済み Runtime に固定されています。`scripts/iac_code.py` だけを置き換えたり、Runtime URL やダイジェストを手動で変更したりしないでください。

アンインストールするには、ホストエージェントの Skill ルートから `iac-code/` を削除します。Runtime キャッシュは Skill ディレクトリと一緒には削除されません。ユーザーが明示的に削除を求めた場合にだけ `cache list` と `cache clean` を実行してください。

## Runtime キャッシュ

初回利用時にダウンロードされた Runtime は `<IAC_CODE_CONFIG_DIR または ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/` にキャッシュされ、その後は自動的に再利用されます。通常はこのディレクトリを管理する必要はありません。ディスク使用量の確認や過去バージョンの削除には次を使用します。

- `python3 scripts/iac_code.py cache list` — インストール済み Runtime と Candidate パッケージを表示します。
- `python3 scripts/iac_code.py cache clean [--runtime-tag <tag>] [--candidates] --confirm` — Runtime キャッシュまたは Candidate パッケージを削除します。`--confirm` が必須です。

現在使用中の Runtime と実行中プロセスが使用している Runtime は削除から保護されます。パッケージ形式と Runtime の制約は、ソースリポジトリの `skill-runtime/skill-package-contract.json` で定義されます。通常のユーザーがこのファイルを操作する必要はありません。

## トラブルシューティング

### 設定が不完全と表示される

Skill はタスク作成前に設定を確認しますが、Secret の値を読み取ったり返したりしません。

| 状況 | 結果 |
|---|---|
| LLM プロバイダーまたは API Key が不完全 | `llm_not_configured` を返し、タスクを作成しません |
| selling Pipeline で Alibaba Cloud 認証情報が不完全 | `cloud_credentials_not_configured` を返し、タスクを作成しません |
| normal モードで Alibaba Cloud 認証情報が不完全 | クラウド API を呼び出さないタスクは、事前確認の警告付きで続行できる場合があります |

### 実行中に一時停止する理由

IaC Code は権限の確認、追加情報、プラン選択が必要になると一時停止し、ホストエージェントが要求をユーザーに表示します。

- ツールまたはデプロイの権限リクエスト（`permission`）。
- 選択式の質問または追加情報の要求（`ask_user_question`）。
- Pipeline の候補プラン選択（`candidate_selection`）。

確認前に、対象リソース、リージョン、想定される影響、価格を確認してください。ホストエージェントは IaC Code の拒否を上書きできません。1 回限りの許可はプロトコル上 `allow_once` として表されます。

> **ホストエージェントの統合に関する注意**
>
> ブリッジ結果に `inputRequired` が含まれる場合、ホストエージェントは現在の要求を表示し、応答を待つ必要があります。`boundaryReached` は表示または対話の境界に到達したことを示すだけで、タスクの完了を意味しません。ホストは更新を表示して、同じタスクの追跡を続けます。

## セキュリティ

- Runtime は `127.0.0.1` のランダムなポートだけで待ち受けます。起動ごとに新しい Bearer Token を生成し、すべてのブリッジリクエストに付与します。
- ブリッジは成果物と結果をジョブのワークスペース内に保持します。結果は `.iac-code-skill-results/` に書き込まれます。
- 事前確認と権限表示のフィールドはサニタイズされ、Secret や認証情報は表示されません。

## 関連ドキュメント

- [A2A プロトコル概要](./overview.md)
- [A2A プロトコルリファレンス](./protocol-reference.md)
- [LLM プロバイダー](../configuration/llm-providers.md)
- [Alibaba Cloud 認証情報](../configuration/alibaba-cloud-credentials.md)
- [Runtime 設定](../configuration/runtime-configuration.md)
