---
title: Référence des commandes
description: Référence complète des commandes CLI pour exécuter et appeler iac-code via A2A.
sidebar_position: 3
---

# Référence des commandes A2A

Cette page documente chaque commande `iac-code` liée à A2A. Utilisez-la lorsque vous avez besoin des noms exacts des options, des motifs de commandes courants et du sens opérationnel de chaque indicateur.

## Vue d'ensemble des commandes

| Commande | Objectif |
|---------|---------|
| `iac-code a2a` | Exécuter iac-code comme serveur A2A |
| `iac-code a2a-client call` | Découvrir une Agent Card distante et envoyer un prompt |
| `iac-code a2a-client discover` | Récupérer et vérifier optionnellement une Agent Card |
| `iac-code a2a-client task-get` | Récupérer une tâche par ID |
| `iac-code a2a-client task-list` | Lister les tâches avec filtres et pagination |
| `iac-code a2a-client task-cancel` | Annuler une tâche active |
| `iac-code a2a-client task-subscribe` | S'abonner au flux d'événements d'une tâche active |
| `iac-code a2a-client push-config-create` | Créer une configuration de notification push de tâche |
| `iac-code a2a-client push-config-get` | Récupérer une configuration de notification push de tâche |
| `iac-code a2a-client push-config-list` | Lister les configurations de notification push de tâche |
| `iac-code a2a-client push-config-delete` | Supprimer une configuration de notification push de tâche |
| `iac-code a2a-client extended-card` | Récupérer l'Agent Card étendue authentifiée |
| `iac-code a2a-route-preview` | Prévisualiser la sélection de route locale pour `a2a-client call` |

Toutes les commandes client HTTP acceptent les mêmes options d'authentification :

| Option | Description |
|--------|-------------|
| `--token` | Jeton Bearer envoyé comme `Authorization: Bearer <token>` |
| `--basic-username` | Nom d'utilisateur Basic auth |
| `--basic-password` | Mot de passe Basic auth |
| `--api-key` | Valeur de clé API |
| `--api-key-header` | Nom de l'en-tête de clé API ; vaut `X-API-Key` par défaut |

## Configuration client A2A

Toutes les sous-commandes `a2a-client` acceptent un fichier de configuration YAML au niveau du groupe :

```bash
iac-code a2a-client --config a2a-client.yml call --prompt "Create a VPC"
```

Les options CLI remplacent les valeurs de configuration. Utilisez la configuration pour les paramètres stables de connexion, d'authentification, de vérification, de routage et les paramètres répétés de tâche ou de push ; gardez le texte de prompt ponctuel sur la ligne de commande.

```yaml
url: http://127.0.0.1:41242/
token: your-bearer-token
basic-username: iac-code
basic-password: your-password
api-key: your-api-key
api-key-header: X-IAC-Code-Key
verify-card-secret: your-card-signing-secret
verify-card-jwks-url: https://a2a.example.com/.well-known/jwks.json
require-card-signature: true
timeout: 30
cwd: /path/to/workspace
context-id: ctx-123
task-id: task-123
config-id: webhook-1
callback-url: https://hooks.example.com/a2a
notification-token: notification-token
auth-scheme: bearer
auth-credentials: callback-token
routes:
  - name: ros
    url: http://127.0.0.1:41242/
    skills:
      - iac_generation
    tags:
      - ros
      - template
```

## `iac-code a2a`

Exécute iac-code comme serveur A2A.

```bash
iac-code a2a
```

Par défaut, le serveur se lie à `127.0.0.1:41242` et sert JSON-RPC via HTTP. Le port `41242` est la valeur par défaut d'iac-code ; ce n'est pas un port A2A enregistré.

### Options serveur de base

| Option | Défaut | Description |
|--------|---------|-------------|
| `--config` | vide | Fichier de configuration YAML contenant les options du serveur A2A |
| `--host` | `127.0.0.1` | Hôte du serveur HTTP |
| `--port` | `41242` | Port du serveur HTTP |
| `--transport` | `http` | Transport serveur : `http`, `stdio`, `unix`, `websocket`, `grpc`, `grpc-jsonrpc` ou `redis-streams` |
| `--thinking-exposure` | `tool-trace` | Expose un type de signal de thinking A2A ; répétez pour en fournir plusieurs. Valeurs : `raw-thinking`, `tool-trace` |
| `--debug`, `-d` | `false` | Activer la journalisation de débogage |

