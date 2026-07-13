---
sidebar_position: 2
title: MCP 設定
description: CLI コマンド、設定ファイル、プロジェクトファイル、ACP セッションで MCP サーバーを設定します。
---

# MCP 設定

MCP servers は `mcpServers` object の下で設定します。IaC Code は Claude Code 互換の core schema として `stdio`, `http`, `sse`, and URL-only `ws` servers をサポートします。

## クイックスタート

Yuque などのリモート HTTP MCP サーバーでは、位置 URL 形式でサーバーを追加してから OAuth を開始します。

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

`mcp-remote` などの stdio ラッパーの場合は、サブプロセス コマンドを `--` の後に置きます。

```bash
iac-code mcp add yuque-remote --scope user -- npx mcp-remote https://mcp.example.com/yuque/mcp
```

## Configuration Sources

IaC コードは、次のソースから MCP サーバーを読み取ります。

|出典 |範囲 |ファイルまたはエントリ ポイント |信頼モデル |
|---|---|---|---|
|ユーザー設定 | `user` | `~/.iac-code/settings.yml` または `IAC_CODE_CONFIG_DIR/settings.yml` |現在のユーザーから信頼されています。 |
|プロジェクトのローカル設定 | `local` | `<workspace>/.iac-code/settings.local.yml` |ローカルチェックアウト専用。 |
|プロジェクト MCP ファイル | `project` | `<workspace>/.mcp.json` |プロジェクトと共有され、ローカルの承認が必要です。 |
| ACP セッション構成 | `session` | ACP クライアントによって渡された `mcpServers` |その ACP セッション ランタイムにのみ適用されます。 |

優先順位はユーザー、プロジェクト、ローカル、セッションの順です。後のソースは、サーバー名によって以前のソースをオーバーライドします。同等の構成もコンテンツ署名によって重複排除されます。

プロジェクトの `.mcp.json` ファイルは、ワークスペースのルートから現在のディレクトリまで検出されます。子プロジェクト ファイルは、サーバー名によって親ファイルをオーバーライドします。

## CLI Commands

永続化された MCP 設定を管理するには、`iac-code mcp`を使用します。

```bash
iac-code mcp add local-catalog \
  --scope local \
  --command python \
  --arg ./tools/catalog_mcp.py
```

```bash
iac-code mcp add remote-reviewer \
  --scope user \
  --transport http \
  https://mcp.example.com/mcp \
  --header 'Authorization=${MCP_REVIEWER_TOKEN}'
```

リモート HTTP サーバーは、Claude スタイルの位置 URL フォームを使用して追加できます。

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

SSE サーバーと WebSocket サーバーも、対応する transport を指定して同じ位置 URL フォームを使用します。

```bash
iac-code mcp add --transport sse events https://mcp.example.com/sse
iac-code mcp add --transport ws realtime wss://mcp.example.com/mcp
```

`mcp-remote` などの stdio ラッパーの場合は、サブプロセス コマンドを `--` の後に置きます。

```bash
iac-code mcp add yuque-remote --scope user -- npx mcp-remote https://mcp.example.com/yuque/mcp
```

利用可能なコマンド:

| コマンド | 目的 |
|---|---|
| `iac-code mcp add` |構造化された CLI フラグからサーバーを追加します。 |
| `iac-code mcp add-json` | JSON オブジェクトからサーバーを追加します。 |
| `iac-code mcp list` | 接続せずに、設定済み server、scope、transport、approval status を一覧表示します。 |
| `iac-code mcp list --config-only` | 既定の config listing の alias です。 |
| `iac-code mcp list --check` | 短時間接続し、有界の health diagnostics を表示します。 |
| `iac-code mcp get` |接続せずに 1 つの編集されたサーバー構成を印刷します。 |
| `iac-code mcp get --config-only` |接続せずに 1 つの編集されたサーバー構成を印刷します。 |
| `iac-code mcp get --check` |短時間接続し、1 台のサーバーの限定された正常性診断を表示します。 |
| `iac-code mcp remove` |永続化スコープから 1 つのサーバーを削除します。 |
| `iac-code mcp approve` |プロジェクト `.mcp.json` サーバーを承認します。 |
| `iac-code mcp reject` |プロジェクト `.mcp.json` サーバーを拒否します。 |
| `iac-code mcp reset-project-choices` |保存されているプロジェクト承認の選択肢をクリアします。 |
| `iac-code mcp auth` |サーバーのOAuth認証を開始します。 |
| `iac-code mcp reset-auth` |保存されているサーバーの OAuth トークンとクライアント シークレットを削除します。 |
| `iac-code mcp reconnect` | 1 つのサーバー、または永続化されたすべてのサーバーを`--all`で再接続します。 |
| `iac-code mcp disable` |共有プロジェクト構成を編集せずに永続化サーバーを無効にします。 |
| `iac-code mcp enable` |永続化サーバーを再度有効にします。 |

## コマンドオプション

次の option set は `iac-code mcp <command> --help` に合わせています。

| コマンド | オプション |
|---|---|
| `iac-code mcp add` | `--command`, `--arg`, `--env`, `--type`, `--transport`, `--url`, `--header`, `--scope`, `--client-id`, `--client-secret`, `--client-secret-env`, `--callback-port`, `--auth-server-metadata-url` |
| `iac-code mcp add-json` | `--scope` |
| `iac-code mcp list` | `--check`, `--config-only` |
| `iac-code mcp get` | `--scope`, `--source-path`, `--check`, `--config-only` |
| `iac-code mcp remove` | `--scope`, `--source-path` |
| `iac-code mcp approve` | No command-specific options。追加は `--help` のみです。 |
| `iac-code mcp reject` | No command-specific options。追加は `--help` のみです。 |
| `iac-code mcp reset-project-choices` | No command-specific options。追加は `--help` のみです。 |
| `iac-code mcp auth` | `--scope`, `--source-path` |
| `iac-code mcp reset-auth` | `--scope`, `--source-path` |
| `iac-code mcp reconnect` | `--all`, `--scope`, `--source-path` |
| `iac-code mcp disable` | `--scope`, `--source-path` |
| `iac-code mcp enable` | `--scope`, `--source-path` |

