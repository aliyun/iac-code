---
title: プロトコルリファレンス
description: iac-code 統合のための完全な ACP プロトコルメソッドとイベントリファレンス。
sidebar_position: 3
---

# プロトコルリファレンス

このドキュメントでは、iac-code サーバーが公開する ACP（Agent Client Protocol）のメソッドとストリーミングイベントの完全なリファレンスを提供します。

## ライフサイクル概要

典型的な ACP セッションは以下のフローに従います：

```
initialize → new_session → prompt (loop) → close_session
                ↑                              │
                └── load_session / resume ──────┘
```

1. **initialize** — ハンドシェイク。プロトコルバージョンのネゴシエーションとサーバー機能の検出。
2. **session/new** — 独立したエージェントランタイムで新しいセッションを作成。
3. **session/prompt** — ユーザー入力を送信し、最終レスポンスまでストリーミングイベントを受信。
4. **session/close** — セッションとそのリソースを解放。

新規作成の代わりに、履歴からセッションを読み込む（`session/load`）か再開する（`session/resume`）こともできます。

---

## メソッド

### initialize

プロトコルハンドシェイク。すべての接続で最初の呼び出しである必要があります。

**リクエストパラメータ**

| フィールド | 型 | 必須 | 説明 |
|-------|------|----------|-------------|
| `protocolVersion` | integer | はい | 要求するプロトコルバージョン（現在 `1`） |
| `clientInfo` | object | いいえ | クライアント名とバージョン |
| `clientCapabilities` | object | いいえ | クライアントがサポートする機能 |

**レスポンスフィールド**

| フィールド | 型 | 説明 |
|-------|------|-------------|
| `protocolVersion` | integer | ネゴシエートされたプロトコルバージョン |
| `agentCapabilities` | object | サーバー機能（下記参照） |
| `agentInfo` | object | サーバー名とバージョン |
| `authMethods` | array | 利用可能な認証方法（組み込み認証情報を使用する場合は空） |

**エージェント機能**

| 機能 | 値 | 意味 |
|-----------|-------|---------|
| `loadSession` | `true` | 履歴からのセッション復元をサポート |
| `promptCapabilities.embeddedContext` | `true` | プロンプトに埋め込みリソースコンテンツを受け入れ |
| `promptCapabilities.image` | `false` | 画像入力は未サポート（テキストマーカーにフォールバック） |
| `promptCapabilities.audio` | `false` | 音声入力は未サポート（テキストマーカーにフォールバック） |
| `sessionCapabilities.list` | `{}` | セッション一覧をサポート |
| `sessionCapabilities.close` | `{}` | セッションのクローズをサポート |

---

### session/new

独立したエージェントランタイム、ツールレジストリ、LLM コンテキストを持つ新しいセッションを作成します。

**リクエストパラメータ**

| フィールド | 型 | 必須 | 説明 |
|-------|------|----------|-------------|
| `cwd` | string | はい | 作業ディレクトリの絶対パス |
| `mcpServers` | object | いいえ | MCP サーバー設定（受け入れられますがまだ機能しません） |

**レスポンスフィールド**

| フィールド | 型 | 説明 |
|-------|------|-------------|
| `sessionId` | string | 以降の呼び出しで使用する一意のセッション識別子 |
| `modes` | object | 利用可能なモードと現在のモード |
| `models` | object | 利用可能なモデルと現在のモデル |

:::note
各セッションは独立した AgentLoop を作成します。複数のセッションを同時に実行できますが、それぞれが LLM 接続を消費します。
:::

---

### session/load

以前に永続化されたセッションをディスクから読み込み、メッセージ履歴を復元します。

**リクエストパラメータ**

| フィールド | 型 | 必須 | 説明 |
|-------|------|----------|-------------|
| `cwd` | string | はい | 作業ディレクトリのパス |
| `sessionId` | string | はい | 読み込むセッションの ID |

**レスポンスフィールド**

| フィールド | 型 | 説明 |
|-------|------|-------------|
| `models` | object | 利用可能なモデルと現在のモデル状態 |
| `modes` | object | 利用可能なモードと現在のモード状態 |

:::note
セッションの読み込みは `~/.iac-code/sessions/` から履歴を読み取り、中断されたメッセージを自動修復し、新しい AgentLoop に履歴を注入します。
:::

---

### session/fork

既存のセッションをフォークして、同じ履歴を持つ独立したブランチを作成します。

**リクエストパラメータ**

| フィールド | 型 | 必須 | 説明 |
|-------|------|----------|-------------|
| `cwd` | string | はい | 作業ディレクトリのパス |
| `sessionId` | string | はい | フォークするセッションの ID |

**レスポンスフィールド**

