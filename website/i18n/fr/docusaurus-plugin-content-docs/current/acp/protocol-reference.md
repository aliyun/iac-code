---
title: Référence du protocole
description: Référence complète des méthodes et événements du protocole ACP pour l'intégration iac-code.
sidebar_position: 3
---

# Référence du protocole

Ce document fournit une référence complète des méthodes ACP (Agent Client Protocol) et des événements de streaming exposés par le serveur iac-code.

## Aperçu du cycle de vie

Une session ACP typique suit ce flux :

```
initialize → new_session → prompt (loop) → close_session
                ↑                              │
                └── load_session / resume ──────┘
```

1. **initialize** -- Poignée de main. Négocier la version du protocole et découvrir les capacités du serveur.
2. **session/new** -- Créer une nouvelle session avec un runtime d'agent indépendant.
3. **session/prompt** -- Envoyer l'entrée utilisateur ; recevoir des événements en streaming jusqu'à une réponse finale.
4. **session/close** -- Libérer la session et ses ressources.

Les sessions peuvent également être chargées depuis l'historique (`session/load`) ou reprises (`session/resume`) au lieu d'en créer de nouvelles.

---

## Méthodes

### initialize

Poignée de main du protocole. Doit être le premier appel sur chaque connexion.

**Paramètres de requête**

| Champ | Type | Requis | Description |
|-------|------|----------|-------------|
| `protocolVersion` | integer | Oui | Version du protocole demandée (actuellement `1`) |
| `clientInfo` | object | Non | Nom et version du client |
| `clientCapabilities` | object | Non | Capacités prises en charge par le client |

**Champs de réponse**

| Champ | Type | Description |
|-------|------|-------------|
| `protocolVersion` | integer | Version du protocole négociée |
| `agentCapabilities` | object | Capacités du serveur (voir ci-dessous) |
| `agentInfo` | object | Nom et version du serveur |
| `authMethods` | array | Méthodes d'authentification disponibles (vide si utilisation des identifiants intégrés) |

**Capacités de l'agent**

| Capacité | Valeur | Signification |
|-----------|-------|---------|
| `loadSession` | `true` | Prend en charge la restauration de sessions depuis l'historique |
| `promptCapabilities.embeddedContext` | `true` | Accepte le contenu de ressources embarquées dans les requêtes |
| `promptCapabilities.image` | `false` | Entrée image non prise en charge (dégradée en marqueur texte) |
| `promptCapabilities.audio` | `false` | Entrée audio non prise en charge (dégradée en marqueur texte) |
| `sessionCapabilities.list` | `{}` | Prend en charge le listage des sessions |
| `sessionCapabilities.close` | `{}` | Prend en charge la fermeture des sessions |

---

### session/new

Créer une nouvelle session avec un runtime d'agent indépendant, un registre d'outils et un contexte LLM.

**Paramètres de requête**

| Champ | Type | Requis | Description |
|-------|------|----------|-------------|
| `cwd` | string | Oui | Chemin absolu vers le répertoire de travail |
| `mcpServers` | object | Non | Configuration du serveur MCP (acceptée mais pas encore fonctionnelle) |

**Champs de réponse**

| Champ | Type | Description |
|-------|------|-------------|
| `sessionId` | string | Identifiant de session unique pour les appels suivants |
| `modes` | object | Modes disponibles et mode actuel |
| `models` | object | Modèles disponibles et modèle actuel |

:::note
Chaque session crée un AgentLoop indépendant. Plusieurs sessions peuvent s'exécuter simultanément mais chacune consomme une connexion LLM.
:::

---

### session/load

Charger une session précédemment persistée depuis le disque, en restaurant son historique de messages.

**Paramètres de requête**

| Champ | Type | Requis | Description |
|-------|------|----------|-------------|
| `cwd` | string | Oui | Chemin du répertoire de travail |
| `sessionId` | string | Oui | Identifiant de la session à charger |

**Champs de réponse**

| Champ | Type | Description |
|-------|------|-------------|
| `models` | object | Modèles disponibles et état du modèle actuel |
| `modes` | object | Modes disponibles et état du mode actuel |

:::note
Le chargement d'une session lit l'historique depuis `~/.iac-code/sessions/`, répare automatiquement les messages interrompus et injecte l'historique dans un nouvel AgentLoop.
:::

---

### session/fork

Dupliquer une session existante pour créer une branche indépendante avec le même historique.

**Paramètres de requête**

| Champ | Type | Requis | Description |
|-------|------|----------|-------------|
| `cwd` | string | Oui | Chemin du répertoire de travail |
| `sessionId` | string | Oui | Identifiant de la session à dupliquer |

