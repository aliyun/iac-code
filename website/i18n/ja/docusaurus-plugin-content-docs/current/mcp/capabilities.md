---
sidebar_position: 3
title: ツール、リソース、プロンプト、スキル
description: MCP 機能が IaC Code 内でどのように見えるかを説明します。
---

# ツール、リソース、プロンプト、スキル

接続済み MCP servers は 4 種類の機能を IaC Code に公開できます。

## Tools

各 MCP tool は IaC Code tool になります。

```text
mcp__<server>__<tool>
```

Tool description と JSON input schema は MCP server から取得されます。IaC Code はモデルの tool input を MCP server に転送し、MCP content blocks を通常の tool result に変換します。

可能な場合、MCP tool annotations は尊重されます。

| MCP annotation | IaC Code の動作 |
|---|---|
| `readOnlyHint: true` | tool は読み取り専用で並行実行安全として扱われます。 |
| `destructiveHint: true` | tool は権限判断で破壊的操作として扱われます。 |

MCP tools も IaC Code の既存権限システムを通ります。通常の `permissions` settings、または `--allowed-tools`、`--disallowed-tools`、`--permission-mode` などの CLI flags で権限ポリシーを設定します。

MCP progress notifications は、対話型 rendering、headless progress output、ACP tool progress updates、A2A tool metadata に表示されます。

## Tool Results と Artifacts

IaC Code は MCP content blocks をモデルに見えるテキストへ変換します。

| MCP content | IaC Code result |
|---|---|
| Text content | tool result に直接含めます。 |
| `structuredContent` | structured-content section に整形済み JSON として表示します。 |
| Text resources | server と URI の出所情報付きで表示します。 |
| `resource_link` | URI と MIME type 付きの resource link として表示します。 |
| Image、audio、blob data | 非公開 artifact ファイルとして保存し、artifact id で参照します。 |

バイナリ artifacts は次の場所に保存されます。

```text
<config-dir>/tool-results/<session-id>/mcp/<server>/<tool>/
```

モデルが見るのは artifact id と metadata であり、raw base64 data ではありません。

## Resources

接続済み server のいずれかが resources を公開すると、IaC Code は 2 つの global tools を登録します。

| Tool | 用途 |
|---|---|
| `list_mcp_resources` | 接続済み MCP servers の resources を一覧表示します。server name で任意に絞り込めます。 |
| `read_mcp_resource` | `server` と `uri` で 1 つの resource を読み取ります。 |

Resource 行には server name、URI、任意の resource name、任意の MIME type が含まれます。

## Prompts

MCP prompts は slash commands になります。

```text
/mcp__<server>__<prompt> key=value
```

呼び出すと、IaC Code は MCP `prompts/get` を呼び出し、返された prompt messages をレンダリングし、レンダリング済み prompt を会話に注入してモデルを続行させます。Prompt arguments は次の形式で渡せます。

```text
template_name=prod-vpc region=cn-hangzhou
```

または JSON で渡せます。

```json
{"template_name": "prod-vpc", "region": "cn-hangzhou"}
```

Required prompt arguments は MCP call の前に検証されます。引用符付きの値に対応し、バックスラッシュを含む Windows paths も扱えます。

## Skills

`skill://` URI を持つ MCP resources は skill commands になります。

```text
$mcp__<server>__<skill>
```

IaC Code は remote skill resource を読み取り、frontmatter を解析し、通常の skill command として登録します。Remote MCP skills には安全上の制限があります。

- Remote `allowed_tools` は消去されます。
- Remote auto-trigger path rules は消去されます。
- Remote skill body と description には長さ制限があります。
- Remote skill が既存 command と衝突する場合、MCP warning とともにスキップされます。

MCP skill resources は、ユーザーが呼び出す前に command を登録できるよう、起動時に読み取られる場合があります。

## 動的更新

MCP server が `tools/list_changed`、`resources/list_changed`、`prompts/list_changed` を送信すると、IaC Code は対象の capability list を更新し、tool または command registry を更新します。更新失敗は MCP warning として報告され、アクティブなセッションを停止しません。
