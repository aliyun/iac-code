---
sidebar_position: 1
title: MCP 連携
description: Model Context Protocol サーバーで IaC Code に外部ツール、リソース、プロンプト、スキルを拡張します。
---

# MCP 連携

IaC Code は Model Context Protocol (MCP) host として動作できます。MCP servers は IaC Code の permission、session、logging、output handling の経路を通りながら、agent に外部 tools、resources、prompts、reusable skills を拡張します。

IaC コードで、プライベート テンプレート カタログ、内部展開レビュー担当者、インベントリ クエリ サービス、特殊なクラウド操作ツールなど、製品に組み込まれていないローカルまたはリモートの機能を呼び出す場合は、MCP を使用します。

## Supported Surfaces

| Surface | MCP support |
|---|---|
|インタラクティブ REPL |ユーザー、ローカル、および承認されたプロジェクト サーバーを読み込みます。新しいプロジェクトの `.mcp.json` サーバーを信頼する前にプロンプ​​トが表示されます。 |
|非対話型モード |ユーザー、ローカル、および承認されたプロジェクト サーバーを読み込みます。決してプロンプトを表示しません。保留中のプロジェクト サーバーは警告とともにスキップされます。 |
| ACPサーバー | ACP クライアントからセッション MCP サーバー設定を受け入れ、そのセッション内で検出された MCP 機能を公開します。 |
| A2Aサーバー |通常のランタイムを通じて MCP をロードし、MCP 警告とツールの進行状況を A2A タスク メタデータで公開できます。 |
|パイプラインモード | MCP ツールの進行状況や警告の伝達など、通常モードと同じランタイム統合を使用します。 |

## Supported Capabilities

| Capability | Status |
|---|---|
| `stdio` トランスポート |ローカル MCP サーバー プロセスでサポートされています。 |
|ストリーミング可能な HTTP トランスポート |リモート MCP サーバーでサポートされています。 |
| SSEトランスポート |リモート MCP サーバーでサポートされています。 |
| MCP ツール | `mcp__<server>__<tool>` という名前のエージェント ツールとして公開されます。 |
| MCP リソース | `list_mcp_resources` および `read_mcp_resource` を通じて公開されます。 |
| MCP プロンプト | `mcp__<server>__<prompt>` という名前のスラッシュ コマンドとして公開されます。 |
| MCP `skill://` リソース | `mcp__<server>__<skill>` という名前のスキル コマンドとして公開されます。 |
| OAuth ループバック認証 | OAuth メタデータを含むリモート サーバーでサポートされます。 |
| `roots/list` |サポートされています。 IaC コードは、アクティブなワークスペースのルートをファイル URI として返します。 |
| `list_changed` 通知 |ツール、リソース、プロンプトがサポートされています。登録は動的に更新されます。 |
| MCP elicitation | interactive session でサポートされます。non-interactive run では安全に cancel されます。URL elicitation はユーザー確認後に元の tool call を retry できます。 |
| WebSocket transport | URL のみの `ws://` と `wss://` server をサポートします。installed SDK transport は URL のみを受け取るため、WebSocket では headers、`headersHelper`、OAuth が拒否されます。 |
| 動的 `headersHelper` commands | trusted `http` と `sse` server でサポートされます。helper は shell なし、有界 timeout、最小環境、脱敏 diagnostics で実行されます。 |
| SDK および IDE トランスポート |サポートされていません。 |
| MCP サーバーとしての IaC コード |サポートされていません。 IaC コードは現在、MCP ホストとしてのみ機能します。 |

## How It Works

At runtime IaC Code:

1. ユーザー、プロジェクト、ローカル、セッションの各ソースから MCP 設定を読み込みます。
2. `${VAR}` と `${VAR:-default}` 参照を展開します。
3. 安全でない、または無効なサーバーをユーザーに見える warning とともにスキップします。
4. 承認済みサーバーに制限付き並行数で接続します。
5. tools、resources、prompts、`skill://` resources を発見します。
6. それらの機能を既存の tool registry と command registry に登録します。
7. 接続済みサーバーの instructions を server-scoped guidance として agent prompt に注入します。
8. MCP tool result を通常の IaC Code tool result に変換し、バイナリ artifact と大きな text artifact を runtime 設定ディレクトリに保存します。
9. REPL、headless run、ACP session、A2A runtime の終了時に MCP client を切断します。

1 台の MCP サーバーに障害が発生しても、他の構成済みサーバーはブロックされません。接続と検出の失敗は、MCP 警告として表示されたままになります。

## Naming

MCP ツールとコマンドは、パブリック名に正規化されます。

```text
mcp__<server>__<tool>
mcp__<server>__<prompt>
mcp__<server>__<skill>
```

文字、数字、アンダースコア以外の文字はアンダースコアになります。検出された 2 つの機能が正規化後に衝突した場合、IaC コードは名前を一意に保つために短いダイジェストを追加します。

MCP スキルの場合、IaC コードは、エイリアスが既存のコマンドと競合しない場合、`<server>:<skill>` などの互換性エイリアスも登録します。診断では、パブリック名が正規化されている場合でも、元のサーバー、ツール、プロンプト、またはスキルの名前が保持されます。

## Related Pages

- [MCP クイックスタート](./quick-start.md)
- [MCP 設定](./configuration.md)
- [ツール、リソース、プロンプト、スキル](./capabilities.md)
- [OAuth とセキュリティ](./oauth-and-security.md)
- [トラブルシューティング](./troubleshooting.md)
