---
title: コマンドリファレンス
description: A2A 経由で iac-code を実行して呼び出すための完全な CLI コマンドリファレンス。
sidebar_position: 3
---

# A2A コマンドリファレンス

このページでは、A2A 関連のすべての `iac-code` コマンドを説明します。正確なオプション名、一般的なコマンドパターン、各フラグの運用上の意味が必要な場合に使用してください。

## コマンド概要

| Command | 用途 |
|---------|---------|
| `iac-code a2a` | iac-code を A2A サーバーとして実行 |
| `iac-code a2a-client call` | リモート Agent Card を発見してプロンプトを送信 |
| `iac-code a2a-client discover` | Agent Card を取得し、任意で検証 |
| `iac-code a2a-client task-get` | ID で 1 つのタスクを取得 |
| `iac-code a2a-client task-list` | フィルターとページネーションでタスクを一覧表示 |
| `iac-code a2a-client task-cancel` | アクティブなタスクをキャンセル |
| `iac-code a2a-client task-subscribe` | アクティブなタスクイベントストリームを購読 |
| `iac-code a2a-client push-config-create` | タスクプッシュ通知設定を作成 |
| `iac-code a2a-client push-config-get` | 1 つのタスクプッシュ通知設定を取得 |
| `iac-code a2a-client push-config-list` | タスクプッシュ通知設定を一覧表示 |
| `iac-code a2a-client push-config-delete` | タスクプッシュ通知設定を削除 |
| `iac-code a2a-client extended-card` | 認証済みの拡張 Agent Card を取得 |
| `iac-code a2a-route-preview` | `a2a-client call` のローカルルート選択をプレビュー |

すべての HTTP クライアントコマンドは、同じ認証オプションを受け付けます。

| Option | 説明 |
|--------|-------------|
| `--token` | `Authorization: Bearer <token>` として送信される Bearer token |
| `--basic-username` | Basic auth のユーザー名 |
| `--basic-password` | Basic auth のパスワード |
| `--api-key` | API key 値 |
| `--api-key-header` | API key ヘッダー名。デフォルトは `X-API-Key` |

## A2A クライアント設定

すべての `a2a-client` サブコマンドは、グループレベルで YAML 設定ファイルを受け付けます。

```bash
iac-code a2a-client --config a2a-client.yml call --prompt "Create a VPC"
```

CLI オプションは設定値を上書きします。安定した接続、認証、検証、ルーティング、繰り返し使うタスクまたはプッシュ設定には config を使用し、一度きりのプロンプトテキストはコマンドラインに置いてください。

```yaml
url: http://127.0.0.1:41242/
token: your-bearer-token
basic-username: iac-code
basic-password: your-password
api-key: your-api-key
api-key-header: X-IAC-Code-Key
verify-card-secret: your-card-signing-secret
verify-card-jwks-url: https://a2a.example.com/.well-known/jwks.json
require-card-signature: true
timeout: 30
cwd: /path/to/workspace
context-id: ctx-123
task-id: task-123
config-id: webhook-1
callback-url: https://hooks.example.com/a2a
notification-token: notification-token
auth-scheme: bearer
auth-credentials: callback-token
routes:
  - name: ros
    url: http://127.0.0.1:41242/
    skills:
      - iac_generation
    tags:
      - ros
      - template
```

## `iac-code a2a`

iac-code を A2A サーバーとして実行します。

```bash
iac-code a2a
```

デフォルトでは、サーバーは `127.0.0.1:41242` にバインドし、HTTP 上の JSON-RPC を提供します。ポート `41242` は iac-code のデフォルトであり、登録済み A2A ポートではありません。

### 基本サーバーオプション

| Option | Default | 説明 |
|--------|---------|-------------|
| `--config` | empty | A2A サーバーオプションを含む YAML 設定ファイル |
| `--host` | `127.0.0.1` | HTTP サーバーホスト |
| `--port` | `41242` | HTTP サーバーポート |
| `--transport` | `http` | サーバートランスポート: `http`, `stdio`, `unix`, `websocket`, `grpc`, `grpc-jsonrpc`, or `redis-streams` |
| `--thinking-exposure` | `tool-trace` | A2A thinking 信号タイプを公開します。複数指定するには繰り返します。値：`raw-thinking`、`tool-trace` |
| `--debug`, `-d` | `false` | デバッグログを有効化 |

