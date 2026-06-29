---
sidebar_position: 2
title: MCP 設定
description: CLI コマンド、settings ファイル、プロジェクトファイル、ACP セッションで MCP server を設定します。
---

# MCP 設定

MCP server は `mcpServers` オブジェクトの下に設定します。IaC Code は Claude Code と互換性のある中心的な schema をサポートし、`stdio`、`http`、`sse` servers に対応します。

## 設定ソース

IaC Code は次のソースから MCP servers を読み込みます。

| ソース | Scope | ファイルまたは入口 | 信頼モデル |
|---|---|---|---|
| ユーザー settings | `user` | `~/.iac-code/settings.yml` または `IAC_CODE_CONFIG_DIR/settings.yml` | 現在のユーザーが信頼する設定。 |
| プロジェクトローカル settings | `local` | `<workspace>/.iac-code/settings.local.yml` | ローカル checkout だけの非公開設定。 |
| プロジェクト MCP ファイル | `project` | `<workspace>/.mcp.json` | プロジェクトで共有され、ローカル承認が必要。 |
| ACP セッション設定 | `session` | ACP client から渡される `mcp_servers` | その ACP session runtime のみに適用。 |

優先順位は user、project、local、session です。後のソースは server name ごとに前のソースを上書きします。同等の設定は content signature でも重複排除されます。

プロジェクト `.mcp.json` ファイルは workspace root から現在のディレクトリまで探索されます。子プロジェクトのファイルは server name ごとに親のファイルを上書きします。

## CLI コマンド

永続化された MCP 設定は `iac-code mcp` で管理します。

```bash
iac-code mcp add local-catalog \
  --scope local \
  --command python \
  --arg ./tools/catalog_mcp.py
```

```bash
iac-code mcp add remote-reviewer \
  --scope user \
  --type http \
  --url https://mcp.example.com/mcp \
  --header "Authorization=${MCP_REVIEWER_TOKEN}"
```

利用できるコマンド:

| コマンド | 用途 |
|---|---|
| `iac-code mcp add` | 構造化された CLI flags から server を追加します。 |
| `iac-code mcp add-json` | JSON object から server を追加します。 |
| `iac-code mcp list` | 設定済み server、scope、transport、approval status を一覧表示します。 |
| `iac-code mcp get` | 1 つの server config を秘匿化して出力します。 |
| `iac-code mcp remove` | 永続化 scope から server を削除します。 |
| `iac-code mcp approve` | プロジェクト `.mcp.json` server を承認します。 |
| `iac-code mcp reject` | プロジェクト `.mcp.json` server を拒否します。 |
| `iac-code mcp reset-project-choices` | 保存された project approval choices を消去します。 |
| `iac-code mcp auth` | server の OAuth 認証を開始します。 |
| `iac-code mcp reset-auth` | server の保存済み OAuth tokens と client secret を削除します。 |

`--scope` を省略した場合、IaC Code はプロジェクト内では `local`、プロジェクト外では `user` に書き込みます。

## Stdio Servers

Stdio servers はローカルコマンドを起動します。

```json
{
  "mcpServers": {
    "catalog": {
      "command": "python",
      "args": ["./tools/catalog_mcp.py"],
      "env": {
        "CATALOG_ENV": "prod"
      }
    }
  }
}
```

`command` がある場合、`type` フィールドは省略できます。IaC Code は安全な継承環境と server の `env` を渡します。Windows では、Node-based server に裸の `npx` ではなく `cmd /c npx` を使うことを推奨します。

## HTTP と SSE Servers

リモート server には `type` と `url` が必要です。

```json
{
  "mcpServers": {
    "reviewer": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "${MCP_REVIEWER_TOKEN}"
      }
    }
  }
}
```

SSE server には `type: "sse"` を使います。静的 headers はサポートされます。動的 `headersHelper` commands は、別途 trusted-execution design が必要なため拒否されます。

## 環境変数展開

文字列値では次を利用できます。

```text
${VAR}
${VAR:-default-value}
```

デフォルト値のない不足変数は MCP warning を発生させ、該当 server はスキップされます。環境変数展開は list と object 内の文字列にも再帰的に適用されます。

headers や env 値に plaintext secrets を保存しないでください。環境変数参照または OAuth secret storage を使ってください。

## プロジェクト承認

プロジェクト `.mcp.json` はリポジトリにコミットできるため、IaC Code は自動的には信頼しません。

対話型 REPL の起動時に次のように確認します。

```text
Approve project MCP server 'name' from /path/to/.mcp.json? [y/N]
```

Enter を押すと既定の `N` になり、そのプロジェクト server config を拒否します。承認するには `y` または `yes` を入力します。Approval は IaC Code config ディレクトリにローカル保存され、workspace path、project file path、server name、config signature を含みます。`.mcp.json` の server config が変わると approval は無効化され、server は再び pending になります。

Headless、ACP、A2A の起動時は対話的な approval 質問を行いません。未承認の project servers はスキップされ warning として報告されます。