Exemple :

```bash
iac-code a2a --host 127.0.0.1 --port 41242 --debug
```

### Configuration YAML

Utilisez `--config` pour l'authentification, le stockage, la signature, les paramètres propres aux transports, la livraison push et d'autres détails de déploiement. Les clés peuvent utiliser des tirets ou des underscores. Les indicateurs CLI communs `--host`, `--port` et `--transport` remplacent les valeurs du fichier de configuration.

```yaml
host: 127.0.0.1
port: 41242
transport: http
token: local-dev-token
persistence-dir: .iac-code-a2a/state
artifact-dir: .iac-code-a2a/artifacts
push-notifications: true
```

Exécutez-le avec :

```bash
iac-code a2a --config a2a-server.yml --port 41243
```

### Authentification HTTP

L'authentification est optionnelle. Configurez l'authentification du serveur en YAML ou avec des variables d'environnement. Si aucun paramètre d'authentification n'est configuré, les requêtes ne sont pas authentifiées. Lorsqu'un ou plusieurs schémas sont configurés, une requête peut satisfaire n'importe lequel des schémas configurés.

| Clé de configuration | Variable d'environnement | Description |
|--------|----------------------|-------------|
| `token` | `IACCODE_A2A_HTTP_TOKEN` | Jeton Bearer |
| `basic-username` | `IACCODE_A2A_BASIC_USERNAME` | Nom d'utilisateur Basic auth |
| `basic-password` | `IACCODE_A2A_BASIC_PASSWORD` | Mot de passe Basic auth |
| `api-key` | `IACCODE_A2A_API_KEY` | Valeur de clé API |
| `api-key-header` | `IACCODE_A2A_API_KEY_HEADER` | Nom de l'en-tête de clé API |

Jeton Bearer :

```yaml
token: local-dev-token
```

Basic auth :

```yaml
basic-username: iac-code
basic-password: local-dev-password
```

Clé API :

```yaml
api-key: local-dev-key
api-key-header: X-IAC-Code-Key
```

### Persistance et artefacts

| Clé de configuration | Défaut | Description |
|--------|---------|-------------|
| `persistence-dir` | `~/.iac-code/a2a` | Métadonnées JSON locales pour les tâches, contextes, routes et configurations push |
| `artifact-dir` | `<persistence-dir>/artifacts` | Magasin local de charges utiles d'artefacts |

La persistance duplique les instantanés de tâches et de contextes pour les métadonnées de restauration. Elle ne redémarre pas une tâche asyncio en cours après un crash de processus.

```yaml
persistence-dir: ~/.iac-code/a2a
artifact-dir: ~/.iac-code/a2a/artifacts
```

### Signature d'Agent Card

| Clé de configuration | Description |
|--------|-------------|
| `signing-secret` | Secret HMAC utilisé pour signer l'Agent Card publique |

Le serveur émet les champs JWS `AgentCardSignature` du SDK A2A. Le mode symétrique utilise `HS256`.

```yaml
signing-secret: local-card-signing-secret
```

### Livraison des notifications push

| Clé de configuration | Défaut | Description |
|--------|---------|-------------|
| `push-notifications` | `false` | Activer les méthodes de configuration des notifications push de tâche A2A et la livraison des états terminaux |
| `push-queue` | `local-file` | Backend de file push : `local-file` ou `redis-streams` |
| `push-redis-url` | vide | URL Redis pour la file push adossée à Redis |
| `push-stream` | `iac-code:a2a:push` | Stream Redis pour les tâches push |
| `push-retry-key` | `iac-code:a2a:push:retry` | Ensemble trié Redis pour les nouvelles tentatives différées |
| `push-dead-stream` | `iac-code:a2a:push:dead` | Stream Redis pour les tâches en dead-letter |
| `push-consumer-group` | `iac-code-push` | Groupe de consommateurs Redis pour les workers push |
| `push-consumer-name` | vide | Nom de consommateur Redis pour ce worker |
| `push-lease-timeout-ms` | `300000` | Délai de bail pending Redis |

File locale :

```yaml
push-notifications: true
persistence-dir: ~/.iac-code/a2a
push-queue: local-file
```

File Redis Streams :

