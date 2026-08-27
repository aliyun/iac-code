---
sidebar_position: 2
title: クイックスタート
description: iac-code AG-UI アダプターのインストール、起動、呼び出し。
---

# AG-UI クイックスタート

## 前提条件

1. Python 3.10 以降がインストール済みであること。
2. iac-code の LLM provider が設定済みであること。[認証](../configuration/authentication.md)を参照してください。
3. Alibaba Cloud を操作する場合は、クラウド認証情報を設定するか、リクエスト単位で一時認証情報を渡すこと。
4. iac-code が読み書きできるワークスペースの絶対パスがあること。

AG-UI 依存関係をインストールします。

```bash
pip install "iac-code[agui]"
```

ソースリポジトリで開発する場合：

```bash
uv sync --extra agui
```

## 方法 1：管理対象のローカル A2A カーネル

最も簡単な方法は `--a2a-url` を省略することです。

```bash
iac-code agui --host 127.0.0.1 --port 41243
```

アダプターは空いているループバックポートを選び、`iac-code a2a` 子プロセスを起動し、終了時に停止します。子プロセスは現在の iac-code 設定と実行環境を継承します。

ローカル開発や単一ライフサイクル管理に適しています。2 つのプロセスを個別に管理する場合は次の方法を使います。

## 方法 2：独立した A2A カーネルへ接続

A2A server を起動します。

```bash
iac-code a2a --host 127.0.0.1 --port 41242 --thinking-exposure all
```

次に AG-UI adapter を起動します。

```bash
iac-code agui \
  --host 0.0.0.0 \
  --port 41243 \
  --a2a-url http://127.0.0.1:41242
```

A2A は A2A クライアントへの提供を続けながら、AG-UI adapter からもループバック経由で利用できます。

`--thinking-exposure all` は raw thinking を標準 `REASONING_*` に変換できるようにします。信頼できるクライアントにのみ有効化してください。推論を公開しない場合は既定の `tool-trace` を使います。

A2A server が Bearer token を使う場合：

```bash
export IACCODE_A2A_HTTP_TOKEN="a2a-local-secret"
iac-code a2a --host 127.0.0.1 --port 41242
```

adapter に同じ upstream token を設定します。

```bash
export IAC_CODE_AGUI_A2A_TOKEN="a2a-local-secret"
iac-code agui --port 41243 --a2a-url http://127.0.0.1:41242
```

## YAML 設定

```yaml title="agui-server.yml"
host: 0.0.0.0
port: 41243
a2a-url: http://127.0.0.1:41242
interrupt-ttl: 540
state-dir: /var/lib/iac-code/agui
idle-shutdown: 0
debug: false
log-stdout: true
```

```bash
iac-code agui --config agui-server.yml
```

明示した CLI 引数は YAML より優先されます。token などの機密値は設定ファイルではなく環境変数で渡すことを推奨します。

| CLI / YAML | 既定値 | 意味 |
|------------|--------|------|
| `--host` / `host` | `127.0.0.1` | AG-UI HTTP バインド先 |
| `--port` / `port` | `8000` | AG-UI ポート。例では `41243` |
| `--a2a-url` / `a2a-url` | 空 | ローカル A2A URL。空なら子プロセスを起動 |
| `--interrupt-ttl` / `interrupt-ttl` | `540` | Interrupt を Resume できる秒数 |
| `--state-dir` / `state-dir` | `<config-dir>/agui` | thread 状態ディレクトリ |
| `--idle-shutdown` / `idle-shutdown` | `0` | アイドル終了秒数。`0` は無効 |
| `--debug` / `debug` | `false` | デバッグログ |
| `--log-stdout` / `log-stdout` | `false` | stdout にもログを出力 |

| 環境変数 | 用途 |
|----------|------|
| `IAC_CODE_AGUI_HOST` | バインド先 |
| `IAC_CODE_AGUI_PORT` | ポート |
| `IAC_CODE_AGUI_A2A_URL` | ローカル A2A upstream URL |
| `IAC_CODE_AGUI_A2A_TOKEN` | A2A upstream token |
| `IAC_CODE_AGUI_AUTH_TOKEN` | AG-UI endpoint を保護する token |
| `IAC_CODE_AGUI_INTERRUPT_TTL` | Interrupt 有効期間 |
| `IAC_CODE_AGUI_STATE_DIR` | thread 状態ディレクトリ |
| `IAC_CODE_AGUI_ALLOWED_CWDS` | OS のパス区切りで列挙した許可ワークスペースルート |
| `IAC_CODE_CONFIG_DIR` | iac-code 設定ルートと既定状態ディレクトリの親 |