`--scope` が省略された場合、IaC コードはプロジェクト内の `local` とプロジェクト外の `user` に書き込みます。

既存の永続化サーバー上で動作するコマンドの場合、`--scope`を省略すると、IaC コードは永続化スコープ全体で一意のサーバーを見つけることができます。同じ名前が複数のスコープに存在する場合、曖昧さを解消するための正確な `--scope` コマンドではコマンドは失敗します。

## 対話型 MCP マネージャー

対話 REPL では、`/mcp` が全画面の MCP マネージャーを開きます。サーバーをソース別にグループ化し、接続状態、認証状態、設定診断、失敗詳細、設定場所を表示します。

マネージャーでは、接続済みサーバーの tools、resources、prompts を確認できます。リモートサーバーの authenticate、re-authenticate、clear authentication、サーバーの再接続、永続化サーバーの有効化/無効化、プロジェクト `.mcp.json` サーバーの承認/拒否、永続化エントリの削除も実行できます。OAuth フローは認可 URL を表示し、コピーに対応し、ブラウザーリダイレクトがローカル callback listener に到達できない場合は貼り付けた callback URL または認可コードを受け付けます。

`/mcp enable <name>`、`/mcp disable <name>`、`/mcp reconnect <name>` は、マネージャーを開かずにクイック操作を実行します。`/mcp` がパイプされた stdin やその他の非 TTY 入力から渡された場合、IaC Code は端末が必要であることを表示します。非対話の自動化には `iac-code mcp <command>` を使用してください。

## Stdio Servers

Stdio servers launch a local command:

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

`command`が存在する場合、`type`フィールドは省略できます。 IaC コードは、安全に継承された環境とサーバーの`env`を渡します。 Windows では、ノードベースのサーバーには裸の `npx` ではなく `cmd /c npx` を優先します。

## HTTP and SSE Servers

リモートサーバーには`type`と`url`が必要です。

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

SSE サーバーには`type: "sse"`を使用します。静的ヘッダーは、`KEY=VALUE`または`Name: Value` CLI 構文でサポートされます。

動的ヘッダーは `headersHelper` で提供できます：

```json
{
  "mcpServers": {
    "reviewer": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "X-Org": "platform"
      },
      "headersHelper": "python ./scripts/mcp_headers.py"
    }
  }
}
```

helper は、キーと値がどちらも文字列である JSON object を出力する必要があります。動的ヘッダーは同名の静的ヘッダーを上書きします。IaC Code は helper を shell なし、stdin なし、最小限の継承環境、設定ソース directory を cwd、5 秒 timeout、脱敏済み stderr diagnostics で実行します。`headersHelper` command string は環境変数展開されません。参照された変数は helper 環境に渡され、helper 側で読み取る必要があります。project `.mcp.json` の helper は、project approval 後にだけ実行できます。

## WebSocket Servers

WebSocket servers use `type: "ws"`:

```json
{
  "mcpServers": {
    "events": {
      "type": "ws",
      "url": "wss://mcp.example.com/mcp"
    }
  }
}
```

インストールされた MCP SDK WebSocket トランスポートは URL のみを受け入れます。 IaC コードは、`headers`、`headersHelper`、または`oauth`も設定されている WebSocket 構成を拒否します。

## Environment Expansion

String values support:

```text
${VAR}
${VAR:-default-value}
```

default のない欠落変数は MCP warning を生成し、対象 server はスキップされます。環境変数展開は list と object 内の文字列に再帰的に適用されますが、`headersHelper` command string は例外です。この文字列は literal のまま保持され、参照された変数は helper 環境経由で渡されます。

プレーンテキストのシークレットをヘッダーまたは環境値に保存しないでください。環境変数参照または OAuth シークレット ストレージを使用します。

## Project Approval

プロジェクト `.mcp.json` はリポジトリにコミットできるため、IaC コードはそれを自動的に信頼しません。

Interactive REPL startup asks:

```text
Approve project MCP server 'name' from /path/to/.mcp.json? [y/N]
```

Enter キーを押すと、デフォルトの`N`が維持され、その正確なプロジェクト サーバー構成が拒否されます。承認するには「y」または「yes」を入力します。承認は IaC コード構成ディレクトリの下にローカルに保存され、ワー​​クスペース パス、プロジェクト ファイル パス、サーバー名、構成署名が含まれます。 `.mcp.json`サーバー構成が変更されると、承認は無効になり、サーバーは再び保留状態になります。

ヘッドレス、ACP、および A2A のスタートアップでは、対話型の承認の質問をすることはありません。保留中のプロジェクト サーバーはスキップされ、警告として報告されます。

## Disabled Servers

`iac-code mcp disable <name>` は、IaC Code config ディレクトリの下にプライベートの無効状態エントリを保存します。プロジェクト スコープのサーバーの場合、共有の `.mcp.json` ファイルは変更されません。無効なエントリはスコープ、ソース ファイル、サーバー名、および構成署名によってキー設定されるため、サーバー構成を変更すると、古い無効状態が無効になります。