```yaml
push-notifications: true
push-queue: redis-streams
push-redis-url: redis://localhost:6379/0
push-stream: iac-code:a2a:push
push-retry-key: iac-code:a2a:push:retry
push-dead-stream: iac-code:a2a:push:dead
push-consumer-group: iac-code-push
push-consumer-name: worker-1
```

La livraison push adossée à Redis nécessite l'extra `a2a-redis`.

### Options de transport

| Transport | Commande | Notes |
|-----------|---------|-------|
| HTTP JSON-RPC et REST | `iac-code a2a --transport http` | Par défaut. Annonce les interfaces `JSONRPC` et `HTTP+JSON`. |
| stdio | `iac-code a2a --transport stdio` | Trames JSON-RPC personnalisées expérimentales via entrée/sortie standard. |
| Socket Unix | `iac-code a2a --config a2a-server.yml --transport unix` | Nécessite `socket-path` dans la configuration. |
| WebSocket | `iac-code a2a --config a2a-server.yml --transport websocket` | Utilise `ws-path` depuis la configuration, avec `/a2a` par défaut. |
| gRPC | `iac-code a2a --config a2a-server.yml --transport grpc` | Utilise `grpc-host` et `grpc-port` depuis la configuration. |
| gRPC JSON-RPC | `iac-code a2a --config a2a-server.yml --transport grpc-jsonrpc` | Enveloppe JSON-RPC personnalisée via gRPC. |
| Redis Streams | `iac-code a2a --config a2a-server.yml --transport redis-streams` | Nécessite `redis-url` dans la configuration. |

Options du transport Redis Streams :

| Clé de configuration | Défaut | Description |
|--------|---------|-------------|
| `redis-url` | vide | URL de connexion Redis ; requise pour `--transport redis-streams` |
| `request-stream` | `iac-code:a2a:requests` | Nom du stream de requêtes |
| `response-stream` | `iac-code:a2a:responses` | Nom du stream de réponses |
| `consumer-group` | `iac-code` | Groupe de consommateurs du stream de requêtes |

### Exposition du thinking

| Clé de configuration | Défaut | Description |
|--------|---------|-------------|
| `thinking-exposure` | `tool-trace` | Types de signaux d'exécution hors réponse exposés via A2A `metadata.iac_code`. Utilisez une liste YAML, une chaîne séparée par des virgules ou des flags `--thinking-exposure` répétés. Les valeurs prises en charge sont `tool-trace` et `raw-thinking`. |

`tool-trace` préserve les métadonnées existantes de progression d'outil, d'autorisation et de résultat. `raw-thinking` émet les chunks de raisonnement du provider sous forme de mises à jour `metadata.iac_code.thinking` avec `type: raw_thinking` et `text`. iac-code ne produit actuellement pas d'événements séparés thought-summary ou progress-summary ; ces valeurs ne sont donc pas des types d'exposition valides.

```yaml
thinking-exposure:
  - tool-trace
  - raw-thinking
```

### Comportement des autorisations

| Clé de configuration | Défaut | Description |
|--------|---------|-------------|
| `auto-approve-permissions` | `false` | Approuver automatiquement les demandes d'autorisation d'outil levées pendant les tours A2A |

Sans `auto-approve-permissions: true`, le mode A2A rejette les prompts d'autorisation et émet des métadonnées d'autorisation. Utilisez-le seulement pour les environnements d'automatisation de confiance.

## `iac-code a2a-client call`

Découvre une Agent Card, choisit l'endpoint annoncé et envoie un prompt.

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Create a ROS VPC template with two vSwitches." \
  --cwd "$PWD"
```

| Option | Défaut | Description |
|--------|---------|-------------|
| `--url` | vide | URL de base de l'agent A2A ou URL de l'endpoint JSON-RPC ; peut venir de la configuration |
| `--route` | répétable | Spécification de route utilisée lorsque `--url` est omis |
| `--route-name` | vide | Route nommée à sélectionner |
| `--prompt`, `-p` | obligatoire | Texte du prompt |
| `--cwd` | `.` | Chemin d'espace de travail envoyé comme `message.metadata.iac_code.cwd` |
| `--context-id` | vide | ID de contexte A2A existant pour un message de suivi |
| `--verify-card-secret`, `--signing-secret` | vide | Secret HMAC pour la vérification de l'Agent Card |
| `--verify-card-jwks-url` | vide | URL JWKS distante utilisée pour la vérification de l'Agent Card |
| `--require-card-signature`, `--require-signature` | `false` | Rejeter les Agent Cards non signées ou invalides |
| `--timeout` | `30.0` | Délai d'appel en secondes |
| `--stream` | `false` | Utiliser `SendStreamingMessage` et afficher les événements du flux |

Suivi dans le même contexte :

```bash
iac-code a2a-client --config a2a-client.yml call \
  --context-id ctx-123 \
  --prompt "Now add outputs for the VPC and vSwitch IDs." \
  --cwd "$PWD"
