---
title: HTTP トランスポート
description: JSON-RPC HTTP 経由で iac-code A2A サーバーを実行して呼び出します。
sidebar_position: 5
---

# HTTP トランスポート

iac-code のデフォルト A2A サーバーは、HTTP 上の JSON-RPC と A2A SDK REST ルートを公開します。サーバーは Starlette で構築され、Uvicorn 上で実行されます。

## サーバーの起動

```bash
# Default host and port
iac-code a2a

# Explicit host and port
iac-code a2a --host 127.0.0.1 --port 41242

# Listen on all interfaces
iac-code a2a --host 0.0.0.0 --port 41242
```

最初に任意のサーバー依存関係をインストールします。

```bash
uv sync --extra a2a
```

## エンドポイント概要

| Route | Method | Response |
|-------|--------|----------|
| `/health` | `GET` | プレーン JSON のヘルス応答 |
| `/.well-known/agent-card.json` | `GET` | Agent Card JSON |
| `/` | `POST` | JSON-RPC 応答または SSE ストリーム |
| SDK REST routes | mixed | SDK によって登録される A2A REST エンドポイント |

## ヘッダー

推奨ヘッダー:

```text
Content-Type: application/json
A2A-Version: 1.0
```

Bearer auth が有効な場合:

```text
Authorization: Bearer <token>
```

## 認証

サーバーは任意の Bearer token、Basic auth、API key 認証をサポートします。認証オプションや環境変数が設定されていない場合、リクエストに認証は不要です。1 つ以上の方式が設定されている場合、リクエストはいずれかの設定済み方式で認証できます。

### Bearer Token

```bash
export IACCODE_A2A_HTTP_TOKEN=your-secret-token
iac-code a2a
```

A2A YAML 設定ファイルで `token` を設定することもできます。

### Basic Auth

```bash
export IACCODE_A2A_BASIC_USERNAME=iac-code
export IACCODE_A2A_BASIC_PASSWORD=your-password

iac-code a2a
```

Basic auth を有効にするには、ユーザー名とパスワードの両方を設定する必要があります。

### API Key

```bash
export IACCODE_A2A_API_KEY=your-api-key
iac-code a2a
```

デフォルトの API key ヘッダーは `X-API-Key` です。YAML で変更できます。

```yaml
api-key: ${IACCODE_A2A_API_KEY}
api-key-header: X-IAC-Code-Key
```

または `IACCODE_A2A_API_KEY_HEADER` を使用します。

| シナリオ | 挙動 |
|----------|----------|
| 認証方式が設定されていない | 認証は不要 |
| 1 つ以上の方式が設定され、いずれか 1 つが一致する | リクエストは続行される |
| 1 つ以上の方式が設定され、どの方式も一致しない | HTTP `401` と `{"error":"Unauthorized"}` |

## Agent Card ディスカバリー

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json
```

認証あり:

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json \
  -H "Authorization: Bearer $IACCODE_A2A_HTTP_TOKEN"
```

API key 認証あり:

```bash
curl http://127.0.0.1:41242/.well-known/agent-card.json \
  -H "X-API-Key: $IACCODE_A2A_API_KEY"
```

JSON-RPC エンドポイント URL は `supportedInterfaces[0].url` で広告されます。HTTP モードは REST 対応クライアント向けに `HTTP+JSON` インターフェイスも広告します。

## 非ストリーミングメッセージ

`SendMessage` は、エージェントターンが完了した後に単一の JSON-RPC 応答を返します。

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "send-1",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-1",
        "role": "ROLE_USER",
        "parts": [{"text": "Create a Terraform VPC module for Alibaba Cloud."}],
        "metadata": {
          "iac_code": {"cwd": "/path/to/project"}
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

## ストリーミングメッセージ

`SendStreamingMessage` は Server-Sent Events を返します。イベントが到着したタイミングで出力するには `curl -N` を使用します。

```bash
curl -N -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "stream-1",
    "method": "SendStreamingMessage",
    "params": {
      "message": {
        "messageId": "msg-2",
        "role": "ROLE_USER",
        "parts": [{"text": "Generate a ROS template for one VPC and two vSwitches."}],
        "metadata": {
          "iac_code": {"cwd": "/path/to/project"}
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

各 SSE `data:` 行には、`result` が A2A `StreamResponse` である 1 つの JSON-RPC 応答が含まれます。

## フォローアップメッセージ

最初の応答で返された `taskId` と `contextId` を使用して、同じ会話を継続します。

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "send-2",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-3",
        "taskId": "task-id-from-first-response",
        "contextId": "context-id-from-first-response",
        "role": "ROLE_USER",
        "parts": [{"text": "Now add tags for environment and owner."}],
        "metadata": {
          "iac_code": {"cwd": "/path/to/project"}
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

再利用される `contextId` では、ワークスペースが同じままである必要があります。

## 実行中タスクをキャンセルする

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "cancel-1",
    "method": "CancelTask",
    "params": {
      "id": "task-id"
    }
  }'
```

キャンセルは協調的です。iac-code はアクティブなエージェントターンをキャンセルし、キャンセル済み状態を出力し、コンテキストロックを解放します。既に実行中でない既存タスクをキャンセルすると、標準 A2A `TaskNotCancelableError` が返されます。

## 対応する CLI

ほとんどの HTTP ワークフローには対応する CLI コマンドがあります。

```yaml
url: http://127.0.0.1:41242/
```

```bash
# Discover the Agent Card
iac-code a2a-client --config a2a-client.yml discover

# Send a non-streaming prompt
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a Terraform VPC module for Alibaba Cloud." \
  --cwd "$PWD"

# Send a streaming prompt
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Generate a ROS template for one VPC and two vSwitches." \
  --cwd "$PWD" \
  --stream

# Inspect task state
iac-code a2a-client --config a2a-client.yml task-get --task-id task-id
iac-code a2a-client --config a2a-client.yml task-list --output table

# Cancel an active task
iac-code a2a-client --config a2a-client.yml task-cancel --task-id task-id
```

完全なオプション一覧は[コマンドリファレンス](./command-reference.md)を参照してください。

## 運用上の注意

- ローカル専用の利用では `127.0.0.1` にバインドしてください。
- 共有ネットワークインターフェイスにバインドする前に、A2A 設定の `token` または `IACCODE_A2A_HTTP_TOKEN` を使用してください。
- A2A モードはツール権限リクエストを自動的に拒否します。ローカル自動化サービスのような認証なしエンドポイントは保護してください。
- アクティブなランタイム状態はメモリ内にあります。永続化はタスクとコンテキストのメタデータをミラーしますが、プロセスを再起動しても実行中の asyncio 作業は再開されません。
- 1 つのコンテキストでは同時に 1 つのタスクだけを実行できます。別々のコンテキストは並行して実行できます。