| フィールド | 型 | 説明 |
|-------|------|-------------|
| `sessionId` | string | フォークされたブランチの新しいセッション ID |
| `models` | object | 利用可能なモデルと現在のモデル状態 |
| `modes` | object | 利用可能なモードと現在のモード状態 |

---

### session/resume

既存のセッションを再開または再接続します。必要に応じて自動的に履歴を読み込みます。

**リクエストパラメータ**

| フィールド | 型 | 必須 | 説明 |
|-------|------|----------|-------------|
| `cwd` | string | はい | 作業ディレクトリのパス |
| `sessionId` | string | はい | 再開するセッションの ID |

**レスポンスフィールド**

| フィールド | 型 | 説明 |
|-------|------|-------------|
| `models` | object | 利用可能なモデルと現在のモデル状態（任意） |
| `modes` | object | 利用可能なモードと現在のモード状態（任意） |

:::note
`session/new` とは異なり、クライアントはリクエストからセッション ID を既に知っているため、レスポンスに `sessionId` フィールドは含まれません。
:::

---

### session/prompt

ユーザー入力を送信し、ストリーミングエージェントレスポンスをトリガーします。

**リクエストパラメータ**

| フィールド | 型 | 必須 | 説明 |
|-------|------|----------|-------------|
| `sessionId` | string | はい | ターゲットセッション ID |
| `prompt` | array | はい | コンテンツブロックの配列（下記のコンテンツブロックタイプを参照） |

**コンテンツブロックタイプ**

| タイプ | 説明 |
|------|-------------|
| `TextContentBlock` | プレーンテキストのユーザー入力 |
| `EmbeddedResourceContentBlock` | インラインに埋め込まれたファイルコンテンツ |
| `ResourceContentBlock` | リソースリンク参照 |
| `ImageContentBlock` | 画像（`[image: mime/type]` テキストマーカーにフォールバック） |
| `AudioContentBlock` | 音声（`[audio: mime/type]` テキストマーカーにフォールバック） |

**レスポンスフィールド**

| フィールド | 型 | 説明 |
|-------|------|-------------|
| `stopReason` | string | プロンプトが完了した理由（停止理由を参照） |
| `usage` | object | トークン使用量：`inputTokens`、`outputTokens`、`totalTokens` |

**停止理由**

| 値 | 意味 |
|-------|---------|
| `end_turn` | モデルが正常に完了 |
| `max_turn_requests` | ツール呼び出しループの最大制限に到達 |
| `max_tokens` | 出力トークン制限に到達 |
| `cancelled` | クライアントがプロンプトをキャンセル |
| `refusal` | モデルが回答を拒否 |

:::note
実行中、サーバーは最終レスポンスを返す前にストリーミングイベントを含む `session/update` 通知をプッシュします。
:::

---

### session/cancel

実行中のプロンプトタスクをキャンセルします。

**リクエストパラメータ**

| フィールド | 型 | 必須 | 説明 |
|-------|------|----------|-------------|
| `sessionId` | string | はい | 実行中のプロンプトがあるセッション |

**動作**

- ストリームイベントの消費を停止します
- 実行中のツールは強制終了されませんが、結果は破棄されます
- 保留中の `prompt` 呼び出しは `stopReason: "cancelled"` で返されます

---

### session/close

セッションを閉じてリソースを解放します。

**リクエストパラメータ**

| フィールド | 型 | 必須 | 説明 |
|-------|------|----------|-------------|
| `sessionId` | string | はい | 閉じるセッション |

**動作**

- セッションがメモリから削除されます
- 永続化された履歴はディスクに残ります
- このセッションへの以降の `prompt` 呼び出しはエラーを返します

---

### sessions/list

指定された作業ディレクトリのすべての永続化されたセッションを一覧表示します。

**リクエストパラメータ**

| フィールド | 型 | 必須 | 説明 |
|-------|------|----------|-------------|
| `cwd` | string | はい | 一覧のスコープとなる作業ディレクトリ |

**レスポンスフィールド**

| フィールド | 型 | 説明 |
|-------|------|-------------|
| `sessions` | array | `sessionId` とメタデータを持つセッションオブジェクトのリスト |

---

### config/set

セッションの設定オプションを動的に設定します。

**リクエストパラメータ**

| フィールド | 型 | 必須 | 説明 |
|-------|------|----------|-------------|
| `sessionId` | string | はい | ターゲットセッション |
| `configId` | string | はい | 設定するコンフィグキー |
| `value` | any | はい | 新しい値 |

---

## ストリーミングイベント

`session/prompt` の実行中、サーバーはストリーミングイベントデータを含む `session/update` 通知をプッシュします。

### イベント形式

各 `session/update` 通知は特定のタイプを持つ更新オブジェクトを運びます：

```json
{
  "jsonrpc": "2.0",
  "method": "session/update",
  "params": {
    "sessionId": "abc123",
    "update": { "type": "agent_message_chunk", "text": "..." }
  }
}
```

