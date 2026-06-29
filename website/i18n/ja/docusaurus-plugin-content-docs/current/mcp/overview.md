---
sidebar_position: 1
title: MCP 連携
description: Model Context Protocol サーバーを使って、外部ツール、リソース、プロンプト、スキルで IaC Code を拡張します。
---

# MCP 連携

IaC Code は Model Context Protocol (MCP) のホストとして動作できます。MCP サーバーは、外部ツール、リソース、プロンプト、再利用可能なスキルでエージェントを拡張します。これらは引き続き IaC Code の権限、セッション、ログ、出力処理の経路を通ります。

製品に組み込まれていないローカルまたはリモート機能を IaC Code から呼び出したい場合に MCP を使います。例として、社内テンプレートカタログ、内部デプロイレビュー、インベントリ照会サービス、特殊なクラウド操作ツールがあります。

## 対応する利用面

| 利用面 | MCP 対応 |
|---|---|
| 対話型 REPL | ユーザー、ローカル、承認済みプロジェクトサーバーを読み込みます。新しいプロジェクト `.mcp.json` サーバーを信頼する前に確認します。 |
| 非対話モード | ユーザー、ローカル、承認済みプロジェクトサーバーを読み込みます。確認は行わず、保留中のプロジェクトサーバーは warning とともにスキップされます。 |
| ACP server | ACP client からセッション作成時に渡された MCP server 設定を受け取り、そのセッション内で発見した MCP 機能を公開します。 |
| A2A server | 通常の runtime 経由で MCP を読み込み、A2A task metadata に MCP warning とツール進捗を出力できます。 |
| Pipeline モード | normal モードと同じ runtime 連携を使い、MCP ツール進捗と warning の伝播を含みます。 |

## 対応機能

| 機能 | 状態 |
|---|---|
| `stdio` transport | ローカル MCP server プロセスに対応。 |
| Streamable HTTP transport | リモート MCP server に対応。 |
| SSE transport | リモート MCP server に対応。 |
| MCP tools | `mcp__<server>__<tool>` という agent tool として公開。 |
| MCP resources | `list_mcp_resources` と `read_mcp_resource` で公開。 |
| MCP prompts | `mcp__<server>__<prompt>` という slash command として公開。 |
| MCP `skill://` resources | `mcp__<server>__<skill>` という skill command として公開。 |
| OAuth loopback auth | OAuth metadata を持つリモートサーバーに対応。 |
| `roots/list` | 対応。IaC Code は現在の workspace root を file URI として返します。 |
| `list_changed` notifications | tools、resources、prompts に対応。登録情報は動的に更新されます。 |
| MCP elicitation | まだ未対応。elicitation を要求するサーバーには明確な未対応エラーを返します。 |
| WebSocket、SDK、IDE transports | 未対応。 |
| 動的 `headersHelper` commands | 未対応。静的 headers または環境変数参照を使ってください。 |
| IaC Code を MCP server として実行 | 未対応。現在の IaC Code は MCP host のみです。 |

## 動作の流れ

実行時、IaC Code は次の処理を行います。

1. ユーザー、ローカル、プロジェクト、セッションの各ソースから MCP 設定を読み込みます。
2. `${VAR}` と `${VAR:-default}` 参照を展開します。
3. 安全でない、または無効なサーバーをユーザーに見える warning とともにスキップします。
4. 承認済みサーバーに制限付き並行数で接続します。
5. tools、resources、prompts、`skill://` resources を発見します。
6. それらの機能を既存の tool registry と command registry に登録します。
7. MCP tool result を通常の IaC Code tool result に変換し、バイナリ artifact を runtime 設定ディレクトリに保存します。
8. REPL、headless run、ACP session、A2A runtime の終了時に MCP client を切断します。

1 つの MCP server が失敗しても、他の設定済み server はブロックされません。接続と discovery の失敗は MCP warning として表示されます。

## 命名

MCP tools と commands は公開名に正規化されます。

```text
mcp__<server>__<tool>
mcp__<server>__<prompt>
mcp__<server>__<skill>
```

英数字とアンダースコア以外の文字はアンダースコアになります。正規化後に機能名が衝突した場合、IaC Code は短い digest を追加して名前を一意にします。

## 関連ページ

- [MCP 設定](./configuration.md)
- [ツール、リソース、プロンプト、スキル](./capabilities.md)
- [OAuth とセキュリティ](./oauth-and-security.md)
- [トラブルシューティング](./troubleshooting.md)