**Champs de réponse**

| Champ | Type | Description |
|-------|------|-------------|
| `sessionId` | string | Nouvel identifiant de session pour la branche dupliquée |
| `models` | object | Modèles disponibles et état du modèle actuel |
| `modes` | object | Modes disponibles et état du mode actuel |

---

### session/resume

Reprendre ou se reconnecter à une session existante. Charge automatiquement l'historique si nécessaire.

**Paramètres de requête**

| Champ | Type | Requis | Description |
|-------|------|----------|-------------|
| `cwd` | string | Oui | Chemin du répertoire de travail |
| `sessionId` | string | Oui | Identifiant de la session à reprendre |

**Champs de réponse**

| Champ | Type | Description |
|-------|------|-------------|
| `models` | object | Modèles disponibles et état du modèle actuel (optionnel) |
| `modes` | object | Modes disponibles et état du mode actuel (optionnel) |

:::note
Contrairement à `session/new`, la réponse n'inclut pas de champ `sessionId` puisque le client connaît déjà l'identifiant de session depuis la requête.
:::

---

### session/prompt

Envoyer l'entrée utilisateur et déclencher les réponses de l'agent en streaming.

**Paramètres de requête**

| Champ | Type | Requis | Description |
|-------|------|----------|-------------|
| `sessionId` | string | Oui | Identifiant de la session cible |
| `prompt` | array | Oui | Tableau de blocs de contenu (voir Types de blocs de contenu ci-dessous) |

**Types de blocs de contenu**

| Type | Description |
|------|-------------|
| `TextContentBlock` | Entrée texte brut de l'utilisateur |
| `EmbeddedResourceContentBlock` | Contenu de fichier embarqué en ligne |
| `ResourceContentBlock` | Référence de lien vers une ressource |
| `ImageContentBlock` | Image (dégradée en marqueur texte `[image: mime/type]`) |
| `AudioContentBlock` | Audio (dégradé en marqueur texte `[audio: mime/type]`) |

**Champs de réponse**

