---
sidebar_position: 2
title: Demarrage rapide MCP
description: Ajouter un serveur MCP HTTP distant ou un serveur stdio avec des commandes style Claude.
---

# Demarrage rapide MCP

Utilisez ce chemin quand vous avez deja une URL de serveur MCP ou une commande locale de serveur stdio.

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

## Serveur stdio

Pour les serveurs stdio, placez la commande du sous-processus après `--`. Un serveur HTTP distant n'a pas besoin de wrapper : ajoutez-le avec `--transport http` comme indiqué ci-dessus.

```bash
iac-code mcp add local-files --scope user -- npx -y @modelcontextprotocol/server-filesystem ./templates
```

Puis inspectez le serveur configure sans connexion :

```bash
iac-code mcp get local-files --config-only
```

## Etapes suivantes

- Consultez [Configuration MCP](./configuration.md) pour les scopes, fichiers projet, headers, options OAuth et formes JSON avancees.
- Consultez [OAuth et securite](./oauth-and-security.md) pour le stockage des tokens, reset et approval behavior.
- Consultez [Depannage](./troubleshooting.md) quand un serveur est pending approval, needs auth ou connection failed.
