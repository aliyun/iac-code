---
sidebar_position: 5
title: MCP トラブルシューティング
description: MCP 設定、接続、認証、機能 discovery の問題を診断します。
---

# MCP トラブルシューティング

MCP warnings は、必要な機能がすべて利用不可でない限り通常は致命的ではありません。1 つの server が失敗しても、他の MCP servers や IaC Code 組み込み tools は動作し続けるべきです。

## 設定を確認する

設定済み servers を一覧表示します。

```bash
iac-code mcp list
```

秘匿化された server config を確認します。

```bash
iac-code mcp get my-server --scope local
```

問題のある server を削除します。

```bash
iac-code mcp remove my-server --scope local
```

project approval choices を消去します。

```bash
iac-code mcp reset-project-choices
```

## Project Server が Pending

症状:

```text
Project MCP server 'name' is pending approval.
```

修正:

```bash
iac-code mcp approve name
```

または、そのプロジェクトで対話型 REPL を起動し、確認時に `y` と答えます。Enter は `N` を意味し、server を拒否します。

以前は approval が有効だったのに動かなくなった場合は、`.mcp.json` が変更されていないか確認してください。Approval は config signature に紐づきます。

## 環境変数がない

症状:

```text
Environment variable 'TOKEN' is not set for MCP config.
```

次のいずれかで修正します。

```bash
export TOKEN=...
```

または default を使います。

```json
"Authorization": "${TOKEN:-}"
```

必須環境変数が不足している servers はスキップされます。

## 接続失敗

stdio servers の場合:

- `command` が `PATH` 上に存在することを確認します。
- 別ディレクトリから起動する場合、スクリプトには絶対パスを使います。
- Windows では Node-based servers を `cmd /c npx` 経由で実行します。
- 必要な環境変数が設定されているか確認します。

HTTP または SSE servers の場合:

- URL と transport type を確認します。
- TLS と proxy settings を確認します。
- 静的 headers があり、plaintext secrets を含んでいないことを確認します。
- server が OAuth を要求する場合は `iac-code mcp auth <server>` を実行します。

## 認証が必要

症状:

```text
MCP server 'name' requires authentication.
```

修正:

```bash
iac-code mcp auth name --scope user
```

server が OAuth refresh tokens を使っていて再認証が必要な場合、IaC Code は古い tokens を消去し、新しいフローを要求します。

## Capability Discovery Failed

症状の例:

```text
MCP server 'name' tools discovery failed: ...
MCP server 'name' resources discovery failed: ...
MCP server 'name' prompts discovery failed: ...
```

server には接続できていますが、ある capability list が失敗しています。同じ server の他の機能は動作する場合があります。server 側のエラーを修正し、IaC Code を再起動するか reconnect/auth refresh を発生させてください。

## Resources が見つからない

`list_mcp_resources` は、接続済み server の少なくとも 1 つが resources を公開している場合だけ登録されます。tool がない場合:

- server が接続されていることを確認します。
- server が `resources/list` をサポートしていることを確認します。
- 起動時 warnings に resource discovery errors がないか確認します。

## Prompt または Skill Command が見つからない

Prompt と skill commands は discovery 成功後にのみ現れます。次を確認してください。

- 対象の prompt または `skill://` resource が MCP server 上に存在する。
- 正規化後の command name が built-in command と衝突していない。
- Remote skill resource が起動時 timeout 内に読み取れる。
- Skill description と body が IaC Code の安全制限内に収まっている。

## Logs と Artifacts

Runtime logs の既定場所:

```text
<config-dir>/logs/
```

`IAC_CODE_LOG_DIR` が設定されている場合はそのディレクトリを使います。

MCP tool results の binary artifacts は次に保存されます。

```text
<config-dir>/tool-results/<session-id>/mcp/
```

config、log、artifact directories を共有する前に、secrets が含まれていないか確認してください。
