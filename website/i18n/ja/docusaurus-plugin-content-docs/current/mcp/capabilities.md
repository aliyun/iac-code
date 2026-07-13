---
sidebar_position: 3
title: ツール、リソース、プロンプト、スキル
description: MCP の機能が IaC Code 内でどのように表示されるかを理解します。
---

# ツール、リソース、プロンプト、スキル

接続済みの MCP servers は IaC Code に 4 種類の capabilities を公開できます。

## Tools

Each MCP tool becomes an IaC Code tool:

```text
mcp__<server>__<tool>
```

ツールの説明と JSON 入力スキーマは MCP サーバーから取得されます。 IaC コードは、モデルのツール入力を MCP サーバーに転送し、MCP コンテンツ ブロックを通常のツール結果に変換します。

権限プロンプトと監査メタデータには、MCP サーバー名、元のツール名、公開正規化ツール名、読み取り専用/破壊的な注釈が含まれます。

MCP ツールの注釈は可能な場合には尊重されます。

| MCP annotation | IaC Code behavior |
|---|---|
| `readOnlyHint: true` |このツールは読み取り専用で同時実行安全なものとして扱われます。 |
| `destructiveHint: true` |このツールは、権限の決定に関して破壊的なものとして扱われます。 |

MCP ツールは引き続き IaC コードの既存の権限システムを通過します。通常の`permissions`設定、または`--allowed-tools`、`--disallowed-tools`、`--permission-mode`などの CLI フラグを使用してアクセス許可ポリシーを構成します。

MCP 進行状況通知は、インタラクティブ レンダリング、ヘッドレス進行状況出力、ACP ツール進行状況更新、および A2A ツール メタデータで表示されます。

## Tool Results and Artifacts

IaC コードは、MCP コンテンツ ブロックをモデルに表示されるテキストに変換します。

| MCP content | IaC Code result |
|---|---|
| Text content | Included directly in the tool result when small; 大きな text は private な `.txt`、`.json`、または `.md` artifact として保存されます. |
| `structuredContent` |構造化コンテンツセクションの下にフォーマットされた JSON としてレンダリングされます。 |
|テキストリソース |サーバーと URI の出自を使用してレンダリングされます。 |
| `resource_link` | URI と MIME タイプを使用したリソース リンクとしてレンダリングされます。 |
|画像、音声、BLOB データ |プライベート アーティファクト ファイルとして保存され、アーティファクト ID によって参照されます。 |

バイナリ アーティファクトは、v2 セッションのセッション所有の MCP ツール結果ディレクトリに保存されます。

```text
<config-dir>/projects/<project>/<session-id>/tool-results/mcp/<server>/<tool>/
```

サポートされているレイアウト マーカーのないレガシー セッションでは、引き続き以下が使用されます。

```text
<config-dir>/tool-results/<session-id>/mcp/<server>/<tool>/
```

The model sees the artifact id and metadata, not raw base64 data. 大きな text artifact には path が含まれます so the full output can be read without flooding the conversation.

## Resources

接続されたサーバーがリソースを公開すると、IaC コードは 2 つのグローバル ツールを登録します。

| Tool | Purpose |
|---|---|
| `list_mcp_resources` |接続されている MCP サーバーからのリソースをリストします。必要に応じて、サーバー名でフィルタリングします。 |
| `read_mcp_resource` | `server`と`uri`によって1つのリソースを読み取ります。 |

リソース行には、サーバー名、URI、オプションのリソース名、およびオプションの MIME タイプが含まれます。

## Prompts

MCP prompts become slash commands:

```text
/mcp__<server>__<prompt> key=value
```

呼び出されると、IaC コードは MCP `prompts/get` を呼び出し、返されたプロンプト メッセージをレンダリングし、レンダリングされたプロンプトを会話に挿入して、モデルを続行させます。プロンプト引数は次のように渡すことができます。

```text
template_name=prod-vpc region=cn-hangzhou
```

or as JSON:

```json
{"template_name": "prod-vpc", "region": "cn-hangzhou"}
```

必須のプロンプト引数は、MCP 呼び出しの前に検証されます。バックスラッシュを含む Windows パスを含め、引用符で囲まれた値がサポートされています。

## Skills

`skill://` URI を持つ MCP リソースはスキル コマンドになります。

```text
$mcp__<server>__<skill>
```

IaC コードは、リモート スキル リソースを読み取り、frontmatter を解析し、それを通常のスキル コマンドとして登録します。リモート MCP スキルには安全上の制限があります。

- Remote `allowed_tools` are cleared.
- リモート自動トリガー パス ルールがクリアされます。
- リモートスキル本体と説明の長さには制限があります。
- リモート スキルが既存のコマンドと競合する場合、MCP 警告が表示されてスキップされます。

MCP スキル リソースは起動時に読み取られるため、ユーザーがコマンドを呼び出す前にコマンドを登録できます。

コマンドの競合がない場合、MCP スキルには互換性エイリアスも取得されます。

```text
$<server>:<skill>
```

たとえば、`$mcp__yuque__search` と `$yuque:search` は同じリモート スキルに解決できます。

## Server Instructions（サーバー指示）

接続されたサーバーが初期化から「命令」を返した場合、IaC コードはそれらを専用の MCP サーバー命令セクションとしてエージェント プロンプトに挿入します。これらの指示はサーバースコープのガイダンスとして扱われ、ローカル プロジェクトの指示をオーバーライドしません。

## Elicitation（対話要求）

interactive session では MCP elicitation request をユーザーへ渡せます。URL mode elicitation は外部 URL flow の完了をユーザーに求め、その後 bounded retry limit 内で元の MCP tool call を retry できます。non-interactive context では elicitation を安全に cancel します。

## Dynamic Updates

MCP サーバーが`tools/list_changed`、`resources/list_changed`、または`prompts/list_changed`を送信すると、IaC コードは影響を受ける機能リストを更新し、ツールまたはコマンド レジストリを更新します。更新の失敗は MCP 警告として報告され、アクティブなセッションは停止されません。
