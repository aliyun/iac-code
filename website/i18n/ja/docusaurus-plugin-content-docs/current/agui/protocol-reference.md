---
sidebar_position: 3
title: プロトコルリファレンス
description: iac-code AG-UI のリクエスト、イベント、Interrupt、Resume、キャンセル、永続化。
---

# AG-UI プロトコルリファレンス

`iac-code agui` の HTTP/SSE インターフェースと、標準 AG-UI envelope 内の iac-code 拡張を説明します。先に[概要](./overview.md)と[クイックスタート](./getting-started.md)を参照してください。

## HTTP エンドポイント

| メソッドとパス | 用途 |
|----------------|------|
| `GET /health` | ヘルスとプロトコルバージョン |
| `POST /` | `RunAgentInput` を送信し SSE を受信 |
| `POST /extensions/iac-code/v1/executions/{executionId}/cancel` | 名前空間付きキャンセル拡張 |

`POST /` は JSON で送信し、SSE を要求します。

```http
Content-Type: application/json
Accept: text/event-stream
```

`IAC_CODE_AGUI_AUTH_TOKEN` を設定した場合：

```http
Authorization: Bearer <token>
```

標準 `Accept-Language` はエラーメッセージ言語のフォールバックです。`forwardedProps.iacCode.preferredLanguage` が優先され、A2A runtime にも転送されます。

## RunAgentInput

```json
{
  "threadId": "8473547e-c8ed-4aef-a84c-603a6a8d42da",
  "runId": "32c263f2-b0b0-42ac-905c-524a0a9bb652",
  "state": {},
  "messages": [{"id": "message-1", "role": "user", "content": "VPC テンプレートを作成"}],
  "tools": [],
  "context": [],
  "forwardedProps": {
    "iacCode": {
      "schemaVersion": 1,
      "rosInvocationId": "invocation-1",
      "cwd": "/workspace/session-1",
      "runMode": "normal"
    }
  }
}
```

| 標準フィールド | 要件と動作 |
|----------------|------------|
| `threadId` | 必須。会話中安定し、A2A context と iac-code session に対応 |
| `runId` | 必須。HTTP/SSE 実行ごとに一意 |
| `parentRunId` | 任意。`RUN_STARTED` へコピー |
| `state` | 必須。標準 envelope に保持するが runtime 状態源にはしない |
| `messages` | 必須。新規 run は最新 user message を使用 |
| `tools` | 必須かつ空配列。クライアント定義ツールは未対応 |
| `context` | 必須。現在は prompt context へ変換しない |
| `forwardedProps` | 必須。`iacCode` 拡張を含める |
| `resume` | Resume 時に使用。保留中 Interrupt ごとの回答 |

ユーザーメッセージは文字列、`text` part、base64 `data` source の `image` part に対応します。リモート画像 URL、音声、動画、document、汎用 binary は未対応です。画像 1 件はデコード後 8 MiB、合計 10 MiB、HTTP リクエスト全体は 12 MiB が上限です。

## `forwardedProps.iacCode`

未知フィールドを拒否する厳密な schema です。

| フィールド | 型 | 必須 | 意味 |
|------------|----|------|------|
| `schemaVersion` | `1` | はい | 拡張バージョン |
| `rosInvocationId` | string | はい | execution 呼び出し識別子。最大 256 文字 |
| `cwd` | string | はい | ワークスペース絶対パス |
| `model` | string | いいえ | リクエスト単位のモデル上書き |
| `llmApiKey` | string | いいえ | LLM provider key |
| `thinking.enabled/effort/budget` | boolean/string/正整数 | いいえ | thinking 設定 |
| `userId` | string | いいえ | telemetry と呼び出し元の識別 |
| `channel` | string | いいえ | チャネルメタデータ |
| `preferredLanguage` | string | いいえ | ユーザー向け言語（例：`ja`） |
| `candidatePresentation` | `standard` / `rich` | いいえ | Pipeline 候補の表示形式 |
| `runMode` | `normal` / `pipeline` | いいえ | 実行モード |
| `pipelineName` | string | いいえ | Pipeline 名 |
| `cleanupOnly` | boolean | いいえ | クリーンアップのみを要求 |
| `alibabaCloud.accessKeyId` | string | いいえ | 一時 AccessKey ID |
| `alibabaCloud.accessKeySecret` | string | いいえ | 一時 AccessKey Secret |
| `alibabaCloud.securityToken` | string | いいえ | 一時 STS token |
| `alibabaCloud.regionId` | string | いいえ | 既定 region |

