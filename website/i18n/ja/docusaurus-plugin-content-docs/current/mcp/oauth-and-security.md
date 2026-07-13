---
sidebar_position: 4
title: OAuth とセキュリティ
description: リモート MCP サーバーを認証し、IaC Code の MCP セキュリティモデルを理解します。
---

# OAuth とセキュリティ

MCP はローカル プロセスを開始してリモート サービスを呼び出すことができるため、IaC コードは MCP の構成と認証をセキュリティに依存するものとして扱います。

## OAuth

リモートの `http` および `sse` servers は OAuth を使用できます。OAuth metadata を公開し Dynamic Client Registration をサポートする標準準拠 server では、事前に client id を用意する必要はありません。server を追加してから auth を実行します。

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
```

サーバーが事前にプロビジョニングされたクライアントを必要とする場合は、サーバー構成で OAuth メタデータを構成します。

```json
{
  "mcpServers": {
    "secure-reviewer": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "oauth": {
        "clientId": "iac-code",
        "clientSecretEnv": "MCP_CLIENT_SECRET",
        "callbackPort": 38487,
        "authServerMetadataUrl": "https://auth.example.com/.well-known/oauth-authorization-server"
      }
    }
  }
}
```

Supported OAuth fields:

| Field | Purpose |
|---|---|
| `clientId` | OAuth client id. |
| `clientSecretEnv` |クライアントシークレットを含む環境変数。 |
| `callbackPort` |オプションのループバック コールバック ポート。空きポートを選択するには、「0」を使用するか省略します。 |
| `authServerMetadataUrl` |オプションの明示的な認可サーバーのメタデータ URL。 |
| `clientMetadataUrl` | client-id メタデータ ドキュメントをサポートする認可サーバーのオプションの HTTPS クライアント メタデータ ドキュメント URL。 |

平文の `oauth.clientSecret` は拒否されます。 `clientSecretEnv`または安全な CLI プロンプトを使用します。

## Authenticating

Run:

```bash
iac-code mcp auth secure-reviewer --scope user
```

IaC コードは認証 URL を開くか出力し、`127.0.0.1`でループバック コールバック サーバーを起動します。ブラウザを開けない場合、またはコールバックが自動的に完了できない場合は、コールバック URL または認証コードを CLI プロンプトに貼り付けます。承認後、IaC コードはコードをトークンと交換し、安全に保管します。

DCR 対応サーバーの場合、IaC コードは OAuth クライアントをサーバーに登録し、返されたクライアント ID とオプションのクライアント シークレットを MCP シークレット ストレージを通じて保存します。トークン交換とリフレッシュには、保護されたリソースのメタデータで必要な場合に、MCP SDK セマンティクスによって選択されたリソース パラメーターが含まれます。

通常のセッション中にサーバーが認証を必要とする場合、IaC コードは認証ツールを登録します。

```text
mcp__<server>__authenticate
```

モデルはそのツールを呼び出して、ユーザーに OAuth URL を提供できます。フローが完了すると、IaC コードは MCP サーバーに再接続し、検出された機能を更新します。

## Token Storage

IaC コードは、`MCPSecretStorage`を通じて OAuth トークンと MCP クライアント シークレットを保存します。

1. 利用可能な場合は、オペレーティング システムのキーリングを試行します。
2. キーリングが無効になっているか使用できない場合は、暗号化されたフォールバック データが `<config-dir>/mcp/` に保存されます。
3. ファイルのアクセス許可は、フォールバック キーと暗号化されたシークレット ストアに対して制限されます。

`IAC_CODE_MCP_DISABLE_KEYRING=1`を設定すると、暗号化されたフォールバック ストレージが強制的に使用されます。これは、分離されたテストに役立ちます。

保存されている認証状態をクリアするには、次のコマンドを使用します。

```bash
iac-code mcp reset-auth secure-reviewer --scope user
```

`reset-auth` は選択した永続化 scope の OAuth token state、dynamic client registration state、保存済み
`client_id`、任意の `client_secret`、OAuth signature index を消去しますが、server config は保持します。
永続化 server を削除する場合は、設定を削除する前に同じ auth-state cleanup を実行します。

```bash
iac-code mcp remove secure-reviewer --scope user
```

既存 server を再認可したいだけなら `reset-auth` を使います。server config 自体も消す場合は `mcp remove` を使います。
どちらの経路も `MCPSecretStorage` が管理する keyring と encrypted fallback entries を消去します。

## Project Trust

リポジトリは任意のローカル コードを実行する `stdio` サーバーを追加できるため、プロジェクトの `.mcp.json` ファイルは自動的には信頼されません。対話型承認はサーバー構成署名ごとに行われます。コマンド、引数、環境、URL、ヘッダー、または OAuth 設定を変更すると、以前の承認が無効になります。

ヘッドレス モードとプロトコル サーバー モードでは、未承認のプロジェクト サーバーはプロンプトを表示せずにスキップします。

## Secret Handling

IaC コードは、いくつかの方法で秘密を保護します。

- `iac-code mcp get` と `iac-code mcp get --config-only` の設定出力では、token、secret、password、API key、authorization header に見えるキーを秘匿化します。
- 機密性の高い header または env 値の平文は、`iac-code mcp add` または `mcp add-json` でサーバーを追加するときに拒否されます（環境変数参照を使う場合を除く）。手動で編集した設定ファイルは読み込み時に再検証されないため、平文の secret を直接保存しないでください。
- MCP stdio サーバーは、安全な環境変数 allowlist と明示的な server env だけを継承します。
- username または password を含む proxy 環境変数は stdio MCP サーバーに継承されません。
- `headersHelper` コマンドは shell なし、stdin なし、最小限の環境、制限付き stdout/stderr キャプチャ、秘匿化された private stderr diagnostics で実行されます。
- MCP artifact files は非公開の IaC Code runtime configuration directory に書き込まれます。

## Permissions

MCP ツールは、組み込みツールと同じ権限フレームワークを使用します。リモート MCP サーバーは、ツールをアドバタイズするだけでは IaC コードのアクセス許可チェックをバイパスできません。次のルールに留意してください。

- アクティブなアクセス許可ポリシーに応じて、読み取り専用 MCP ツールが自動的に許可される場合があります。
- 明示的に許可されていない限り、破壊的な MCP ツールには承認が必要です。
- ヘッドレス オートメーションでは、`--permission-mode`、`--allowed-tools`、および`--disallowed-tools`を組み合わせて、MCP ツールが実行できることを制限します。
- リモート MCP スキルは、独自の `allowed_tools` を付与しません。

## サポートされていないセキュリティ重視の機能

IaC コードは、現時点では次の MCP 機能を意図的に拒否または省略しています。

- Enterprise managed MCP policy.
- IDE and SDK transports.
- WebSocket headers、WebSocket `headersHelper`、WebSocket OAuth。
- IaC Code acting as an MCP server.