```

Streaming :

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Review this Terraform module." \
  --cwd "$PWD" \
  --stream
```

Exiger une Agent Card signée :

```bash
iac-code a2a-client --config a2a-client.yml call \
  --prompt "Generate a production VPC template." \
  --cwd "$PWD"
```

Vérifier avec une URL JWKS distante :

```bash
iac-code a2a-client --config jwks-client.yml call \
  --prompt "Review the ROS stack."
```

## `iac-code a2a-client discover`

Récupère et affiche une Agent Card distante.

```bash
iac-code a2a-client --config a2a-client.yml discover
```

| Option | Description |
|--------|-------------|
| `--url` | URL de base de l'agent A2A ; peut venir de la configuration |
| `--verify-card-secret`, `--signing-secret` | Secret HMAC pour la vérification |
| `--verify-card-jwks-url` | URL JWKS distante pour la vérification |
| `--require-card-signature`, `--require-signature` | Exiger une signature valide |

Découverte authentifiée :

```bash
iac-code a2a-client --config a2a-client.yml discover
```

## Commandes de tâche

Les commandes de tâche appellent directement les méthodes de tâche JSON-RPC. Elles sont utiles pour les outils opérationnels, les tableaux de bord et le débogage.

### `iac-code a2a-client task-get`

```bash
iac-code a2a-client --config a2a-client.yml task-get \
  --task-id task-123 \
  --history-length 20
```

| Option | Description |
|--------|-------------|
| `--url` | URL de l'endpoint A2A JSON-RPC ; peut venir de la configuration |
| `--task-id` | ID de tâche ; peut venir de la configuration |
| `--history-length` | Nombre maximal d'entrées d'historique de tâche à renvoyer |

### `iac-code a2a-client task-list`

```bash
iac-code a2a-client --config a2a-client.yml task-list \
  --context-id ctx-123 \
  --status TASK_STATE_INPUT_REQUIRED \
  --page-size 20 \
  --output table
```

| Option | Défaut | Description |
|--------|---------|-------------|
| `--url` | vide | URL de l'endpoint A2A JSON-RPC ; peut venir de la configuration |
| `--context-id` | vide | Filtrer par ID de contexte |
| `--status` | vide | Filtrer par état de tâche |
| `--page-size` | vide | Nombre maximal de tâches à renvoyer |
| `--page-token` | vide | Jeton de pagination |
| `--include-artifacts` | `false` | Inclure les artefacts de tâche dans la réponse |
| `--output` | `table` | `table` ou `json` |

Sortie JSON :

```bash
iac-code a2a-client --config a2a-client.yml task-list \
  --include-artifacts \
  --output json
```

### `iac-code a2a-client task-cancel`

```bash
iac-code a2a-client --config a2a-client.yml task-cancel \
  --task-id task-123
```

L'annulation est coopérative. Une tâche terminée, échouée, annulée ou nécessitant une entrée renvoie l'erreur A2A standard de tâche non annulable.

### `iac-code a2a-client task-subscribe`

```bash
iac-code a2a-client --config a2a-client.yml task-subscribe \
  --task-id task-123
```

La commande diffuse les événements des tâches actives. Pour un nouveau tour, préférez `a2a-client call --stream` ; il démarre la tâche et diffuse les mises à jour en une seule commande.

## Commandes de configuration des notifications push

Ces commandes nécessitent un serveur démarré avec `push-notifications: true`. Elles gèrent les configurations standard de notifications push de tâche A2A.

### `iac-code a2a-client push-config-create`

```bash
iac-code a2a-client --config a2a-client.yml push-config-create \
  --task-id task-123 \
  --config-id webhook-1 \
  --callback-url https://hooks.example.com/a2a \
  --notification-token "$NOTIFICATION_TOKEN" \
  --auth-scheme bearer \
  --auth-credentials "$WEBHOOK_BEARER_TOKEN"
```

