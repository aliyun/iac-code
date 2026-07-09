---
title: HTTP+SSE トランスポート
description: リモートおよびマルチクライアントシナリオ向けに HTTP と Server-Sent Events で ACP サーバーを実行。
sidebar_position: 5
---

# HTTP+SSE トランスポート

iac-code の ACP サーバーは 2 つのトランスポートモードをサポートしています。デフォルトの **Stdio** トランスポートは標準入出力で通信し、ローカルの IDE 統合に最適です。**HTTP+SSE** トランスポートはネットワークエンドポイントを公開し、Server-Sent Events でレスポンスをストリーミングするため、リモートデプロイ、ロードバランス環境、マルチクライアントアクセスに適しています。

## HTTP+SSE を選ぶ理由

Stdio には固有の制限があります：

- サーバープロセスがクライアントの直接の子プロセスである必要があり、リモートアクセスができません。
- ブロッキングプロセス管理のため、複数のクライアントへの同時サービスが困難です。
- ネットワークプロキシ、ロードバランサー、コンテナ化されたデプロイメントと互換性がありません。

HTTP+SSE はこれらの制約に対処します：

- **ネットワーク対応** — エンドポイントに到達できるあらゆるマシンからアクセス可能。
- **マルチクライアント** — 各クライアントは独自のイベントストリームを持つ分離された接続を取得。
- **インフラ対応** — リバースプロキシの背後、コンテナ内、標準的な HTTP 監視ツールで動作。
- **簡単な統合** — あらゆる HTTP クライアント（curl、fetch、SDK）でサーバーと対話可能。

## HTTP サーバーの起動

```bash
# Default port 8765
iac-code acp --transport http

# Custom port
iac-code acp --transport http --port 9090
```

サーバーは ASGI フレームワークとして [Starlette](https://www.starlette.io/) を使用し、Uvicorn で動作します。

## ルート

すべてのルートは `/acp` パスで提供されます。HTTP メソッドが操作を決定します。

### `POST /acp`

JSON-RPC リクエストをサーバーに送信します。

- **`initialize`** — 新しい接続を作成し、完全な JSON-RPC レスポンスを直接返します。レスポンスには `Acp-Connection-Id` ヘッダーが含まれます。
- **その他のメソッド** — 有効な `Acp-Connection-Id` ヘッダーが必要です。即座に `202 Accepted` を返し、実際の結果は SSE ストリーム経由で非同期に配信されます。

### `GET /acp`

レスポンスと通知を受信するための Server-Sent Events ストリームを開きます。

- `Acp-Connection-Id` ヘッダーが必要です。
- イベントのタイプは `message` で、JSON-RPC レスポンス/通知が `data` フィールドとなります。
- ストリームには自動再接続用の `id` と `retry` フィールドが含まれます。

### `DELETE /acp`

接続を閉じ、関連するすべてのリソースを解放します。

- `Acp-Connection-Id` ヘッダーが必要です。
- `200 OK` を返します。

## Connection ID

Connection ID はクライアントのリクエストとその SSE イベントストリームを結び付けます。

1. クライアントが `initialize` メソッドで `POST /acp` を送信します。
2. サーバーは初期化結果と UUID を含む `Acp-Connection-Id` レスポンスヘッダーで応答します。
3. 以降のすべてのリクエスト（`POST`、`GET`、`DELETE`）は、この値を持つ `Acp-Connection-Id` リクエストヘッダーを含める必要があります。
4. 各 Connection ID は独自のイベントキューを持つ独立した ACP エージェントセッションにマッピングされます。

リクエストが存在しないまたは無効な Connection ID を参照した場合、サーバーは `400 Bad Request` を返します。

## 認証

サーバーは `IACCODE_ACP_HTTP_TOKEN` 環境変数によるオプションの Bearer トークン認証をサポートしています。

```bash
# Set the token before starting the server
export IACCODE_ACP_HTTP_TOKEN=your-secret-token
iac-code acp --transport http
```

設定すると、すべてのリクエストに以下を含める必要があります：

```
Authorization: Bearer your-secret-token
```

| シナリオ | 動作 |
|----------|----------|
| トークン未設定 | 認証不要（ローカル開発に適しています） |
| トークン設定済み、ヘッダー一致 | リクエストは正常に処理されます |
| トークン設定済み、ヘッダーが欠落/不一致 | `401 Unauthorized` が返されます |

## 完全なワークフロー

`curl` を使用した完全なやり取りの例：

```bash
# Step 1: Initialize — creates a connection and returns the Connection ID
CONN_ID=$(curl -s -D - -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}' \
  | grep -i 'acp-connection-id' | awk '{print $2}' | tr -d '\r')

echo "Connection ID: $CONN_ID"

# Step 2: Open the SSE stream (run in background)
curl -N http://localhost:8765/acp \
  -H "Acp-Connection-Id: $CONN_ID" &
SSE_PID=$!

# Step 3: Create a session
curl -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: $CONN_ID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/workspace"}}'

# Step 4: Send a prompt
curl -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: $CONN_ID" \
  -d '{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"...","prompt":[{"type":"text","text":"Hello"}]}}'

# Step 5: Close the connection
curl -X DELETE http://localhost:8765/acp \
  -H "Acp-Connection-Id: $CONN_ID"

# Clean up background SSE process
kill $SSE_PID 2>/dev/null
```

:::tip
`initialize` のレスポンスは同期的に返されます（30 秒のタイムアウト内）。以降のすべてのレスポンスは、ステップ 2 で開いた SSE ストリームを通じてのみ配信されます。
:::
