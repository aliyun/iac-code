---
title: プロトコルリファレンス
description: iac-code 統合のための完全な A2A プロトコルリファレンス。
sidebar_position: 4
---

# プロトコルリファレンス

このドキュメントでは、iac-code サーバーが公開する A2A 1.0 の範囲と、`iac-code a2a-client call` で使用される Phase 1 クライアントの挙動を説明します。正確な CLI オプションについては、[コマンドリファレンス](./command-reference.md)を参照してください。

## ライフサイクル概要

典型的な A2A インタラクションは次の流れに従います。

```text
GET Agent Card -> SendMessage or SendStreamingMessage -> GetTask / follow-up / CancelTask
```

1. **発見** — `/.well-known/agent-card.json` を取得します。
2. **送信** — `/` の JSON-RPC エンドポイントへテキストメッセージを送信します。
3. **ストリーム** — `Task`、`Message`、`TaskStatusUpdateEvent` ペイロードを受信します。
4. **継続** — 同じ `contextId` でフォローアップメッセージを送信します。
5. **キャンセルまたは照会** — `CancelTask`、`GetTask`、または `ListTasks` を使用します。

## Agent Card

Agent Card は次の場所で利用できます。

```text
GET /.well-known/agent-card.json
```

重要なフィールド:

| Field | Value | 意味 |
|-------|-------|---------|
| `name` | `iac-code` | エージェント名 |
| `supportedInterfaces[0].protocolBinding` | `JSONRPC` | トランスポートバインディング |
| `supportedInterfaces[0].protocolVersion` | `1.0` | A2A プロトコルバージョン |
| `supportedInterfaces[0].url` | `http://<host>:<port>/` | JSON-RPC エンドポイント |
| `capabilities.streaming` | `true` | ストリーミングタスク更新をサポート |
| `capabilities.pushNotifications` | `false` or `true` | `true` は `push-notifications: true` が設定されている場合 |
| `capabilities.extendedAgentCard` | `true` | 認証済み呼び出し元は拡張ランタイム詳細をリクエスト可能 |
| `capabilities.extensions` | `urn:iac-code:a2a:artifact-metadata:v1` | ツール状態と保存済みアーティファクトメタデータのための任意の iac-code メタデータ名前空間 |
| `defaultInputModes` | text, JSON, YAML, image, audio, and binary MIME types | 受け付ける入力 MIME モード |
| `defaultOutputModes` | `["text/plain"]` | テキスト出力のみ |

Agent Card 応答には `Cache-Control: public, max-age=60`、`ETag`、`Last-Modified` が含まれます。クライアントは `If-None-Match` を送信でき、カードが変更されていない場合は `304 Not Modified` を受け取ります。

広告されるスキル:

| Skill ID | 用途 |
|----------|---------|
| `iac_generation` | 自然言語から Alibaba Cloud ROS と Terraform テンプレートを生成 |
| `iac_review` | IaC テンプレートを検査し修正を提案 |
| `aliyun_ros_operations` | Alibaba Cloud ROS スタックワークフローを支援 |
| `terraform_ros_conversion` | バンドルされたスキルリソースを使用して Terraform から ROS への変換を支援 |

認証が有効な場合、Agent Card は設定済みのセキュリティ方式を広告します。

| Scheme | 広告される条件 |
|--------|-----------------|
| `bearerAuth` | `token` または `IACCODE_A2A_HTTP_TOKEN` が設定されている |
| `basicAuth` | Basic username と password の両方が設定されている |
| `apiKeyAuth` | `api-key` または `IACCODE_A2A_API_KEY` が設定されている |

## ルート

| Route | Method | 説明 |
|-------|--------|-------------|
| `/health` | `GET` | `{"status":"healthy"}` を返す |
| `/.well-known/agent-card.json` | `GET` | Agent Card を返す |
| `/` | `POST` | A2A JSON-RPC リクエストを処理 |
| REST routes | mixed | `create_rest_routes` によって登録される A2A SDK REST ルート |

## Phase 1 クライアントとトランスポートの注意点

