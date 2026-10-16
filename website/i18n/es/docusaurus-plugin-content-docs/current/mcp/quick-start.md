---
sidebar_position: 2
title: Inicio rapido MCP
description: Agrega un servidor MCP HTTP remoto o servidor stdio con comandos estilo Claude.
---

# Inicio rapido MCP

Usa esta ruta cuando ya tengas una URL de servidor MCP o un comando local de servidor stdio.

## Servidor HTTP remoto

Agrega el servidor remoto con la forma de URL posicional:

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
```

Si el servidor usa OAuth, inicia el flujo de autorizacion:

```bash
iac-code mcp auth yuque
```

Despues de autorizar, ejecuta un health check acotado:

```bash
iac-code mcp get yuque --check
```

## Servidor stdio

Para servidores stdio, coloca el comando del subproceso después de `--`. Un servidor HTTP remoto no necesita un wrapper: añádelo con `--transport http` como se muestra arriba.

```bash
iac-code mcp add local-files --scope user -- npx -y @modelcontextprotocol/server-filesystem ./templates
```

Luego inspecciona el servidor configurado sin conectarte:

```bash
iac-code mcp get local-files --config-only
```

## Siguientes pasos

- Usa [Configuración MCP](./configuration.md) para scopes, project files, headers, OAuth options y advanced JSON forms.
- Usa [OAuth y seguridad](./oauth-and-security.md) para token storage, reset y approval behavior.
- Usa [Solución de problemas](./troubleshooting.md) cuando un servidor este pending approval, needs auth o connection failed.