## ヘルスチェック

```bash
curl http://127.0.0.1:41243/health
```

```json
{
  "status": "ok",
  "protocol": "ag-ui",
  "protocolPackageVersion": "0.1.20",
  "executionKernel": "a2a-1.0",
  "serverVersion": "現在の iac-code バージョン"
}
```

## 公式 JavaScript client

```bash
pnpm add @ag-ui/client@0.0.58
```

標準 `HttpAgent` で `iac-code agui` へ直接接続し、`forwardedProps` に実行情報を渡します。

```javascript
import { HttpAgent, randomUUID } from "@ag-ui/client";

const agent = new HttpAgent({
  url: "http://127.0.0.1:41243/",
  threadId: randomUUID(),
  // IAC_CODE_AGUI_AUTH_TOKEN を設定した場合：
  // headers: { Authorization: `Bearer ${process.env.AG_UI_TOKEN}` },
});

const forwardedProps = {
  iacCode: {
    schemaVersion: 1,
    rosInvocationId: randomUUID(),
    cwd: process.cwd(),
    runMode: "normal",
    preferredLanguage: "ja",
  },
};

agent.addMessage({
  id: randomUUID(),
  role: "user",
  content: "2 つの vSwitch を持つ VPC テンプレートを作成してください。",
});

const subscriber = {
  onTextMessageContentEvent({ event }) {
    process.stdout.write(event.delta);
  },
  onToolCallStartEvent({ event }) {
    console.log(`\n[tool] ${event.toolCallName}`);
  },
  onStepStartedEvent({ event }) {
    console.log(`\n[step] ${event.stepName}`);
  },
  onRunErrorEvent({ event }) {
    console.error(`\n${event.code}: ${event.message}`);
  },
};

await agent.runAgent({ forwardedProps }, subscriber);
```

Bearer token を使う場合は `HttpAgent.headers` で `Authorization` を渡します。ブラウザーは通常、同一オリジンのバックエンドまたはリバースプロキシ経由で接続します。adapter 自体は CORS を追加しません。

## Interrupt の処理

公式 client は Interrupt を `agent.pendingInterrupts` に保持します。各 `responseSchema` に従って回答し、新しい run で送信します。

```javascript
const responses = agent.pendingInterrupts.map((interrupt) => ({
  interruptId: interrupt.id,
  status: "resolved",
  payload: { decision: "allow_once" },
}));

await agent.runAgent({ forwardedProps, resume: responses }, subscriber);
```

この payload は `decision` を要求する権限 Interrupt 専用です。質問や選択はそれぞれの schema に従ってください。

Resume では、元の `threadId`、新しい `runId`、中断時と同じ `rosInvocationId` を使い、保留中の全 Interrupt を 1 回ずつ回答します。`resolved` は schema に合う payload が必須で、続行しない場合は `cancelled` を使います。

## Pipeline の開始

```javascript
const forwardedProps = {
  iacCode: {
    schemaVersion: 1,
    rosInvocationId: randomUUID(),
    cwd: process.cwd(),
    runMode: "pipeline",
    pipelineName: "selling",
    candidatePresentation: "rich",
  },
};
```

クライアントは `STEP_*`、`TOOL_CALL_*`、`ACTIVITY_SNAPSHOT`、`CUSTOM` を処理します。iac-code 独自イベントを知らない汎用クライアントも標準イベントは正常に利用できます。

## ワークスペースと一時認証情報

`cwd` はサーバー起動時には固定されず、リクエストごとに指定します。`IAC_CODE_AGUI_ALLOWED_CWDS` または `IACCODE_A2A_ALLOWED_CWDS` で許可されたルート配下の絶対パスでなければなりません。

モデル、LLM key、Alibaba Cloud 一時認証情報は `forwardedProps.iacCode` でリクエスト単位に渡せます。adapter はこれらを thread 状態へ保存せず、A2A 実行カーネルへ転送します。

## 状態ディレクトリ

```text
<IAC_CODE_CONFIG_DIR>/agui/
  threads/
    <threadId>.json
```

thread ごとに独立して保存され、起動時に全履歴を走査しません。通常の UUID は読みやすいファイル名を維持し、安全でない ID はエンコードされ、長い ID には固定長キーを使います。JSON 内では元の `threadId` を常に保存・検証します。

ここに保存されるのは adapter の対応情報、Interrupt、冪等性状態だけです。会話本文や認証情報は含まれません。JSON を手動編集しないでください。

## 次のステップ

- [AG-UI 概要](./overview.md)
- [プロトコルリファレンス](./protocol-reference.md)