例:

```bash
iac-code a2a --host 127.0.0.1 --port 41242 --debug
```

### YAML 設定

認証、ストレージ、署名、トランスポート固有設定、プッシュ配信、その他のデプロイ詳細には `--config` を使用します。キーにはハイフンまたはアンダースコアを使用できます。共通 CLI フラグ `--host`、`--port`、`--transport` は設定ファイルの値を上書きします。

```yaml
host: 127.0.0.1
port: 41242
transport: http
token: local-dev-token
persistence-dir: .iac-code-a2a/state
artifact-dir: .iac-code-a2a/artifacts
push-notifications: true
```

次のように実行します。

```bash
iac-code a2a --config a2a-server.yml --port 41243
```

### HTTP 認証

認証は任意です。サーバー認証は YAML または環境変数で設定します。認証設定がない場合、リクエストは未認証です。1 つ以上の方式が設定されている場合、リクエストはいずれかの設定済み方式を満たせます。

| Config key | Environment Variable | 説明 |
|--------|----------------------|-------------|
| `token` | `IACCODE_A2A_HTTP_TOKEN` | Bearer token |
| `basic-username` | `IACCODE_A2A_BASIC_USERNAME` | Basic auth username |
| `basic-password` | `IACCODE_A2A_BASIC_PASSWORD` | Basic auth password |
| `api-key` | `IACCODE_A2A_API_KEY` | API key value |
| `api-key-header` | `IACCODE_A2A_API_KEY_HEADER` | API key header name |

Bearer token:

```yaml
token: local-dev-token
```

Basic auth:

```yaml
basic-username: iac-code
basic-password: local-dev-password
```

API key:

```yaml
api-key: local-dev-key
api-key-header: X-IAC-Code-Key
```

### 永続化とアーティファクト

| Config key | Default | 説明 |
|--------|---------|-------------|
| `persistence-dir` | `~/.iac-code/a2a` | タスク、コンテキスト、ルート、プッシュ設定のローカル JSON メタデータ |
| `artifact-dir` | `<persistence-dir>/artifacts` | ローカルアーティファクトペイロードストア |

永続化は復元メタデータのためにタスクとコンテキストのスナップショットをミラーします。プロセスクラッシュ後に実行中の asyncio タスクを再開するものではありません。

```yaml
persistence-dir: ~/.iac-code/a2a
artifact-dir: ~/.iac-code/a2a/artifacts
```

### Agent Card 署名

| Config key | 説明 |
|--------|-------------|
| `signing-secret` | 公開 Agent Card の署名に使用される HMAC secret |

サーバーは A2A SDK `AgentCardSignature` JWS フィールドを出力します。対称モードは `HS256` を使用します。

```yaml
signing-secret: local-card-signing-secret
```

### プッシュ通知配信

| Config key | Default | 説明 |
|--------|---------|-------------|
| `push-notifications` | `false` | A2A タスクプッシュ通知設定メソッドと終端状態の配信を有効化 |
| `push-queue` | `local-file` | プッシュキューバックエンド: `local-file` または `redis-streams` |
| `push-redis-url` | empty | Redis ベースのプッシュキュー用 Redis URL |
| `push-stream` | `iac-code:a2a:push` | プッシュジョブ用 Redis stream |
| `push-retry-key` | `iac-code:a2a:push:retry` | 遅延リトライ用 Redis sorted set |
| `push-dead-stream` | `iac-code:a2a:push:dead` | dead-letter ジョブ用 Redis stream |
| `push-consumer-group` | `iac-code-push` | プッシュワーカー用 Redis consumer group |
| `push-consumer-name` | empty | このワーカーの Redis consumer name |
| `push-lease-timeout-ms` | `300000` | Redis pending lease timeout |

ローカルファイルキュー:

```yaml
push-notifications: true
persistence-dir: ~/.iac-code/a2a
push-queue: local-file
```

Redis Streams キュー:

```yaml
push-notifications: true
push-queue: redis-streams
push-redis-url: redis://localhost:6379/0
push-stream: iac-code:a2a:push
push-retry-key: iac-code:a2a:push:retry
push-dead-stream: iac-code:a2a:push:dead
push-consumer-group: iac-code-push
push-consumer-name: worker-1
```

Redis ベースのプッシュ配信には `a2a-redis` extra が必要です。

### トランスポートオプション

| Transport | Command | 注意 |
|-----------|---------|-------|
| HTTP JSON-RPC and REST | `iac-code a2a --transport http` | デフォルト。`JSONRPC` と `HTTP+JSON` インターフェイスを広告します。 |
| stdio | `iac-code a2a --transport stdio` | 標準入出力上の実験的なカスタム JSON-RPC フレーム。 |
| Unix socket | `iac-code a2a --config a2a-server.yml --transport unix` | config に `socket-path` が必要。 |
| WebSocket | `iac-code a2a --config a2a-server.yml --transport websocket` | config の `ws-path` を使用し、デフォルトは `/a2a`。 |
| gRPC | `iac-code a2a --config a2a-server.yml --transport grpc` | config の `grpc-host` と `grpc-port` を使用。 |
| gRPC JSON-RPC | `iac-code a2a --config a2a-server.yml --transport grpc-jsonrpc` | gRPC 上のカスタム JSON-RPC エンベロープ。 |
| Redis Streams | `iac-code a2a --config a2a-server.yml --transport redis-streams` | config に `redis-url` が必要。 |

Redis Streams トランスポートオプション:

| Config key | Default | 説明 |
|--------|---------|-------------|
| `redis-url` | empty | Redis 接続 URL。`--transport redis-streams` では必須 |
| `request-stream` | `iac-code:a2a:requests` | リクエスト stream 名 |
| `response-stream` | `iac-code:a2a:responses` | レスポンス stream 名 |
| `consumer-group` | `iac-code` | リクエスト stream consumer group |

### Thinking の公開

| Config key | Default | 説明 |
|--------|---------|-------------|
| `thinking-exposure` | `tool-trace` | A2A `metadata.iac_code` を通じて公開する非回答ランタイム信号タイプ。YAML リスト、カンマ区切り文字列、または繰り返しの `--thinking-exposure` フラグを使用できます。対応値は `tool-trace` と `raw-thinking` です。 |

`tool-trace` は既存のツール進行状況、権限、結果メタデータを維持します。`raw-thinking` は provider 推論チャンクを `metadata.iac_code.thinking` 更新として出力し、`type: raw_thinking` と `text` を含みます。iac-code は現在、独立した thought-summary または progress-summary イベントを生成しないため、それらは有効な公開タイプではありません。

```yaml
thinking-exposure:
  - tool-trace
  - raw-thinking
```

### 権限の挙動

| Config key | Default | 説明 |
|--------|---------|-------------|
| `auto-approve-permissions` | `false` | A2A ターン中に発生したツール権限リクエストを自動承認 |

`auto-approve-permissions: true` がない場合、A2A モードは権限プロンプトを拒否し、権限メタデータを出力します。信頼できる自動化環境でのみ使用してください。

## `iac-code a2a-client call`

Agent Card を発見し、広告されたエンドポイントを選択して、プロンプトを送信します。

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a ROS VPC template with two vSwitches." \
  --cwd "$PWD"
```

| Option | Default | 説明 |
|--------|---------|-------------|
| `--url` | empty | A2A エージェントのベース URL または JSON-RPC エンドポイント URL。config 由来でも可 |
| `--route` | repeatable | `--url` が省略された場合に使用される route spec |
| `--route-name` | empty | 選択する名前付きルート |
| `--prompt`, `-p` | required | プロンプトテキスト |
| `--cwd` | `.` | `message.metadata.iac_code.cwd` として送信されるワークスペースパス |
| `--context-id` | empty | フォローアップメッセージ用の既存 A2A context ID |
| `--verify-card-secret`, `--signing-secret` | empty | Agent Card 検証用の HMAC secret |
| `--verify-card-jwks-url` | empty | Agent Card 検証に使用されるリモート JWKS URL |
| `--require-card-signature`, `--require-signature` | `false` | 署名なしまたは無効な Agent Card を拒否 |
| `--timeout` | `30.0` | 呼び出しタイムアウト秒数 |
| `--stream` | `false` | `SendStreamingMessage` を使用し、ストリームイベントを出力 |

同じコンテキスト内のフォローアップ:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --context-id ctx-123 \
  --prompt "Now add outputs for the VPC and vSwitch IDs." \
  --cwd "$PWD"
```