デフォルトの相互運用可能な Phase 1 トランスポートは、HTTP 上の JSON-RPC です。HTTP モードは SDK REST ルート向けに `HTTP+JSON` も広告します。

サーバーには、stdio、Unix ソケット、WebSocket、公式 gRPC、gRPC JSON-RPC エンベロープ、Redis Streams の任意トランスポートもあります。stdio、Unix ソケット、WebSocket、gRPC JSON-RPC、Redis Streams はカスタム JSON-RPC トランスポートです。公式 gRPC は `grpc` として広告され、任意の gRPC 依存関係が必要です。

組み込みクライアントは、メッセージ呼び出しの前に Agent Card ディスカバリー (`GET /.well-known/agent-card.json`) を使用し、最初に広告された実行可能な `supportedInterfaces[].url` を選択してから、`A2A-Version: 1.0` と `SendMessage` などの A2A 1.0 メソッド名を使って JSON-RPC リクエストを送信します。

`push-notifications: true` は A2A プッシュ通知設定メソッドと終端状態の配信を有効にします。

Agent Card 署名は A2A SDK の署名ユーティリティを使用し、標準の `AgentCardSignature` JWS フィールドを出力します。対称キーモードは `HS256` を使用します。検証では、保護ヘッダー `kid` による設定済みシークレット、ローカル octet-key JWKS、またはリモート JWKS URL を選択できます。サーバー側の非対称署名と自動キーローテーションは Phase 1 では実装されていません。

Phase 1 で未サポートの挙動の標準的な一覧は、[A2A プロトコル](./overview.md#phase-1-unsupported)を参照してください。

## プッシュ通知配信バックエンド

`iac-code a2a --config a2a-server.yml` は 2 つのプッシュ配信キューをサポートします。

- `push-queue: local-file` は A2A 永続化ディレクトリの下にジョブを保存し、ローカルの単一ノード利用を想定しています。
- `push-queue: redis-streams` は Redis Streams にジョブを保存し、Redis consumer group を通じてワーカーを調整します。

Redis ベースのプッシュ配信には任意の `a2a-redis` extra が必要で、at-least-once です。ワーカーのクラッシュ、リース期限切れ、再接続、リトライ競合の後にジョブが再配信される可能性があるため、コールバック受信側はタスク更新を冪等に処理する必要があります。

一般的な Redis オプション:

```yaml
push-notifications: true
push-queue: redis-streams
push-redis-url: redis://localhost:6379/0
push-stream: iac-code:a2a:push
push-retry-key: iac-code:a2a:push:retry
push-dead-stream: iac-code:a2a:push:dead
push-consumer-group: iac-code-push
push-consumer-name: worker-1
push-lease-timeout-ms: 300000
```

Callback URL は保存前と配送前に検証されます。デフォルトのバリデーターは、非 HTTP(S) URL、localhost ホスト名、リテラルな private/local IP アドレスを拒否します。コールバック受信側は、それでも独自の認証と冪等性ポリシーを適用する必要があります。

## JSON-RPC メソッド

### SendMessage

非ストリーミング A2A メッセージターンを実行します。応答には、ターン完了後のタスクまたはメッセージが含まれます。

**リクエスト**

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "msg-1",
      "role": "ROLE_USER",
      "parts": [{"text": "Create a VPC with two vSwitches."}],
      "metadata": {
        "iac_code": {"cwd": "/absolute/path/to/project"}
      }
    },
    "configuration": {
      "acceptedOutputModes": ["text/plain"]
    }
  }
}
```

**必須メッセージフィールド**

| Field | Type | Required | 説明 |
|-------|------|----------|-------------|
| `messageId` | string | Yes | 一意のクライアントメッセージ ID |
| `role` | string | Yes | ユーザー入力には `ROLE_USER` を使用 |
| `parts` | array | Yes | テキスト風、JSON データ、生テキスト、ローカルファイル URL、または制限付きマルチモーダルパーツ |
| `metadata.iac_code.cwd` | string | Recommended | 絶対ワークスペースパス。省略時はサーバープロセスディレクトリがデフォルト |

`metadata.iac_code.cwd` が指定された場合、既存の絶対ディレクトリである必要があります。許可されたワークスペースルート内になければなりません。デフォルトでは、許可されるルートはサーバープロセスディレクトリとシステム一時ディレクトリです。`IACCODE_A2A_ALLOWED_CWDS` で、OS パス区切りの許可リストを指定できます。

サポートされる入力カテゴリ:

| Category | Accepted Shape | 制限と挙動 |
|----------|----------------|---------------------|
| テキスト風パーツ | `text` with `text/plain`, JSON, Markdown, YAML, or configured extra text MIME types | プロンプトへ直接追加 |
| JSON データパーツ | `data` with `application/json` | コンパクト JSON にシリアライズ。インライン最大 1 MiB |
| 生テキストパーツ | `raw` with a text-like MIME type | 有効な UTF-8 である必要あり。インライン最大 1 MiB |
| ローカルテキストファイル URL | `url` with `file://...` and text-like MIME type | ファイルは `cwd` と許可済みルート内に存在する必要あり。最大 1 MiB |
| マルチモーダル raw/data/file パーツ | image, audio, or configured multimodal MIME types | ファイル名、メディアタイプ、バイトサイズ、ハッシュ、ソースを含むプロンプトマニフェストに変換。raw/data は最大 5 MiB、file URL は最大 25 MiB |

