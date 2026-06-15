---
sidebar_position: 2
title: はじめに
description: A2A サーバーを起動して最初のメッセージを送信します。
---

# A2A を始める

## 前提条件

1. **iac-code がインストール済み** — [インストール](/docs/getting-started/installation)ガイドを参照してください。

2. **LLM 認証情報が設定済み** — モデルプロバイダーの認証情報を設定するには、[認証](/docs/configuration/authentication)ガイドを参照してください。

3. **A2A サーバー依存関係** — `a2a` extra 付きで iac-code をインストールします。

```bash
uv sync --extra a2a
```

## A2A サーバーの起動

デフォルトのローカルインターフェイスでサーバーを起動します。

```bash
iac-code a2a --host 127.0.0.1 --port 41242
```

ローカル状態、アーティファクトストレージ、プッシュ通知配信、または署名済み Agent Card が必要な場合は YAML 設定ファイルを使用します。

```yaml
persistence-dir: ~/.iac-code/a2a
artifact-dir: ~/.iac-code/a2a/artifacts
signing-secret: local-card-signing-secret
push-notifications: true
thinking-exposure:
  - tool-trace
  - raw-thinking
```

次のように実行します。

```bash
iac-code a2a --config a2a-server.yml
```

`push-notifications: true` は、A2A タスクプッシュ通知設定メソッドと終端状態の配信を有効にします。複数のワーカーがプッシュ配信を調整する必要がある場合は、`push-queue: redis-streams` と `push-redis-url` を使用してください。

`thinking-exposure` は、ストリーミング中に `metadata.iac_code` 経由で公開する非回答ランタイム信号を制御します。デフォルトは `tool-trace` です。provider の推論内容を見てもよい信頼済みクライアントにだけ `raw-thinking` を追加してください。

サーバーは次を公開します。

| Route | 用途 |
|-------|---------|
| `GET /health` | ヘルスチェック |
| `GET /.well-known/agent-card.json` | Agent Card ディスカバリー |
| `POST /` | A2A JSON-RPC エンドポイント |

HTTP サーバーは A2A SDK REST ルートも登録し、Agent Card で `JSONRPC` と `HTTP+JSON` の両方のインターフェイスを広告します。

## ディスカバリーの検証

Agent Card を取得します。

```text
curl http://127.0.0.1:41242/.well-known/agent-card.json
```

`name: "iac-code"`、`JSONRPC` と `HTTP+JSON` のインターフェイス、`ETag` などのキャッシュヘッダー、任意の `urn:iac-code:a2a:artifact-metadata:v1` 拡張、サポートされる入力モード、`iac_generation`、`iac_review`、`aliyun_ros_operations`、`terraform_ros_conversion` などのスキルが表示されるはずです。

ヘルスエンドポイントを確認します。

```bash
curl http://127.0.0.1:41242/health
```

期待される応答:

```json
{"status":"healthy"}
```

## 認証を必須にする

認証は任意です。A2A 認証オプションや環境変数が設定されていない場合、リクエストに認証は不要です。何らかの認証方式が設定されている場合、Agent Card ディスカバリーを含むすべてのリクエストは、設定された方式のいずれかを満たす必要があります。

### Bearer Token

```bash
export IACCODE_A2A_HTTP_TOKEN=your-secret-token
iac-code a2a
```

対応する YAML 設定キーは `token` です。

```text
Authorization: Bearer <token>
```

### Basic Auth

```bash
export IACCODE_A2A_BASIC_USERNAME=iac-code
export IACCODE_A2A_BASIC_PASSWORD=your-password

iac-code a2a
```

ユーザー名とパスワードは両方とも存在する必要があります。対応する YAML 設定キーは `basic-username` と `basic-password` です。

### API Key

```bash
export IACCODE_A2A_API_KEY=your-api-key

iac-code a2a
```

デフォルトの API key ヘッダーは次のとおりです。

```text
X-API-Key: <api-key>
```

`api-key-header` YAML 設定キーまたは `IACCODE_A2A_API_KEY_HEADER` で上書きします。

```yaml
api-key: your-api-key
api-key-header: X-IAC-Code-Key
```

## リモート A2A エージェントを呼び出す

安定したクライアント接続と認証設定を YAML ファイルに入れます。

```yaml
url: http://127.0.0.1:41242/
token: your-secret-token
verify-card-secret: your-card-signing-secret
require-card-signature: true
cwd: /path/to/workspace
```