ストリーミング:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Review this Terraform module." \
  --cwd "$PWD" \
  --stream
```

署名済み Agent Card を必須にする:

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Generate a production VPC template." \
  --cwd "$PWD"
```

リモート JWKS URL を使って検証する:

```bash
iac-code a2a-client --config jwks-client.yml call \
  --prompt "Review the ROS stack."
```

## `iac-code a2a-client discover`

リモート Agent Card を取得して出力します。

```bash
iac-code a2a-client --config a2a-client.yml discover
```

| Option | 説明 |
|--------|-------------|
| `--url` | A2A エージェントのベース URL。config 由来でも可 |
| `--verify-card-secret`, `--signing-secret` | 検証用 HMAC secret |
| `--verify-card-jwks-url` | 検証用リモート JWKS URL |
| `--require-card-signature`, `--require-signature` | 有効な署名を必須にする |

認証済みディスカバリー:

```bash
iac-code a2a-client --config a2a-client.yml discover
```

## タスクコマンド

タスクコマンドは JSON-RPC タスクメソッドを直接呼び出します。運用ツール、ダッシュボード、デバッグに便利です。

### `iac-code a2a-client task-get`

```bash
iac-code a2a-client --config a2a-client.yml task-get \
  --task-id task-123 \
  --history-length 20
```

| Option | 説明 |
|--------|-------------|
| `--url` | A2A JSON-RPC エンドポイント URL。config 由来でも可 |
| `--task-id` | タスク ID。config 由来でも可 |
| `--history-length` | 返すタスク履歴エントリの最大数 |

### `iac-code a2a-client task-list`

```bash
iac-code a2a-client --config a2a-client.yml task-list \
  --context-id ctx-123 \
  --status TASK_STATE_INPUT_REQUIRED \
  --page-size 20 \
  --output table
```

| Option | Default | 説明 |
|--------|---------|-------------|
| `--url` | empty | A2A JSON-RPC エンドポイント URL。config 由来でも可 |
| `--context-id` | empty | context ID でフィルター |
| `--status` | empty | タスク状態でフィルター |
| `--page-size` | empty | 返すタスクの最大数 |
| `--page-token` | empty | ページネーショントークン |
| `--include-artifacts` | `false` | 応答にタスクアーティファクトを含める |
| `--output` | `table` | `table` または `json` |

JSON 出力:

```bash
iac-code a2a-client --config a2a-client.yml task-list \
  --include-artifacts \
  --output json
```

### `iac-code a2a-client task-cancel`

```bash
iac-code a2a-client --config a2a-client.yml task-cancel \
  --task-id task-123
```

キャンセルは協調的です。完了済み、失敗済み、キャンセル済み、または input-required のタスクは、標準 A2A task-not-cancelable エラーを返します。

### `iac-code a2a-client task-subscribe`

```bash
iac-code a2a-client --config a2a-client.yml task-subscribe \
  --task-id task-123
```

このコマンドはアクティブタスクのイベントをストリーミングします。新しいターンでは `a2a-client call --stream` を優先してください。1 つのコマンドでタスクを開始し、更新をストリーミングします。

## プッシュ通知設定コマンド

これらのコマンドには、`push-notifications: true` で起動されたサーバーが必要です。標準 A2A タスクプッシュ通知設定を管理します。

### `iac-code a2a-client push-config-create`

```bash
iac-code a2a-client --config a2a-client.yml push-config-create \
  --task-id task-123 \
  --config-id webhook-1 \
  --callback-url https://hooks.example.com/a2a \
  --notification-token "$NOTIFICATION_TOKEN" \
  --auth-scheme bearer \
  --auth-credentials "$WEBHOOK_BEARER_TOKEN"
```