リモート HTTP(S) URL 取り込みはサポートされません。File URL パーツはローカル `file://` URL を使用し、許可されたワークスペース内に留まる必要があります。

### SendStreamingMessage

ストリーミング A2A メッセージターンを実行します。リクエスト本文は `SendMessage` と同じ形ですが、サーバーは JSON-RPC 応答を Server-Sent Events としてストリーミングします。

```json
{
  "jsonrpc": "2.0",
  "id": "2",
  "method": "SendStreamingMessage",
  "params": {
    "message": {
      "messageId": "msg-2",
      "role": "ROLE_USER",
      "parts": [{"text": "Review this ROS template."}],
      "metadata": {
        "iac_code": {"cwd": "/absolute/path/to/project"}
      }
    },
    "configuration": {
      "acceptedOutputModes": ["text/plain"]
    }
  }
}
```

### GetTask

保存された A2A タスクを ID で返します。保存済みタスク履歴を変更せずに返却履歴を制限するには `historyLength` を使用します。省略すると、サーバーの現在のデフォルト履歴を受け取ります。

```json
{
  "jsonrpc": "2.0",
  "id": "3",
  "method": "GetTask",
  "params": {
    "id": "task-id",
    "historyLength": 10
  }
}
```

### ListTasks

認証済み呼び出し元に見える既知のタスクを返します。結果は、安定した順序になるように、ステータスタイムスタンプの降順、次にタスク ID の降順でソートされます。サーバーは `contextId`、`status`、`pageSize`、`pageToken`、`historyLength`、`includeArtifacts` をサポートします。

```json
{
  "jsonrpc": "2.0",
  "id": "4",
  "method": "ListTasks",
  "params": {
    "contextId": "ctx-id",
    "status": "TASK_STATE_WORKING",
    "pageSize": 20,
    "includeArtifacts": false
  }
}
```

別のページが利用可能な場合、`nextPageToken` が返されます。`includeArtifacts` のデフォルトは `false` のため、明示的に要求しない限り、一覧応答ではタスクアーティファクトが省略されます。

### CancelTask

実行中タスクのキャンセルをリクエストします。

```json
{
  "jsonrpc": "2.0",
  "id": "5",
  "method": "CancelTask",
  "params": {
    "id": "task-id"
  }
}
```

タスクがアクティブな場合、サーバーは実行中のエージェントターンをキャンセルし、キャンセル済みタスク状態を出力します。タスクは存在するが実行中でない場合、サーバーは標準 A2A `TaskNotCancelableError` を返します。

### SubscribeToTask

クライアントトランスポートでサポートされる場合、アクティブタスク更新ストリームを購読します。

```json
{
  "jsonrpc": "2.0",
  "id": "6",
  "method": "SubscribeToTask",
  "params": {
    "id": "task-id"
  }
}
```

