---
sidebar_position: 4
title: OAuth とセキュリティ
description: リモート MCP server を認証し、IaC Code の MCP セキュリティモデルを理解します。
---

# OAuth とセキュリティ

MCP はローカルプロセスの起動やリモートサービス呼び出しができるため、IaC Code は MCP 設定と認証をセキュリティ上重要なものとして扱います。

## OAuth

リモート `http` と `sse` servers は OAuth を利用できます。Server config に OAuth metadata を設定します。

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

対応する OAuth fields:

| Field | 用途 |
|---|---|
| `clientId` | OAuth client id。 |
| `clientSecretEnv` | client secret を含む環境変数。 |
| `callbackPort` | 任意の loopback callback port。`0` または省略で空きポートを選びます。 |
| `authServerMetadataUrl` | 任意の明示的な authorization server metadata URL。 |

Plaintext `oauth.clientSecret` は拒否されます。`clientSecretEnv` または安全な CLI prompt を使ってください。

## 認証

次を実行します。

```bash
iac-code mcp auth secure-reviewer --scope user
```

IaC Code は authorization URL を開くか表示し、`127.0.0.1` で loopback callback server を起動します。Provider が authorization code 付きで戻ってくると、IaC Code は token と交換して安全に保存します。

通常セッション中に server が認証を必要とする場合、IaC Code は authentication tool を登録します。

```text
mcp__<server>__authenticate
```

モデルはこの tool を呼び出して、OAuth URL をユーザーに提示できます。フロー完了後、IaC Code は MCP server に再接続し、発見済み機能を更新します。

## Token Storage

IaC Code は `MCPSecretStorage` で OAuth tokens と MCP client secrets を保存します。

1. 利用できる場合は OS keyring を優先します。
2. keyring が無効または利用不可の場合、`<config-dir>/mcp/` に暗号化 fallback data を保存します。
3. fallback key と暗号化 secret store には制限されたファイル権限を設定します。

隔離テストでは `IAC_CODE_MCP_DISABLE_KEYRING=1` を設定すると encrypted fallback storage を強制できます。

保存された auth state を消すには次を使います。

```bash
iac-code mcp reset-auth secure-reviewer --scope user
```

## プロジェクト信頼

プロジェクト `.mcp.json` は自動的には信頼されません。リポジトリが任意のローカルコードを実行する `stdio` server を追加できるためです。対話型 approval は server config signature ごとに紐づきます。command、args、env、URL、headers、OAuth config を変更すると以前の approval は無効になります。

Headless と protocol server modes は、未承認の project servers を確認せずスキップします。

## Secret Handling

IaC Code は複数の方法で secrets を保護します。

- `iac-code mcp get` の config 出力では、token、secret、password、API key、authorization header に見える keys を秘匿化します。
- 機密性の高い header または env 値の plaintext は、環境変数参照でない限り拒否されます。
- MCP stdio servers は、安全な環境変数 allowlist と明示的な server env だけを継承します。
- username または password を含む proxy 環境変数は stdio MCP servers に継承されません。
- MCP artifact files は非公開の IaC Code runtime configuration directory に書き込まれます。

## 権限

MCP tools は組み込み tools と同じ権限フレームワークを使います。リモート MCP server は tool を広告するだけで IaC Code の権限チェックを回避することはできません。次の点に注意してください。

- Read-only MCP tools は、現在の権限ポリシーによって自動許可される場合があります。
- Destructive MCP tools は明示的に許可されていない限り approval が必要です。
- Headless automation では、`--permission-mode`、`--allowed-tools`、`--disallowed-tools` を組み合わせて MCP tools の操作範囲を制限します。
- Remote MCP skills は独自の `allowed_tools` を付与しません。

## 未対応のセキュリティ関連機能

IaC Code は現在、次の MCP 機能を意図的に拒否または省略しています。

- `headersHelper` dynamic commands。
- MCP elicitation UI。
- WebSocket、IDE、SDK transports。
- Enterprise managed MCP policy。
- IaC Code を MCP server として実行すること。