| Option | 説明 |
|--------|-------------|
| `--url` | A2A JSON-RPC エンドポイント URL。config 由来でも可 |
| `--task-id` | タスク ID。config 由来でも可 |
| `--config-id` | Push config ID。config 由来でも可 |
| `--callback-url` | HTTP(S) callback URL。config 由来でも可 |
| `--notification-token` | `X-A2A-Notification-Token` として送信されるトークン |
| `--auth-scheme` | `bearer` や `basic` などの callback auth scheme |
| `--auth-credentials` | callback auth credentials |

Callback URL は保存前と配送前に検証されます。デフォルトのバリデーターは、非 HTTP(S) URL、localhost 名、リテラルな private/local IP アドレスを拒否します。

### `iac-code a2a-client push-config-get`

```bash
iac-code a2a-client --config a2a-client.yml push-config-get \
  --task-id task-123 \
  --config-id webhook-1
```

### `iac-code a2a-client push-config-list`

```bash
iac-code a2a-client --config a2a-client.yml push-config-list \
  --task-id task-123 \
  --page-size 10
```

### `iac-code a2a-client push-config-delete`

```bash
iac-code a2a-client --config a2a-client.yml push-config-delete \
  --task-id task-123 \
  --config-id webhook-1
```

## `iac-code a2a-client extended-card`

認証済みの拡張 Agent Card を取得します。

```bash
iac-code a2a-client --config a2a-client.yml extended-card \
  --token "$A2A_TOKEN"
```

公開 Agent Card は `capabilities.extendedAgentCard=true` を広告します。拡張カードは、タスク管理やプッシュ設定機能メタデータを含む認証済みランタイム詳細を追加します。

## `iac-code a2a-route-preview`

`a2a-client call` が、`--url` が省略された場合に設定済みルートをどのように解決するかをプレビューします。

```bash
iac-code a2a-route-preview \
  --route "template=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --prompt "Create a ROS VPC template"
```

| Option | 説明 |
|--------|-------------|
| `--route` | `name=url;skills=a,b;tags=x,y` 形式の繰り返し可能な route spec |
| `--name` | 解決するルート名 |
| `--skill` | 解決する Skill ID |
| `--prompt` | 名前/タグ一致に使用されるプロンプトテキスト |
| `--route-state-dir`, `--persistence-dir` | ルートスナップショットの永続化に使用されるディレクトリ |
| `--save-routes` | 指定されたルートをルート状態ディレクトリに保存 |

ルートスナップショットを保存する:

```bash
iac-code a2a-route-preview \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros" \
  --route-state-dir ~/.iac-code/a2a \
  --save-routes
```

ルート経由で呼び出す:

```bash
iac-code a2a-client call \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros" \
  --route-name ros \
  --prompt "Create a ROS VPC template." \
  --cwd "$PWD"
```

## 環境変数

| Variable | 説明 |
|----------|-------------|
| `IACCODE_A2A_HTTP_TOKEN` | サーバー/クライアント Bearer token のデフォルト |
| `IACCODE_A2A_BASIC_USERNAME` | サーバー/クライアント Basic auth username のデフォルト |
| `IACCODE_A2A_BASIC_PASSWORD` | サーバー/クライアント Basic auth password のデフォルト |
| `IACCODE_A2A_API_KEY` | サーバー/クライアント API key のデフォルト |
| `IACCODE_A2A_API_KEY_HEADER` | API key header name のデフォルト |
| `IACCODE_A2A_ALLOWED_CWDS` | 受信メッセージメタデータと file URL に許可されるワークスペースルートの OS パス区切りリスト |
| `IACCODE_A2A_TEXT_MIME_TYPES` | 追加のカンマ区切りまたはセミコロン区切りのテキスト風 MIME types |
| `IACCODE_A2A_MULTIMODAL_MIME_TYPES` | 追加のカンマ区切りまたはセミコロン区切りのマルチモーダル MIME types |
| `IAC_CODE_A2A_PUSH_KEYRING` | 環境管理の暗号化プッシュシークレット keyring |
