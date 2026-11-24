---
sidebar_position: 2
title: はじめに
description: ACP サーバーの起動と最初のクライアント接続。
---

# ACP をはじめる

## 前提条件

1. **iac-code がインストール済み** — [インストール](../getting-started/installation.md)ガイドをご覧ください。

2. **LLM 認証情報の設定** — デスクトップや Web の**設定 → モデル**で設定するか、自動化環境にモデル設定を渡します。[認証](../configuration/authentication.md)を参照してください。

3. **Python ACP SDK**（任意、プログラムクライアント用）

   公式 Python SDK は PyPI で **`agent-client-protocol`**（`acp` としてインポート）として公開されています。このページの例はバージョン `0.9.0` で検証済みです：

   ```bash
   pip install "agent-client-protocol==0.9.0"
   ```

## ACP サーバーの起動

### Stdio モード（デフォルト）

```bash
iac-code acp
```

サーバーは JSON-RPC を使用して stdin/stdout で通信します。これは IDE が iac-code をサブプロセスとして起動する際に使用されるモードです。

### HTTP+SSE モード

```bash
iac-code acp --transport http --port 8765
```

指定されたポートでリッスンします。クライアントは HTTP でリクエストを送信し、Server-Sent Events でストリーミング更新を受信します。リモートまたはマルチクライアントのシナリオに適しています。

`IACCODE_ACP_HTTP_TOKEN` 環境変数を設定することで HTTP エンドポイントをセキュアにできます — サーバーは一致する `Authorization: Bearer <token>` ヘッダーを要求します。

### 動作確認

```bash
# Stdio: プロセスが起動し、stdin で JSON-RPC 入力を待ちます
iac-code acp

# HTTP: ヘルスエンドポイントを確認します
curl http://127.0.0.1:8765/health
```

## 最小限の例

公式 `agent-client-protocol` SDK を使用した最小限の Python の例です。より詳しいウォークスルー（ツール呼び出しのレンダリング、思考チャンク、HTTP+SSE トランスポート）については、[サンプル集](./examples.md)をご覧ください。

```python
"""Minimal iac-code ACP client using agent-client-protocol==0.9.0."""

import asyncio
from typing import Any

import acp
import acp.schema


class MyClient(acp.Client):
    async def session_update(
        self, session_id: str, update: Any, **kwargs: Any
    ) -> None:
        # Stream assistant text to stdout; ignore other update kinds in this minimal demo.
        if isinstance(update, acp.schema.AgentMessageChunk):
            print(update.content.text, end="", flush=True)

    async def request_permission(
        self, options, session_id, tool_call, **kwargs: Any
    ) -> acp.RequestPermissionResponse:
        # Auto-approve for demonstration — use interactive approval in production.
        return acp.RequestPermissionResponse(
            outcome=acp.schema.AllowedOutcome(
                outcome="selected", option_id="allow_once"
            )
        )


async def main() -> None:
    async with acp.spawn_agent_process(MyClient(), "iac-code", "acp") as (conn, _):
        # 1. Initialize — negotiate capabilities
        init_result = await conn.initialize(
            protocol_version=1,
            client_info=acp.schema.Implementation(name="demo", version="1.0"),
        )
        print(f"Protocol version: {init_result.protocol_version}")

        # 2. Create a session tied to your project directory
        session = await conn.new_session(cwd="/path/to/project")
        print(f"Session ID: {session.session_id}")

        # 3. Send a prompt; streaming output is delivered via MyClient.session_update
        result = await conn.prompt(
            session_id=session.session_id,
            prompt=[
                acp.schema.TextContentBlock(
                    type="text",
                    text="Generate a VPC template with 2 VSwitches",
                )
            ],
        )
        print(f"\nDone — stop_reason={result.stop_reason}")

        # 4. Clean up
        await conn.close_session(session_id=session.session_id)


asyncio.run(main())
```

ポイント：

- `acp.spawn_agent_process` は `iac-code acp` をサブプロセスとして起動し、その stdio ライフサイクルを管理します。
- `new_session(cwd=...)` はファイル操作を指定されたディレクトリにスコープします。
- ストリーミング更新（テキストチャンク、思考、ツール呼び出し）は `acp.Client` サブクラスの `session_update` コールバックを通じて到着します — `prompt()` 自体はターンが終了すると最終的な `stop_reason` を含む単一の `PromptResponse` を返します。
- 権限リクエストが到着すると、`request_permission` は `AllowedOutcome(outcome="selected", option_id=...)` または `DeniedOutcome(outcome="cancelled")` を返す必要があります — それ以外の値は `pydantic.ValidationError` を発生させます。

## クライアント設定

iac-code は ACP 互換のエディタやクライアントで動作します。以下の設定は **Zed** と **VSCode** に適用されます：

```json
{
  "agent_servers": {
    "iac-code": {
      "type": "custom",
      "command": "iac-code",
      "args": ["acp"]
    }
  }
}
```

- **Zed** — Zed の `settings.json` にスニペットを追加します。Zed は ACP エージェントサーバーをネイティブにサポートしています。
- **VSCode** — まず ACP クライアント拡張機能（Agent Client Protocol をサポートする拡張機能）をインストールし、拡張機能の設定で同じ設定を適用します。

## 次のステップ

- [プロトコルリファレンス](./protocol-reference.md) — メソッドとイベントの完全なドキュメント
- [HTTP+SSE トランスポート](./http-transport.md) — リモートデプロイとトークン認証
