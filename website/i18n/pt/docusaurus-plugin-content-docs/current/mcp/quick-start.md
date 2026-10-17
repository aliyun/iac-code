---
sidebar_position: 2
title: Inicio rapido MCP
description: Adicione um servidor MCP HTTP remoto ou servidor stdio com comandos estilo Claude.
---

# Inicio rapido MCP

Use este caminho quando voce ja tiver uma URL de servidor MCP ou um comando local de servidor stdio.

## Servidor HTTP remoto

Adicione o servidor remoto com a forma de URL posicional:

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
```

Se o servidor usa OAuth, inicie o fluxo de autorizacao:

```bash
iac-code mcp auth yuque
```

Depois da autorizacao, execute um health check limitado:

```bash
iac-code mcp get yuque --check
```

## Servidor stdio

Para servidores stdio, coloque o comando do subprocesso depois de `--`. Um servidor HTTP remoto não precisa de wrapper: adicione-o com `--transport http` como mostrado acima.

```bash
iac-code mcp add local-files --scope user -- npx -y @modelcontextprotocol/server-filesystem ./templates
```

Depois inspecione o servidor configurado sem conectar:

```bash
iac-code mcp get local-files --config-only
```

## Proximos passos

- Use [Configuração MCP](./configuration.md) para scopes, project files, headers, OAuth options e advanced JSON forms.
- Use [OAuth e segurança](./oauth-and-security.md) para token storage, reset e approval behavior.
- Use [Solução de problemas](./troubleshooting.md) quando um servidor estiver pending approval, needs auth ou connection failed.
