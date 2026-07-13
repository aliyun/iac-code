---
sidebar_position: 5
title: MCP トラブルシューティング
description: MCP の設定、接続、認証、機能検出の問題を診断します。
---

# MCP トラブルシューティング

MCP warnings は、必要な capabilities がすべて利用不能な場合を除き fatal ではありません。失敗した server が他の MCP servers や組み込みの IaC Code tools の動作を妨げるべきではありません。

## Inspect Configuration

サーバーへ接続せず、設定済み servers を確認します:

```bash
iac-code mcp list
```

設定済み servers に対して bounded health diagnostics を実行します:

```bash
iac-code mcp list --check
```

接続せずに編集されたサーバー構成を検査します。

```bash
iac-code mcp get my-server --scope local
```

1 つのサーバーに対して限定された正常性診断を実行します。

```bash
iac-code mcp get my-server --scope local --check
```

接続せずに構成を明示的に検査します。

```bash
iac-code mcp list --config-only
iac-code mcp get my-server --scope local --config-only
```

Remove a bad server:

```bash
iac-code mcp remove my-server --scope local
```

Clear project approval choices:

```bash
iac-code mcp reset-project-choices
```

サーバーまたはすべての永続化サーバーを再接続します。

```bash
iac-code mcp reconnect my-server
iac-code mcp reconnect --all
```

## Config Not Found

症状:

```text
MCP server 'name' not found in persisted MCP config.
MCP server 'name' not found in user config.
```

修正:

```bash
iac-code mcp list --config-only
iac-code mcp get name --scope user --config-only
iac-code mcp get name --scope user --source-path /path/to/settings.yml --config-only
```

設定一覧に表示された正確な `--scope` を使います。デフォルト以外の永続化ファイルでは、対応する
`--source-path` も指定します。server が削除済みなら、存在しない設定を auth せずに再度 add してください。

## Pending Project Server

状態または warning code: `pending_approval`.

Symptom:

```text
Project MCP server 'name' is pending approval.
```

Fix:

```bash
iac-code mcp approve name
```

または、そのプロジェクトで対話型 REPL を開始し、プロンプトが表示されたら「y」と答えます。 Enter を押すと`N`を意味し、サーバーを拒否します。

以前は承認が機能していたが停止した場合は、`.mcp.json` が変更されているかどうかを確認してください。承認は構成署名に関連付けられています。

## Missing Environment Variable

Symptom:

```text
Environment variable 'TOKEN' is not set for MCP config.
```

Fix one of these:

```bash
export TOKEN=...
```

or use a default:

```json
"Authorization": "${TOKEN:-}"
```

必要な環境変数が欠落しているサーバーはスキップされます。

## Connection Failed

状態または warning code: `connection_failed`.

For stdio servers:

- Verify `command` exists on `PATH`.
- 別のディレクトリから起動する場合は、スクリプトに絶対パスを使用します。
- Windows では、`cmd /c npx`を通じてノードベースのサーバーを実行します。
- 必要な環境変数が設定されていることを確認してください。

For HTTP or SSE servers:

- Verify the URL and transport type.
- Check TLS and proxy settings.
- 静的ヘッダーが存在し、平文の秘密が含まれていないことを確認します。
- サーバーが OAuth を必要とする場合は、`iac-code mcp auth <server>`を実行します。

## Needs Authentication

状態: `needs-auth`.

Symptom:

```text
MCP server 'name' requires authentication.
```

Fix:

```bash
iac-code mcp auth name --scope user
```

サーバーが OAuth リフレッシュ トークンを使用しており、再認証が必要な場合、IaC コードは古いトークンをクリアし、新しいフローを要求します。

## OAuth Auth Failed

症状 (`auth-failed`):

```text
MCP auth failed for 'name':
```

OAuth flow は開始されましたが正常に完了していません。callback URL が不完全、authorization code が期限切れ、
または authorization server が error を返した可能性があります。新しい flow が完了前に失敗した場合、
IaC Code は以前の auth state を復元します。