| Champ | Type | Description |
|-------|------|-------------|
| `stopReason` | string | Raison de la fin de la requête (voir Raisons d'arrêt) |
| `usage` | object | Utilisation des tokens : `inputTokens`, `outputTokens`, `totalTokens` |

**Raisons d'arrêt**

| Valeur | Signification |
|-------|---------|
| `end_turn` | Le modèle a terminé normalement |
| `max_turn_requests` | Limite maximale de la boucle d'appels d'outils atteinte |
| `max_tokens` | Limite de tokens en sortie atteinte |
| `cancelled` | Le client a annulé la requête |
| `refusal` | Le modèle a refusé de répondre |

:::note
Pendant l'exécution, le serveur envoie des notifications `session/update` avec des événements en streaming avant de retourner la réponse finale.
:::

---

### session/cancel

Annuler une tâche de requête en cours d'exécution.

**Paramètres de requête**

| Champ | Type | Requis | Description |
|-------|------|----------|-------------|
| `sessionId` | string | Oui | Session avec la requête en cours d'exécution |

**Comportement**

- Arrête la consommation des événements du flux
- Les outils en cours d'exécution ne sont pas terminés de force, mais les résultats sont ignorés
- L'appel `prompt` en attente retourne avec `stopReason: "cancelled"`

---

### session/close

Fermer une session et libérer ses ressources.

**Paramètres de requête**

| Champ | Type | Requis | Description |
|-------|------|----------|-------------|
| `sessionId` | string | Oui | Session à fermer |

**Comportement**

- La session est supprimée de la mémoire
- L'historique persisté reste sur le disque
- Les appels `prompt` suivants à cette session retournent une erreur

---

### sessions/list

Lister toutes les sessions persistées pour un répertoire de travail donné.

**Paramètres de requête**

| Champ | Type | Requis | Description |
|-------|------|----------|-------------|
| `cwd` | string | Oui | Répertoire de travail pour limiter la portée du listage |

**Champs de réponse**

| Champ | Type | Description |
|-------|------|-------------|
| `sessions` | array | Liste d'objets session avec `sessionId` et métadonnées |

---

### config/set

Définir dynamiquement une option de configuration pour une session.

**Paramètres de requête**

| Champ | Type | Requis | Description |
|-------|------|----------|-------------|
| `sessionId` | string | Oui | Session cible |
| `configId` | string | Oui | Clé de configuration à définir |
| `value` | any | Oui | Nouvelle valeur |

---

## Événements de streaming

Pendant l'exécution de `session/prompt`, le serveur envoie des notifications `session/update` contenant des données d'événements en streaming.

### Format des événements

Chaque notification `session/update` contient un objet de mise à jour avec un type spécifique :

```json
{
  "jsonrpc": "2.0",
  "method": "session/update",
  "params": {
    "sessionId": "abc123",
    "update": { "type": "agent_message_chunk", "text": "..." }
  }
}
```

### Correspondance des types d'événements

| Événement interne | Type de mise à jour ACP | Description |
|---------------|----------------|-------------|
| `TextDeltaEvent` | `AgentMessageChunk` | Sortie texte incrémentale de l'agent |
| `ThinkingDeltaEvent` | `AgentThoughtChunk` | Contenu de raisonnement/réflexion du modèle |
| `ToolUseStartEvent` | `ToolCallStart` | Début de l'invocation d'un outil |
| `ToolResultEvent` | `ToolCallProgress` | Résultat de l'outil (terminé ou échoué) |
| `CompactionEvent` | `AgentMessageChunk` | Notification de compaction du contexte |
| `ErrorEvent` | `AgentMessageChunk` | Informations d'erreur |

### Cycle de vie des appels d'outils

```
ToolCallStart (status=in_progress)
    │
    ├── ToolCallProgress (status=in_progress, raw_input=tool input)
    │
    ├── ToolCallProgress (status=completed, raw_output=result)   ← success
    │
    └── ToolCallProgress (status=failed, raw_output=error)       ← failure
```

**Correspondance des types d'outils**

| Outil | ToolKind ACP |
|------|-------------|
| `read_file`, `list_files` | `read` |
| `glob`, `grep` | `search` |
| `write_file`, `edit_file` | `edit` |
| `bash`, `agent` | `execute` |
| `web_fetch` | `fetch` |
| Autres | `other` |

---

## Demandes de permission

Avant d'exécuter des outils à haut risque, iac-code envoie un callback `request_permission` au client.

### Catégories de permissions des outils

| Catégorie | Outils | Auto-approuvé |
|----------|-------|-------------|
| Lecture seule | `read_file`, `list_files`, `glob`, `grep`, `web_fetch` | Oui |
| Écriture | `write_file`, `edit_file` | Non -- nécessite une approbation |
| Exécution | `bash`, `agent` | Non -- nécessite une approbation |

### Événement request_permission

Le serveur envoie un callback `request_permission` avec :

| Champ | Type | Description |
|-------|------|-------------|
| `options` | array | Choix de permission disponibles |
| `sessionId` | string | Session demandant la permission |
| `toolCall` | object | Détails de l'appel d'outil (titre, type, entrée) |

### Options de permission

| Identifiant d'option | Signification |
|-----------|---------|
| `allow_once` | Autoriser cette invocation spécifique |
| `allow_always` | Autoriser tous les appels futurs de cet outil dans cette session lorsque l'outil prend en charge l'autorisation globale ; non proposé par défaut pour `bash` |
| `allow_rule:<rules>` | Autoriser les futurs appels correspondant aux règles suggérées dans cette session |
| `deny_rule:<rules>` | Refuser les futurs appels correspondant aux règles suggérées dans cette session |
| `reject_once` | Refuser cette invocation spécifique |
| `reject_always` | Refuser tous les appels futurs de cet outil dans cette session |

### Format de réponse

```json
{
  "outcome": "allowed",
  "option_id": "allow_once"
}
```

Ou pour refuser :

```json
{
  "outcome": "denied"
}
```

| Réponse du client | Comportement de l'outil |
|----------------|---------------|
| `AllowedOutcome` | L'outil s'exécute normalement |
| `DeniedOutcome` | L'outil est ignoré ; le modèle reçoit une erreur "Permission denied." |

---

## Gestion des erreurs

### Format RequestError

Les erreurs suivent le format d'erreur JSON-RPC 2.0 :

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "error": {
    "code": -32602,
    "message": "Invalid params",
    "data": {"session_id": "Session not found"}
  }
}
```

### Codes d'erreur courants

| Code | Nom | Description |
|------|------|-------------|
| `-32700` | Erreur d'analyse | JSON invalide |
| `-32600` | Requête invalide | JSON-RPC malformé |
| `-32601` | Méthode introuvable | Méthode inconnue |
| `-32602` | Paramètres invalides | Paramètres manquants ou invalides (ex. : identifiant de session inconnu) |
| `-32603` | Erreur interne | Défaillance côté serveur |