直接の Phase 1 クライアント呼び出しには `a2a-client call` を使用します。

```bash
iac-code a2a-client --config a2a-client.yml call --prompt "Create a VPC with two vSwitches" --cwd "$PWD"
```

最終応答だけでなく増分イベントが必要な場合は `--stream` を使用します。

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Review this template" \
  --cwd "$PWD" \
  --stream
```

一度きりのターゲットやトークンが必要な場合、コマンドラインオプションは設定値を上書きします。

```bash
iac-code a2a-client --config a2a-client.yml call \
  --url https://other-agent.example.com/ \
  --prompt "Review this template"
```

マルチエージェントルーティングでは、呼び出し前にルート選択をプレビューします。

```bash
iac-code a2a-client route-preview \
  --route "template=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --route-state-dir ~/.iac-code/a2a
```

タスク管理、プッシュ設定 CRUD、拡張 Agent Card、トランスポートオプションを含むすべての A2A コマンドについては、[コマンドリファレンス](./command-reference.md)を参照してください。

## curl で最初のメッセージを送信する

ワークスペースディレクトリは `message.metadata.iac_code.cwd` を通じて渡します。パスは絶対パスで、既に存在し、許可されたワークスペースルート内にある必要があります。デフォルトでは、許可されるルートはサーバープロセスディレクトリとシステム一時ディレクトリです。`IACCODE_A2A_ALLOWED_CWDS` で上書きできます。

サーバーはテキスト風パーツ、JSON データパーツ、生の UTF-8 テキスト、ローカルワークスペースの `file://` テキストファイル、制限付きマルチモーダル添付を受け付けます。リモート URL 取り込みはサポートされません。`url` パーツは、許可されたワークスペース内のローカル `file://` URL でなければなりません。

```bash
curl -s -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-1",
        "role": "ROLE_USER",
        "parts": [
          {"text": "Generate a ROS VPC template with two vSwitches."}
        ],
        "metadata": {
          "iac_code": {
            "cwd": "/path/to/project"
          }
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

ストリーミング出力には `SendStreamingMessage` を使用します。

```bash
curl -N -X POST http://127.0.0.1:41242/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "2",
    "method": "SendStreamingMessage",
    "params": {
      "message": {
        "messageId": "msg-2",
        "role": "ROLE_USER",
        "parts": [
          {"text": "Review my Terraform files and suggest ROS equivalents."}
        ],
        "metadata": {
          "iac_code": {
            "cwd": "/path/to/project"
          }
        }
      },
      "configuration": {
        "acceptedOutputModes": ["text/plain"]
      }
    }
  }'
```

## 最小 Python SDK 例

以下の例では、`a2a-sdk>=1.0.2,<2` を使用します。これは `a2a` extra で使用されるバージョン範囲です。

```python
"""Minimal iac-code A2A client using a2a-sdk."""

import asyncio
import uuid
from pathlib import Path

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, SendMessageRequest


async def main() -> None:
    async with httpx.AsyncClient(timeout=120.0) as httpx_client:
        config = ClientConfig(httpx_client=httpx_client, streaming=True)
        client = await ClientFactory(config).create_from_url("http://127.0.0.1:41242")

        request = SendMessageRequest(
            message=Message(
                message_id=f"msg-{uuid.uuid4().hex}",
                role=Role.ROLE_USER,
                parts=[Part(text="Generate a ROS VPC template with two vSwitches.")],
                metadata={"iac_code": {"cwd": str(Path.cwd())}},
            )
        )

        async for event in client.send_message(request):
            if event.HasField("status_update"):
                status = event.status_update.status
                if status.message:
                    for part in status.message.parts:
                        if part.text:
                            print(part.text, end="", flush=True)

        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
```

:::tip
認証済みサーバーでは、`httpx.AsyncClient` を `headers={"Authorization": "Bearer <token>"}` 付きで構築し、Agent Card ディスカバリーと JSON-RPC 呼び出しの両方にトークンが含まれるようにしてください。
:::

## 次のステップ

- [コマンドリファレンス](./command-reference.md) — CLI コマンドとオプションの完全なリファレンス。
- [プロトコルリファレンス](./protocol-reference.md) — メソッド、ルート、状態、メタデータの詳細。
- [HTTP トランスポート](./http-transport.md) — JSON-RPC HTTP の動作、bearer auth、curl ワークフロー。
- [例](./examples.md) — SDK、直接 HTTP、フォローアップ、キャンセル、メタデータ処理の例。