修正:

```bash
iac-code mcp auth name --scope user
iac-code mcp reset-auth name --scope user
iac-code mcp auth name --scope user
```

まず `auth` を再試行します。保存済み token または dynamic client state が古い場合だけ、`reset-auth` 後に再試行します。

## OAuth Invalid Client

Symptom:

```text
invalid_client
```

IaC コードは、そのサーバーに保存されている OAuth クライアントとトークンの状態をクリアします。認証を再度実行します。

```bash
iac-code mcp auth name
```

## Insufficient Scope

Symptom:

```text
insufficient_scope
```

サーバーは追加の OAuth スコープを要求しました。現在のセッションで`/mcp`を開き、`認証`を選択するか、
そのサーバーを`再認証`します。 IaC コードには、そのフロー内のサーバー チャレンジによって報告されるスコープが含まれます。の
スタンドアロンの`iac-code mcp auth name`コマンドは通常の認証フローを開始し、チャレンジのみのスコープを実行しません。
previous session.

## Scope Ambiguity

Symptom:

```text
MCP server 'name' exists in multiple persisted scopes.
```

エラーに表示された正確な `--scope` command で再実行します。これは scope ambiguity です。server name は有効ですが、command には永続化された scope が 1 つ必要です。

## Capability Discovery Failed

Symptoms can include:

```text
MCP server 'name' tools discovery failed: ...
MCP server 'name' resources discovery failed: ...
MCP server 'name' prompts discovery failed: ...
```

サーバーは接続されましたが、1 つの機能リストが失敗しました。同じサーバーの他の機能は引き続き動作する可能性があります。サーバー側のエラーを修正してから、IaC コードを再起動するか、再接続/認証の更新をトリガーします。

## Session Expired

Symptom:

```text
MCP HTTP session expired
```

Run:

```bash
iac-code mcp reconnect name
```

失敗が繰り返される場合は、リモート サーバーがセッションをドロップしたか、再起動したかを確認してください。

## Headers Helper Failed

症状には、ヘルパー解析エラー、タイムアウト、ゼロ以外の終了ステータス、無効な JSON、または文字列以外のヘッダー値が含まれる場合があります。ヘルパー コマンドが構成ソース ディレクトリから有効であることを確認し、次のような JSON オブジェクトを出力します。

```json
{"X-Org": "platform"}
```

秘密のような標準エラー出力は診断で編集されます。

## WebSocket Config Rejected

WebSocket MCP サーバーは URL のみの構成をサポートします。 `headers`、`headersHelper`、および `oauth` を `type: "ws"` サーバーから削除します。

## Resources Are Missing

`list_mcp_resources` は、接続されている少なくとも 1 つのサーバーがリソースを公開する場合にのみ登録されます。ツールが見つからない場合:

- Confirm the server connected.
- サーバーが`resources/list`をサポートしていることを確認します。
- リソース検出エラーの起動警告を確認します。

## Prompt or Skill Command Missing

プロンプトとスキル コマンドは、検出が成功した後にのみ表示されます。確認してください:

- プロンプトまたは`skill://`リソースが MCP サーバー上に存在します。
・ 正規化されたコマンド名は組み込みコマンドと競合しません。
- リモートスキルリソースは起動タイムアウト以内に読み込むことができます。
- スキルの説明とボディは IaC コードの安全限界に適合しています。

## Logs and Artifacts

Runtime logs default to:

```text
<config-dir>/logs/
```

or `IAC_CODE_LOG_DIR` when set.

ツールの結果からの MCP バイナリ アーティファクトは、v2 セッションのセッション所有ディレクトリの下に保存されます。

```text
<config-dir>/projects/<project>/<session-id>/tool-results/mcp/
```

サポートされているレイアウト マーカーのないレガシー セッションでは、次のものが使用されます。

```text
<config-dir>/tool-results/<session-id>/mcp/
```

シークレットを確認せずに構成、ログ、またはアーティファクト ディレクトリを共有することは避けてください。