initial run とその Resume は同じ `rosInvocationId` を使います。次の通常ターンでは新しい値を利用できます。Cancel も現在の値が必要です。

同じ `threadId` は最初の `cwd` と `userId` に固定され、後続リクエストで別のワークスペースや呼び出し元へ変更できません。

## SSE と heartbeat

各イベントは SSE `data:` レコードです。15 秒イベントがなければ次のコメントを送信します。

```text
: heartbeat
```

これは AG-UI `CUSTOM` ではありません。SSE クライアントは無視しつつ、HTTP 接続の維持に利用します。

## 標準イベント対応

| iac-code/A2A 信号 | AG-UI |
|-------------------|-------|
| リクエスト受付 | `RUN_STARTED` |
| agent テキスト | `TEXT_MESSAGE_*` |
| raw thinking | `REASONING_*` |
| ツール開始・引数 | `TOOL_CALL_START/ARGS/END` |
| ツール結果 | `TOOL_CALL_RESULT` |
| Pipeline step | `STEP_STARTED/STEP_FINISHED` |
| Pipeline 復旧スナップショット | `ACTIVITY_SNAPSHOT` |
| 正常終了 | success の `RUN_FINISHED` |
| 入力待ち | interrupt の `RUN_FINISHED` |
| エラー | `RUN_ERROR` |

`RUN_FINISHED` は AG-UI run 1 回の終了であり、Pipeline 全体の終了とは限りません。複数 Interrupt がある Pipeline では複数 run が生じます。Pipeline の業務終端は `pipeline_completed`、`pipeline_error` などで表します。

AG-UI span の整合性を保つため、Interrupt 前に開いている message、reasoning、tool、step を閉じ、Resume の新 run で継続中の step を再度開きます。同じ業務 step が run 間で一度閉じて再開して見えるのは逆順実行ではありません。

## iac-code カスタムイベント

- `iac-code.session.v1`：`threadId`、`executionId`、`contextId`、`taskId`、`sessionId` などの対応情報。`executionId` は Cancel に使用できます。
- `iac-code.artifact.v1`：A2A task artifact の構造化投影。
- `iac-code.tool-progress.v1`：標準イベントにないツール中間進捗。開始、引数、結果は標準 `TOOL_CALL_*` のままです。
- `iac-code.pipeline.v1`：標準表現のない有用な Pipeline 情報。

`iac-code.pipeline.v1` の `eventType`：

- Pipeline：`pipeline_started`、`pipeline_resumed`、`pipeline_completed`、`pipeline_error`、`pipeline_warning`、`backup_blocked`
- 候補：`candidate_started`、`candidate_completed`、`candidate_failed`、`candidate_interrupted`、`candidate_restart_requested`、`candidate_selected`、`candidate_detail_shown`、`candidate_step_failed`
- sub-pipeline：`sub_pipeline_started`、`sub_pipeline_completed`、`sub_step_failed`、`step_failed`
- スタックとクリーンアップ：`stack_progress`、`stack_instances_progress`、`stack_current_changed`、`cleanup_started`、`cleanup_progress`、`cleanup_completed`、`cleanup_failed`
- rollback：`rollback_triggered`、`rollback_completed`
- context：`context_compaction_started`、`context_compacted`、`context_compaction_failed`、`fields_marked_stale`
- 表示とツール：`diagram_shown`、`mcp_status`、`tool_progress`

`text_delta`、`thinking_delta`、`tool_started/tool_result`、`usage`、step lifecycle は標準イベントに変換されるため `CUSTOM` では重複送信しません。再送イベントは `(name, value.eventId)` または sequence で重複排除してください。

## Interrupt と Resume