アクティブなタスクでは、ストリームは現在の `Task` で開始し、その後のタスクイベントを出力し、アクティブターンが終了すると閉じます。完了済み、失敗済み、キャンセル済み、または input-required のタスクを購読すると、無期限に待つのではなく task-not-found 風のエラーが返されます。新しいターンでは、`SendStreamingMessage` を優先してください。1 つのリクエストで実行を開始し、応答をストリーミングします。

### プッシュ通知設定メソッド

サーバーが `push-notifications: true` で起動した場合、次をサポートします。

| Method | 用途 |
|--------|---------|
| `CreateTaskPushNotificationConfig` | タスクのコールバック設定を保存 |
| `GetTaskPushNotificationConfig` | 1 つのコールバック設定を取得 |
| `ListTaskPushNotificationConfigs` | タスクのコールバック設定を一覧表示 |
| `DeleteTaskPushNotificationConfig` | コールバック設定を削除 |

作成リクエストの例:

```json
{
  "jsonrpc": "2.0",
  "id": "7",
  "method": "CreateTaskPushNotificationConfig",
  "params": {
    "taskId": "task-id",
    "id": "webhook-1",
    "url": "https://hooks.example.com/a2a",
    "token": "notification-token",
    "authentication": {
      "scheme": "bearer",
      "credentials": "callback-token"
    }
  }
}
```

ローカルプッシュ keyring が利用可能な場合、サーバーは保存された通知トークンとコールバック認証情報を暗号化します。

### GetExtendedAgentCard

認証済みクライアントは拡張 Agent Card をリクエストできます。

```json
{
  "jsonrpc": "2.0",
  "id": "8",
  "method": "GetExtendedAgentCard",
  "params": {}
}
```

拡張カードには、公開カードに加えて認証済みランタイム詳細が含まれます。

## タスクとコンテキストの挙動

iac-code は A2A コンテキストを内部エージェントランタイムにマッピングします。

| Concept | 挙動 |
|---------|----------|
| `contextId` omitted | SDK/サーバーが新しいコンテキスト ID を生成 |
| Same `contextId` | 同じ内部 iac-code セッションと会話状態を再利用 |
| Same `contextId`, different `cwd` | 異なるワークスペースとして拒否 |
| Same `contextId`, concurrent message | `Task is already working.` で拒否 |
| Different `contextId` values | 並行実行が可能 |
| Idle context | 設定されたアイドルタイムアウト後にメモリから削除 |

タスク ID とコンテキスト ID は空でなく、最大 128 文字で、文字、数字、`_`、`.`、`:`、`-` のみを含む必要があります。

## タスク状態

| State | 意味 |
|-------|---------|
| `TASK_STATE_SUBMITTED` | タスクが受理された |
| `TASK_STATE_WORKING` | iac-code がエージェントターンを実行中 |
| `TASK_STATE_INPUT_REQUIRED` | ターンが完了し、エージェントがフォローアップ入力を受けられる状態 |
| `TASK_STATE_CANCELED` | キャンセルが要求され適用された |
| `TASK_STATE_FAILED` | タスクが検証または実行に失敗した |

コンテキストがフォローアップメッセージで利用可能なまま残るため、iac-code は通常の完了状態として `TASK_STATE_INPUT_REQUIRED` を使用します。

## ストリーミング更新

実行中、iac-code は `TaskStatusUpdateEvent` 更新を出力します。

アシスタントのテキストはステータスメッセージとして配信されます。

```json
{
  "statusUpdate": {
    "taskId": "task-1",
    "contextId": "ctx-1",
    "status": {
      "state": "TASK_STATE_WORKING",
      "message": {
        "role": "ROLE_AGENT",
        "parts": [{"text": "Here is the ROS template..."}]
      }
    }
  }
}
```

ツールと使用量の詳細は `metadata.iac_code` を通じて配信されます。

