---
sidebar_position: 1
title: Intégration MCP
description: Utilisez des serveurs Model Context Protocol pour étendre IaC Code avec des outils, ressources, prompts et compétences externes.
---

# Intégration MCP

IaC Code peut agir comme hôte Model Context Protocol (MCP). Les serveurs MCP étendent l'agent avec des outils externes, des ressources, des prompts et des compétences réutilisables, tout en passant par les mécanismes d'autorisations, de session, de journalisation et de traitement de sortie de IaC Code.

Utilisez MCP lorsque vous voulez que IaC Code appelle une capacité locale ou distante qui n'est pas intégrée au produit, par exemple un catalogue privé de modèles, un outil interne de revue de déploiement, un service d'inventaire ou un outil spécialisé d'opérations cloud.

## Surfaces prises en charge

| Surface | Prise en charge MCP |
|---|---|
| REPL interactif | Charge les serveurs utilisateur, locaux et de projet approuvés. Demande confirmation avant de faire confiance à de nouveaux serveurs de projet `.mcp.json`. |
| Mode non interactif | Charge les serveurs utilisateur, locaux et de projet approuvés. Ne demande jamais de confirmation ; les serveurs de projet en attente sont ignorés avec des avertissements. |
| Serveur ACP | Accepte les configurations MCP transmises par les clients ACP lors de la session et expose les capacités MCP découvertes dans cette session. |
| Serveur A2A | Charge MCP via le runtime normal et peut publier les avertissements MCP et la progression des outils dans les métadonnées de tâche A2A. |
| Mode pipeline | Utilise les mêmes intégrations runtime que le mode normal, avec propagation de la progression des outils MCP et des avertissements. |

## Capacités prises en charge

| Capacité | État |
|---|---|
| Transport `stdio` | Pris en charge pour les processus MCP locaux. |
| Transport Streamable HTTP | Pris en charge pour les serveurs MCP distants. |
| Transport SSE | Pris en charge pour les serveurs MCP distants. |
| Outils MCP | Exposés comme outils d'agent nommés `mcp__<server>__<tool>`. |
| Ressources MCP | Exposées via `list_mcp_resources` et `read_mcp_resource`. |
| Prompts MCP | Exposés comme commandes slash nommées `mcp__<server>__<prompt>`. |
| Ressources MCP `skill://` | Exposées comme commandes de compétence nommées `mcp__<server>__<skill>`. |
| Authentification OAuth loopback | Prise en charge pour les serveurs distants avec métadonnées OAuth. |
| `roots/list` | Pris en charge. IaC Code renvoie la racine du workspace active sous forme d'URI file. |
| Notifications `list_changed` | Prises en charge pour les outils, ressources et prompts. Les enregistrements sont rafraîchis dynamiquement. |
| Elicitation MCP | Pas encore prise en charge. Les serveurs qui demandent l'elicitation reçoivent une erreur explicite. |
| Transports WebSocket, SDK, IDE | Non pris en charge. |
| Commandes dynamiques `headersHelper` | Non prises en charge. Utilisez des headers statiques ou des références à des variables d'environnement. |
| IaC Code comme serveur MCP | Non pris en charge. IaC Code agit actuellement uniquement comme hôte MCP. |

## Fonctionnement

À l'exécution, IaC Code :

1. Charge la configuration MCP depuis les sources utilisateur, locales, projet et session.
2. Développe les références `${VAR}` et `${VAR:-default}`.
3. Ignore les serveurs non sûrs ou invalides avec des avertissements visibles par l'utilisateur.
4. Se connecte aux serveurs approuvés avec une concurrence bornée.
5. Découvre les outils, ressources, prompts et ressources `skill://`.
6. Enregistre ces capacités dans les registres d'outils et de commandes existants.
7. Convertit les résultats d'outils MCP en résultats d'outils IaC Code normaux et stocke les artefacts binaires dans le répertoire de configuration runtime.
8. Déconnecte les clients MCP quand le REPL, l'exécution headless, la session ACP ou le runtime A2A se ferme.

L'échec d'un serveur MCP ne bloque pas les autres serveurs configurés. Les échecs de connexion et de découverte restent visibles comme avertissements MCP.

## Nommage

Les outils et commandes MCP sont normalisés en noms publics :

```text
mcp__<server>__<tool>
mcp__<server>__<prompt>
mcp__<server>__<skill>
```

Les caractères autres que lettres, chiffres et underscores deviennent des underscores. Si plusieurs capacités entrent en collision après normalisation, IaC Code ajoute un court digest pour garder des noms uniques.

## Pages associées

- [Configuration MCP](./configuration.md)
- [Outils, ressources, prompts et compétences](./capabilities.md)
- [OAuth et sécurité](./oauth-and-security.md)
- [Dépannage](./troubleshooting.md)