入力待ち run は `RUN_FINISHED.outcome.type = "interrupt"` で終了します。Interrupt には `id`、`reason`、ユーザー向け `message`、任意の `toolCallId`、JSON `responseSchema`、`expiresAt`、`title/purpose/safeSummary/options/toolName` などの metadata が含まれます。

権限確認の例：

```json
{"decision": "allow_once"}
```

または：

```json
{"decision": "deny"}
```

UI は `reason` だけで推測せず、`message`、`responseSchema`、説明 metadata を利用してください。

Resume は同じ `threadId`、新しい `runId`、同じ `rosInvocationId` で、新しい `POST /` として送信します。

```json
{
  "resume": [{
    "interruptId": "permission-1",
    "status": "resolved",
    "payload": {"decision": "allow_once"}
  }]
}
```

全 pending Interrupt を 1 回ずつ回答し、重複・未知 ID は使用できません。`resolved` の payload は schema に一致する必要があります。`cancelled` はその Interrupt を中止し、権限では `deny` として扱われます。schema エラーは `RUN_ERROR` となりますが、Interrupt は再試行可能なままです。受理済み回答を再送してもツールは再実行されません。

## turn と識別子

```text
threadId（会話全体で固定）
  ├─ runId-1（ユーザーターン）
  ├─ runId-2（Interrupt Resume）
  ├─ runId-3（次の Resume）
  └─ runId-4（次の通常メッセージ）
```

HTTP/SSE リクエストごとに一意の `runId` を使います。冪等性の範囲は `(threadId, runId)` です。

## キャンセル拡張

```http
POST /extensions/iac-code/v1/executions/<executionId>/cancel
Content-Type: application/json
```

```json
{"threadId": "thread-1", "rosInvocationId": "invocation-1"}
```

結果は `cancelled`、`already_terminal`、または HTTP `404` の `EXECUTION_NOT_FOUND` です。Cancel は pending Interrupt を消去しますが、標準イベント形式は変更しません。

## 永続化と切断

状態は既定で `<config-dir>/agui/threads/<thread-key>.json` に保存されます。thread 対応、session/task/execution ID、Pipeline 復旧位置、pending Interrupt、冪等性情報を含みます。要求された thread だけを遅延読み込みし、その小さなファイルだけを原子的に置き換えます。

LLM key、AccessKey Secret、STS token、会話本文、実行成果物は保存しません。A2A の session/task 永続化は A2A server が管理します。[A2A ドキュメント](../a2a/overview.md)を参照してください。

期限切れ Interrupt は次回アクセス時に拒否・消去され、対応 A2A task のキャンセルを試みます。Interrupt で安全終了した run は古い SSE を必要としません。通常の実行中に切断すると A2A task をキャンセルします。

## エラー

SSE 前のエラーは HTTP JSON、実行中のエラーは `RUN_ERROR` です。主な code：

| code | 意味 |
|------|------|
| `INVALID_INPUT` | envelope、拡張、メッセージ、workspace が無効 |
| `DUPLICATE_RUN_ID` / `RUN_ID_CONFLICT` | run ID の再利用 |
| `THREAD_BUSY` | thread が実行中 |
| `THREAD_BINDING_CONFLICT` | workspace または caller が既存 binding と不一致 |
| `RESUME_REQUIRED` | Interrupt 回答待ち |
| `INCOMPLETE_RESUME` / `UNKNOWN_INTERRUPT` | Resume の不足、重複、未知 ID |
| `RESUME_PAYLOAD_INVALID` | payload が schema と不一致 |
| `RESUME_ALREADY_APPLIED` | 回答を適用済み |
| `EXECUTION_EXPIRED` / `EXECUTION_LOST` | execution が期限切れまたは復旧不能 |
| `STATE_PERSISTENCE_FAILED` | 重要状態を書き込めない |
| `A2A_UNAVAILABLE` / `A2A_PROTOCOL_ERROR` / `A2A_EXECUTION_FAILED` | A2A の接続、ID、実行エラー |
| `CANCELLED` | execution がキャンセル済み |

復旧に必要な書き込みは fail closed です。永続化前に復旧可能だと通知せず、必要に応じて A2A task をキャンセルします。
