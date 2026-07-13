---
sidebar_position: 2
title: Demarrage rapide MCP
description: Ajouter un serveur MCP HTTP distant ou un wrapper stdio avec des commandes style Claude.
---

# Demarrage rapide MCP

Utilisez ce chemin quand vous avez deja une URL de serveur MCP ou une commande locale de wrapper stdio.

## Serveur HTTP distant

Ajoutez le serveur distant avec la forme URL positionnelle :

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
```

Si le serveur utilise OAuth, lancez le flux d'autorisation :

```bash
iac-code mcp auth yuque
```

Apres autorisation, lancez un health check borne :

```bash
iac-code mcp get yuque --check
```

## Wrapper Stdio

Pour les wrappers comme `mcp-remote`, placez la commande subprocess apres `--` :

```bash
iac-code mcp add yuque-remote --scope user -- npx mcp-remote https://mcp.example.com/yuque/mcp
```

Puis inspectez le serveur configure sans connexion :

```bash
iac-code mcp get yuque-remote --config-only
```

## Etapes suivantes

- Consultez [Configuration MCP](./configuration.md) pour les scopes, fichiers projet, headers, options OAuth et formes JSON avancees.
- Consultez [OAuth et securite](./oauth-and-security.md) pour le stockage des tokens, reset et approval behavior.
- Consultez [Depannage](./troubleshooting.md) quand un serveur est pending approval, needs auth ou connection failed.
