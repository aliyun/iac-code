---
sidebar_position: 1
title: Intégration MCP
description: Utiliser des serveurs Model Context Protocol pour étendre IaC Code avec des outils, ressources, prompts et compétences externes.
---

# Intégration MCP

IaC Code peut agir comme host Model Context Protocol (MCP). Les MCP servers etendent l'agent avec des tools, resources, prompts et reusable skills externes tout en passant par les chemins permission, session, logging et output handling de IaC Code.

Utilisez MCP lorsque vous souhaitez qu'IaC Code appelle une fonctionnalité locale ou distante qui n'est pas intégrée au produit, telle qu'un catalogue de modèles privé, un réviseur de déploiement interne, un service de requête d'inventaire ou un outil d'exploitation cloud spécialisé.

## Supported Surfaces

| Surface | MCP support |
|---|---|
| REPL interactif | Charge les serveurs de projet utilisateur, locaux et approuvés. Invite avant de faire confiance aux serveurs du nouveau projet `.mcp.json`. |
| Mode non interactif | Charge les serveurs de projet utilisateur, locaux et approuvés. Ne demande jamais ; les serveurs de projet en attente sont ignorés avec des avertissements. |
| Serveur ACP | Accepte les configurations de serveur MCP de session des clients ACP et expose les fonctionnalités MCP découvertes au sein de cette session. |
| Serveur A2A | Charge MCP via l'exécution normale et peut publier les avertissements MCP et la progression de l'outil dans les métadonnées de la tâche A2A. |
| Mode pipeline | Utilise les mêmes intégrations d'exécution que le mode normal, y compris la progression de l'outil MCP et la propagation des avertissements. |

## Supported Capabilities

| Capability | Status |
|---|---|
| transport `stdio` | Pris en charge pour les processus du serveur MCP local. |
| Transport HTTP diffusable | Pris en charge pour les serveurs MCP distants. |
| Transports ESS | Pris en charge pour les serveurs MCP distants. |
| Outils MCP | Exposé en tant qu'outils d'agent nommés `mcp__<server>__<tool>`. |
| Ressources MCP | Exposé via `list_mcp_resources` et `read_mcp_resource`. |
| Invites MCP | Exposé sous forme de commandes slash nommées `mcp__<server>__<prompt>`. |
| Ressources MCP `skill://` | Exposé sous forme de commandes de compétences nommées `mcp__<server>__<skill>`. |
| Authentification de bouclage OAuth | Pris en charge pour les serveurs distants avec des métadonnées OAuth. |
| `roots/list` | Soutenu. IaC Code renvoie la racine de l'espace de travail actif sous forme d'URI de fichier. |
| notifications `list_changed` | Pris en charge pour les outils, les ressources et les invites. Les inscriptions s’actualisent dynamiquement. |
| MCP elicitation | Pris en charge dans les sessions interactives. Les exécutions non interactives annulent sans risque. URL elicitation peut réessayer le tool call original après confirmation utilisateur. |
| WebSocket transport | Pris en charge pour les servers `ws://` et `wss://` avec URL seule. WebSocket rejette headers, `headersHelper` et OAuth car le SDK transport installé accepte seulement une URL. |
| Commandes dynamiques `headersHelper` | Prises en charge pour les servers `http` et `sse` de confiance. Les helpers tournent sans shell, avec timeout borné, environnement minimal et diagnostics expurgés. |
| Transports SDK et IDE | Non pris en charge. |
| Code IaC en tant que serveur MCP | Non pris en charge. IaC Code agit actuellement uniquement en tant qu'hôte MCP. |

## How It Works

At runtime IaC Code:

1. Charge la configuration MCP à partir des sources utilisateur, projet, locales et session.
2. Développe les références `${VAR}` et `${VAR:-default}`.
3. Ignore les serveurs dangereux ou invalides avec des avertissements visibles par l'utilisateur.
4. Connecte les serveurs approuvés avec une concurrence limitée.
5. Découvrez les outils, les ressources, les invites et les ressources `skill://`.
6. Enregistre ces capacités dans les registres d'outils et de commandes existants.
7. Injecte les instructions du serveur connecté dans l'invite de l'agent en tant que guide à l'échelle du serveur.
8. Convertit les résultats de l'outil MCP en résultats normaux de l'outil IaC Code, en stockant les artefacts binaires et les artefacts de texte volumineux dans le répertoire de configuration d'exécution.
9. Déconnecte les clients MCP à la fermeture du REPL, de l'exécution sans tête, de la session ACP ou du runtime A2A.

Un serveur MCP défaillant ne bloque pas les autres serveurs configurés. Les échecs de connexion et de découverte restent visibles sous forme d’avertissements MCP.

## Naming

Les outils et commandes MCP sont normalisés en noms publics :

```text
mcp__<server>__<tool>
mcp__<server>__<prompt>
mcp__<server>__<skill>
```

Les caractères en dehors des lettres, des chiffres et des traits de soulignement deviennent des traits de soulignement. Si deux fonctionnalités découvertes entrent en collision après la normalisation, IaC Code ajoute un court résumé pour conserver les noms uniques.

Pour les compétences MCP, IaC Code enregistre également un alias de compatibilité tel que `<server>:<skill>` lorsque cet alias n'est pas en conflit avec une commande existante. Les diagnostics conservent les noms d'origine du serveur, de l'outil, de l'invite ou de la compétence même lorsque les noms publics sont normalisés.

## Related Pages

- [Demarrage rapide MCP](./quick-start.md)
- [Configuration MCP](./configuration.md)
- [Outils, ressources, invites et compétences](./capabilities.md)
- [OAuth et sécurité](./oauth-and-security.md)
- [Dépannage](./troubleshooting.md)
