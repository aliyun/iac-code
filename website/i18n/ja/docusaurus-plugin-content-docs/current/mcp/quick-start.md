---
sidebar_position: 2
title: MCP クイックスタート
description: Claude style commands で remote HTTP MCP server または stdio server を追加します。
---

# MCP クイックスタート

MCP server URL またはローカル stdio server command が分かっている場合は、この主経路を使います。

## リモート HTTP Server

位置 URL 形式で remote server を追加します。

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
```

server が OAuth を使う場合は、authorization flow を開始します。

```bash
iac-code mcp auth yuque
```

authorization 後、bounded health check を実行します。

```bash
iac-code mcp get yuque --check
```

## Stdio サーバー

stdio サーバーの場合は、サブプロセスのコマンドを `--` の後に指定します。リモート HTTP サーバーにラッパーは不要です。上の例のように `--transport http` で追加してください。

```bash
iac-code mcp add local-files --scope user -- npx -y @modelcontextprotocol/server-filesystem ./templates
```

次に、接続せずに configured server を確認します。

```bash
iac-code mcp get local-files --config-only
```

## 次のステップ

- scope、project files、headers、OAuth options、advanced JSON forms は [MCP 設定](./configuration.md) を参照してください。
- token storage、reset、approval behavior は [OAuth とセキュリティ](./oauth-and-security.md) を参照してください。
- pending approval、needs auth、connection failed の server は [トラブルシューティング](./troubleshooting.md) を参照してください。
