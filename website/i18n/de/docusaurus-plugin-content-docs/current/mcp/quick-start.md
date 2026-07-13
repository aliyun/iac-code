---
sidebar_position: 2
title: MCP Schnellstart
description: Remote HTTP MCP server oder stdio wrapper mit Claude-style Befehlen hinzufuegen.
---

# MCP Schnellstart

Verwenden Sie diesen Einstieg, wenn Sie bereits eine MCP server URL oder einen lokalen stdio wrapper command haben.

## Remote HTTP Server

Fuegen Sie den remote server mit der positionalen URL-Form hinzu:

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
```

Wenn der server OAuth verwendet, starten Sie den authorization flow:

```bash
iac-code mcp auth yuque
```

Nach der Autorisierung fuehren Sie einen begrenzten health check aus:

```bash
iac-code mcp get yuque --check
```

## Stdio Wrapper

Fuer wrapper wie `mcp-remote` setzen Sie den subprocess command hinter `--`:

```bash
iac-code mcp add yuque-remote --scope user -- npx mcp-remote https://mcp.example.com/yuque/mcp
```

Danach pruefen Sie den configured server ohne Verbindung:

```bash
iac-code mcp get yuque-remote --config-only
```

## Naechste Schritte

- Verwenden Sie [MCP-Konfiguration](./configuration.md) fuer scopes, project files, headers, OAuth options und advanced JSON forms.
- Verwenden Sie [OAuth und Sicherheit](./oauth-and-security.md) fuer token storage, reset und approval behavior.
- Verwenden Sie [Fehlerbehebung](./troubleshooting.md), wenn ein server pending approval, needs auth oder connection failed ist.