| Metadata Path | 説明 |
|---------------|-------------|
| `iac_code.tool.status` | `started`, `input_delta`, `input_complete`, `completed`, or `failed` |
| `iac_code.tool.toolUseId` | ツールイベントを関連付ける安定した tool-use ID |
| `iac_code.tool.name` | 利用可能な場合のツール名 |
| `iac_code.tool.inputSummary` | 完了したツール入力の形状のみの要約。文字列値とホワイトリスト外のフィールド名は、生の業務 payload を含めずに表されます |
| `iac_code.tool.result` | ツール結果。フィールドごとに 4000 文字へ切り詰め |
| `iac_code.permission.autoApproved` | A2A が権限リクエストを自動承認または resolver 経由で承認したかどうか |
| `iac_code.permission.toolName` | 権限を要求したツール |
| `iac_code.permission.safeSummary` | マスク済みの人間向け権限サマリー |
| `iac_code.permission.inputSummary` | `aliyun_api` 権限リクエスト向けの安全な操作サマリー |
| `iac_code.permission.toolInput` | `aliyun_api` 以外の権限リクエスト向けの形状のみのツール入力。文字列値は型、長さ、フィンガープリントで表され、ホワイトリスト外のフィールド名もフィンガープリントで表される場合があります |
| `iac_code.thinking.type` | `thinking-exposure` で `raw-thinking` が有効な場合は `raw_thinking` |
| `iac_code.thinking.text` | 生の provider 推論チャンク。4000 文字に切り詰められ、信頼済み設定で `raw-thinking` が有効な場合のみ出力 |
| `iac_code.usage.inputTokens` | ターンの入力トークン数 |
| `iac_code.usage.outputTokens` | ターンの出力トークン数 |
| `iac_code.usage.totalTokens` | ターンの合計トークン数 |

ツール結果にサポート対象のテキストアーティファクトペイロードが含まれる場合、サーバーはペイロードをローカルに保存し、標準の `TaskArtifactUpdateEvent` を出力し、タスクの `artifacts` フィールドにアーティファクトを記録します。アーティファクトパーツは、`file://` URL と `mediaType`、`byteSize`、`sha256` などのメタデータを使用します。元のアーティファクト内容はツールメタデータ内に重複して含まれません。

## 拡張

Agent Card は任意の iac-code アーティファクトメタデータ拡張を広告します。

```text
urn:iac-code:a2a:artifact-metadata:v1
```

この拡張は、ツール進行状況、権限判断、トークン使用量、ローカルアーティファクトメタデータに使用される `metadata.iac_code` 名前空間を識別します。サーバーが必須拡張を設定している場合、クライアントはその URI を `A2A-Extensions` ヘッダーに含める必要があります。必須拡張がない場合、標準 A2A `ExtensionSupportRequiredError` が返されます。

`thinking-exposure` が 1 つ以上の信号タイプを有効にしている場合、Agent Card は次も広告します。

```text
urn:iac-code:a2a:thinking-exposure:v1
```

拡張 params には `enabledTypes` が含まれます。例：`["tool_trace", "raw_thinking"]`。これは iac-code メタデータ拡張であり、A2A コアのストリーミングイベントタイプではありません。

## エラー処理

| シナリオ | 結果 |
|----------|--------|
| 空のテキスト入力 | `TASK_STATE_FAILED` with `A2A server currently accepts text input only.` |
| サポートされないメディアタイプ | SDK がリクエストを拒否する場所に応じて、検証エラーまたは標準 A2A content-type エラー |
| リモート URL パーツ | URL パーツはローカル `file://` URL を使用する必要があるため検証エラー |
| 許可されたワークスペース外の File URL | 検証エラー |
| 必須 A2A 拡張がない | 標準 A2A `ExtensionSupportRequiredError` |
| 不正なワークスペースメタデータ | invalid workspace メッセージ付きの `TASK_STATE_FAILED` |
| 認証がない、または不正 | HTTP `401` with `{"error":"Unauthorized"}` |
| A2A サーバー依存関係がない | CLI は `a2a` extra のインストールヒントを表示して終了 |
| プロバイダー認証情報がない | サニタイズされた認証エラー |
| 予期しないランタイムエラー | サニタイズされた内部エラー |

サーバーは、予期しないエラーメッセージでローカルパス、シークレット、プロバイダー詳細を返さないようにします。