### イベントタイプマッピング

| 内部イベント | ACP 更新タイプ | 説明 |
|---------------|----------------|-------------|
| `TextDeltaEvent` | `AgentMessageChunk` | エージェントテキストの増分出力 |
| `ThinkingDeltaEvent` | `AgentThoughtChunk` | モデルの推論/思考コンテンツ |
| `ToolUseStartEvent` | `ToolCallStart` | ツール呼び出しの開始 |
| `ToolResultEvent` | `ToolCallProgress` | ツール結果（完了または失敗） |
| `CompactionEvent` | `AgentMessageChunk` | コンテキストコンパクション通知 |
| `ErrorEvent` | `AgentMessageChunk` | エラー情報 |

### ツール呼び出しのライフサイクル

```
ToolCallStart (status=in_progress)
    │
    ├── ToolCallProgress (status=in_progress, 入力要約 / 安全なプロンプト入力)
    │
    ├── ToolCallProgress (status=completed, raw_output=result)   ← success
    │
    └── ToolCallProgress (status=failed, raw_output=error)       ← failure
```

**ツール種別マッピング**

| ツール | ACP ToolKind |
|------|-------------|
| `read_file`, `list_files` | `read` |
| `glob`, `grep` | `search` |
| `write_file`, `edit_file` | `edit` |
| `bash`, `agent` | `execute` |
| `web_fetch` | `fetch` |
| その他 | `other` |

---

## 権限リクエスト

高リスクのツールを実行する前に、iac-code はクライアントに `request_permission` コールバックを送信します。

### ツール権限カテゴリ

| カテゴリ | ツール | 自動許可 |
|----------|-------|-------------|
| 読み取り専用 | `read_file`, `list_files`, `glob`, `grep`, `web_fetch` | はい |
| 読み取り専用クラウド API | 読み取り専用に分類された `aliyun_api` アクション | はい |
| 書き込み | `write_file`, `edit_file` | いいえ — 承認が必要 |
| 実行 | `bash`, `agent` | いいえ — 承認が必要 |
| クラウド書き込み API | 読み取り専用ではない `aliyun_api` 呼び出し | いいえ — API ごとの承認が必要 |

### request_permission イベント

サーバーは以下を含む `request_permission` コールバックを送信します：

| フィールド | 型 | 説明 |
|-------|------|-------------|
| `options` | array | 利用可能な権限選択肢 |
| `sessionId` | string | 権限を要求するセッション |
| `toolCall` | object | ツール呼び出しの詳細（タイトル、種別、入力） |

### 権限オプション

| オプション ID | 意味 |
|-----------|---------|
| `allow_once` | この特定の呼び出しを許可 |
| `allow_always` | ツールが blanket allow をサポートする場合のみ、このセッション内でこのツールの今後のすべての呼び出しを許可（`bash` と `aliyun_api` ではデフォルトで表示されません） |
| `allow_rule:<rules>` | 提案されたルールに一致する今後の呼び出しをこのセッション内で許可 |
| `deny_rule:<rules>` | 提案されたルールに一致する今後の呼び出しをこのセッション内で拒否 |
| `reject_once` | この特定の呼び出しを拒否 |
| `reject_always` | このセッション内でこのツールの今後のすべての呼び出しを拒否 |

`aliyun_api` では、読み取り専用アクションは自動的に許可されます。読み取り専用ではない RPC および ROA アクションでは、`aliyun_api(ros:CreateStack)` や `aliyun_api(cs:CreateCluster)` のような正確なルールを提示できます。ワイルドカードの allow ルールは、読み取り専用ではない呼び出しを引き続き承認しません。

### レスポンス形式

```json
{
  "outcome": "allowed",
  "option_id": "allow_once"
}
```

拒否する場合：

```json
{
  "outcome": "denied"
}
```

| クライアントレスポンス | ツールの動作 |
|----------------|---------------|
| `AllowedOutcome` | ツールは正常に実行されます |
| `DeniedOutcome` | ツールはスキップされ、モデルは "Permission denied." エラーを受け取ります |

---

## エラー処理

### RequestError 形式

エラーは JSON-RPC 2.0 エラー形式に従います：

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "error": {
    "code": -32602,
    "message": "Invalid params",
    "data": {"session_id": "Session not found"}
  }
}
```

### 一般的なエラーコード

| コード | 名前 | 説明 |
|------|------|-------------|
| `-32700` | Parse error | 無効な JSON |
| `-32600` | Invalid request | 不正な JSON-RPC |
| `-32601` | Method not found | 不明なメソッド |
| `-32602` | Invalid params | パラメータの欠落または無効（例：不明なセッション ID） |
| `-32603` | Internal error | サーバー側の障害 |
