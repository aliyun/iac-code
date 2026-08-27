---
sidebar_position: 1
title: AG-UI プロトコル
description: iac-code の AG-UI 連携におけるアーキテクチャ、機能、利用場面。
---

# AG-UI プロトコル

## AG-UI とは

[Agent-User Interaction Protocol（AG-UI）](https://docs.ag-ui.com/concepts/architecture) は、エージェントとユーザー向けアプリケーションをつなぐイベントストリームプロトコルです。クライアントは `RunAgentInput` で実行を開始し、HTTP Server-Sent Events（SSE）を通じて、テキスト、推論、ツール呼び出し、ステップ、状態、Interrupt を構造化イベントとして受信します。

AG-UI は、Web コンソール、チャットクライアント、IDE 拡張など、エージェントの実行状況をリアルタイム表示するアプリケーションに適しています。最終テキストだけでなく、モデル出力、ツール引数と結果、Pipeline ステップ、ユーザー確認待ちの操作を個別に描画できます。

## iac-code のアーキテクチャ

iac-code は **A2A 実行カーネル + AG-UI プロトコルアダプター** という構成です。

```text
AG-UI client
    ↓ RunAgentInput / SSE
iac-code agui
    ↓ A2A 1.0 HTTP
iac-code a2a
    ↓
Agent loop / Pipeline / LLM / Alibaba Cloud API
```

`iac-code a2a` が唯一の実行カーネルであり、次を担当します。

- normal 会話と Pipeline 実行
- iac-code session、A2A context、task
- ツール権限、質問、選択、復旧
- 実行ライフサイクルとキャンセル
- LLM と Alibaba Cloud API の呼び出し

`iac-code agui` は別の Agent runtime を生成せず、Pipeline も直接実行しません。担当範囲は次のとおりです。

- AG-UI `RunAgentInput` を A2A リクエストへ変換
- A2A イベントを標準 AG-UI イベントへ投影
- `threadId/runId` と A2A `contextId/taskId` の対応付け
- AG-UI `resume[]` を A2A 入力復旧へ変換
- プロトコル対応情報と保留中 Interrupt の永続化
- キャンセルの A2A への転送

このため、AG-UI と A2A が別々の実行仕様を持つことはありません。モデル、クラウド認証情報、権限ルール、Pipeline 動作は同じ A2A runtime が処理します。

## 標準プロトコルと iac-code 拡張

外部ストリームには次の標準 AG-UI イベントを使用します。

- `RUN_STARTED`、`RUN_FINISHED`、`RUN_ERROR`
- `TEXT_MESSAGE_*`
- `REASONING_*`
- `TOOL_CALL_*`
- `STEP_STARTED`、`STEP_FINISHED`
- `ACTIVITY_SNAPSHOT`

標準イベントで表現できず、クライアント表示に必要な iac-code Pipeline 情報だけを、名前空間付き `CUSTOM` イベントとして送信します。汎用 AG-UI クライアントはこれらを無視しても、テキスト、ツール呼び出し、Interrupt、run ライフサイクルを正常に処理できます。

リクエストは標準 `RunAgentInput` です。ワークスペースや実行モードなど、iac-code に必要な情報は標準の `forwardedProps` に格納します。

```json
{
  "forwardedProps": {
    "iacCode": {
      "schemaVersion": 1,
      "rosInvocationId": "request-identity",
      "cwd": "/absolute/workspace/path",
      "runMode": "normal"
    }
  }
}
```

汎用 AG-UI クライアントは標準イベントをそのまま利用できます。ただし `iac-code agui` を直接呼び出す場合は、`forwardedProps.iacCode` に `cwd` などの実行情報を指定する必要があります。

## 対応する対話

### 複数ターンの normal 会話

会話全体で同じ `threadId` を使い、ユーザーターンごとに新しい `runId` を指定します。アダプターは thread を 1 つの iac-code session に関連付けます。次のメッセージは新しい HTTP/SSE リクエストで始まり、終了済みの SSE 応答を再利用しません。

### Pipeline

`forwardedProps.iacCode.runMode` を `pipeline` に設定します。実行は A2A Pipeline カーネルが行います。最上位ステップは標準 `STEP_*`、テキスト、推論、ツールは対応する標準イベントになります。候補、スタック進捗、クリーンアップ進捗など標準表現のない情報は `iac-code.pipeline.v1` で送信されます。

並列 sub-pipeline は個別のメッセージ ID とステップ ID を使うため、複数 agent loop のテキストが 1 つに混在しません。

### Interrupt と Resume

権限確認、質問、選択でユーザー入力が必要になると、現在の run は次のイベントで終了します。

```json
{
  "type": "RUN_FINISHED",
  "outcome": {"type": "interrupt", "interrupts": []}
}
```

Interrupt はクライアントへ通知する前に永続化されます。クライアントは回答を集め、同じ `threadId`、新しい `runId`、`resume[]` で新規リクエストを開始します。Resume の SSE は新しいリクエストに属し、古いストリームへ再接続するものではありません。

### アダプター状態

アダプターは thread ごとに、プロトコル対応情報、冪等性情報、保留中 Interrupt を保存します。このディレクトリには会話本文、LLM キー、クラウド認証情報は保存されず、会話のエクスポート先でもありません。

## AG-UI を選ぶ場面

| 要件 | 推奨モード |
|------|------------|
| テキスト、推論、ツール、ステップをリアルタイム表示するチャット UI | **AG-UI** |
| UI で権限、質問、選択を処理 | **AG-UI** |
| 別のエージェントやオーケストレーターから直接呼び出す | **A2A** |
| IDE/エディターで ACP session やターミナル機能を利用 | **ACP** |
| ローカルで手動操作 | **対話 REPL または Web/Desktop** |

AG-UI と A2A は同時に起動できます。HTTP エンドポイントは別ですが、同じ iac-code 実装を利用します。

## 現在の制約

- AG-UI のトランスポートは HTTP POST + SSE です。
- A2A upstream はループバックアドレスに限られ、任意のリモート A2A URL には接続できません。
- `cwd` はリクエストごとに必須で、許可されたワークスペースルート配下でなければなりません。
- クライアント定義の `tools` は現在未対応です。ツール集合は iac-code が管理します。
- ユーザーメッセージはテキストとインライン base64 画像に対応し、リモートメディア URL には対応しません。
- Interrupt 前の実行中 SSE をクライアントが切断すると、対応する A2A task はキャンセルされます。
- SSE は 15 秒ごとにコメント形式の heartbeat を送信し、準拠クライアントはこれを無視します。

## 次に読むページ

- [クイックスタート](./getting-started.md)
- [プロトコルリファレンス](./protocol-reference.md)