| Option | Description |
|--------|-------------|
| `--url` | URL de l'endpoint A2A JSON-RPC ; peut venir de la configuration |
| `--task-id` | ID de tâche ; peut venir de la configuration |
| `--config-id` | ID de configuration push ; peut venir de la configuration |
| `--callback-url` | URL de callback HTTP(S) ; peut venir de la configuration |
| `--notification-token` | Jeton envoyé comme `X-A2A-Notification-Token` |
| `--auth-scheme` | Schéma d'authentification du callback, comme `bearer` ou `basic` |
| `--auth-credentials` | Identifiants d'authentification du callback |

Les URL de callback sont validées avant le stockage et l'envoi. Le validateur par défaut rejette les URL non HTTP(S), les noms localhost et les adresses IP littérales privées/locales.

### `iac-code a2a-client push-config-get`

```bash
iac-code a2a-client --config a2a-client.yml push-config-get \
  --task-id task-123 \
  --config-id webhook-1
```

### `iac-code a2a-client push-config-list`

```bash
iac-code a2a-client --config a2a-client.yml push-config-list \
  --task-id task-123 \
  --page-size 10
```

### `iac-code a2a-client push-config-delete`

```bash
iac-code a2a-client --config a2a-client.yml push-config-delete \
  --task-id task-123 \
  --config-id webhook-1
```

## `iac-code a2a-client extended-card`

Récupère l'Agent Card étendue authentifiée.

```bash
iac-code a2a-client --config a2a-client.yml extended-card \
  --token "$A2A_TOKEN"
```

L'Agent Card publique annonce `capabilities.extendedAgentCard=true`. La carte étendue ajoute des détails runtime authentifiés, y compris les métadonnées de capacités de gestion des tâches et de configuration push.

## `iac-code a2a-route-preview`

Prévisualise la manière dont `a2a-client call` résout les routes configurées lorsque `--url` est omis.

```bash
iac-code a2a-route-preview \
  --route "template=http://127.0.0.1:41242/;skills=iac_generation;tags=ros,template" \
  --skill iac_generation \
  --prompt "Create a ROS VPC template"
```

| Option | Description |
|--------|-------------|
| `--route` | Spécification de route répétable au format `name=url;skills=a,b;tags=x,y` |
| `--name` | Nom de route à résoudre |
| `--skill` | ID de compétence à résoudre |
| `--prompt` | Texte de prompt utilisé pour la correspondance nom/tag |
| `--route-state-dir`, `--persistence-dir` | Répertoire utilisé pour persister les instantanés de route |
| `--save-routes` | Enregistrer les routes fournies dans le répertoire d'état des routes |

Enregistrer les instantanés de route :

```bash
iac-code a2a-route-preview \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros" \
  --route-state-dir ~/.iac-code/a2a \
  --save-routes
```

Appeler via les routes :

```bash
iac-code a2a-client call \
  --route "ros=http://127.0.0.1:41242/;skills=iac_generation;tags=ros" \
  --route-name ros \
  --prompt "Create a ROS VPC template." \
  --cwd "$PWD"
```

## Variables d'environnement

| Variable | Description |
|----------|-------------|
| `IACCODE_A2A_HTTP_TOKEN` | Valeur par défaut du jeton Bearer serveur/client |
| `IACCODE_A2A_BASIC_USERNAME` | Valeur par défaut du nom d'utilisateur Basic auth serveur/client |
| `IACCODE_A2A_BASIC_PASSWORD` | Valeur par défaut du mot de passe Basic auth serveur/client |
| `IACCODE_A2A_API_KEY` | Valeur par défaut de la clé API serveur/client |
| `IACCODE_A2A_API_KEY_HEADER` | Valeur par défaut du nom de l'en-tête de clé API |
| `IACCODE_A2A_ALLOWED_CWDS` | Liste, séparée par le séparateur de chemins du système d'exploitation, des racines d'espace de travail autorisées pour les métadonnées de message entrantes et les URL de fichier |
| `IACCODE_A2A_TEXT_MIME_TYPES` | Types MIME de type texte supplémentaires séparés par des virgules ou points-virgules |
| `IACCODE_A2A_MULTIMODAL_MIME_TYPES` | Types MIME multimodaux supplémentaires séparés par des virgules ou points-virgules |
| `IAC_CODE_A2A_PUSH_KEYRING` | Trousseau de clés secret push chiffré géré par l'environnement |
