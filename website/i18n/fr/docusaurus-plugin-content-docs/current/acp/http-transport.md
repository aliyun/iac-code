---
title: Transport HTTP+SSE
description: Exécuter le serveur ACP via HTTP avec Server-Sent Events pour les scénarios distants et multi-clients.
sidebar_position: 5
---

# Transport HTTP+SSE

Le serveur ACP d'iac-code prend en charge deux modes de transport. Le transport **Stdio** par défaut communique via l'entrée/sortie standard et est idéal pour les intégrations IDE locales. Le transport **HTTP+SSE** expose un point de terminaison réseau et diffuse les réponses via Server-Sent Events, ce qui le rend adapté aux déploiements distants, aux environnements à équilibrage de charge et à l'accès multi-clients.

## Pourquoi HTTP+SSE

Le Stdio présente des limitations inhérentes :

- Nécessite que le processus serveur soit un enfant direct du client -- pas d'accès distant.
- La gestion bloquante des processus rend difficile de servir plusieurs clients simultanément.
- Incompatible avec les proxys réseau, les équilibreurs de charge ou les déploiements conteneurisés.

HTTP+SSE répond à ces contraintes :

- **Compatible réseau** -- accessible depuis toute machine pouvant atteindre le point de terminaison.
- **Multi-clients** -- chaque client obtient une connexion isolée avec son propre flux d'événements.
- **Prêt pour l'infrastructure** -- fonctionne derrière des proxys inverses, dans des conteneurs, et avec les outils de surveillance HTTP standard.
- **Intégration facile** -- tout client HTTP (curl, fetch, SDK) peut interagir avec le serveur.

## Démarrage du serveur HTTP

```bash
# Default port 8765
iac-code acp --transport http

# Custom port
iac-code acp --transport http --port 9090
```

Le serveur utilise [Starlette](https://www.starlette.io/) comme framework ASGI et s'exécute sur Uvicorn.

## Routes

Toutes les routes sont servies au chemin `/acp`. La méthode HTTP détermine l'opération.

### `POST /acp`

Envoyer une requête JSON-RPC au serveur.

- **`initialize`** -- Crée une nouvelle connexion et retourne la réponse JSON-RPC complète directement. La réponse inclut un en-tête `Acp-Connection-Id`.
- **Toutes les autres méthodes** -- Nécessite un en-tête `Acp-Connection-Id` valide. Retourne `202 Accepted` immédiatement ; le résultat réel est livré de manière asynchrone via le flux SSE.

### `GET /acp`

Ouvre un flux Server-Sent Events pour recevoir les réponses et notifications.

- Nécessite l'en-tête `Acp-Connection-Id`.
- Les événements ont le type `message` avec la réponse/notification JSON-RPC comme champ `data`.
- Le flux inclut les champs `id` et `retry` pour la reconnexion automatique.

### `DELETE /acp`

Ferme la connexion et libère toutes les ressources associées.

- Nécessite l'en-tête `Acp-Connection-Id`.
- Retourne `200 OK`.

## Identifiant de connexion

L'identifiant de connexion lie les requêtes d'un client à son flux d'événements SSE.

1. Le client envoie un `POST /acp` avec la méthode `initialize`.
2. Le serveur répond avec le résultat d'initialisation et un en-tête de réponse `Acp-Connection-Id` contenant un UUID.
3. Toutes les requêtes suivantes (`POST`, `GET`, `DELETE`) doivent inclure l'en-tête de requête `Acp-Connection-Id` avec cette valeur.
4. Chaque identifiant de connexion correspond à une session d'agent ACP indépendante avec sa propre file d'événements.

Si une requête référence un identifiant de connexion manquant ou invalide, le serveur retourne `400 Bad Request`.

## Authentification

Le serveur prend en charge l'authentification optionnelle par jeton Bearer via la variable d'environnement `IACCODE_ACP_HTTP_TOKEN`.

```bash
# Set the token before starting the server
export IACCODE_ACP_HTTP_TOKEN=your-secret-token
iac-code acp --transport http
```

Lorsqu'il est défini, chaque requête doit inclure :

```
Authorization: Bearer your-secret-token
```

| Scénario | Comportement |
|----------|----------|
| Jeton non défini | Aucune authentification requise (adapté au développement local) |
| Jeton défini, en-tête correspondant | La requête se poursuit normalement |
| Jeton défini, en-tête manquant/incorrect | `401 Unauthorized` retourné |

## Flux de travail complet

Voici une interaction complète utilisant `curl` :

```bash
# Step 1: Initialize — creates a connection and returns the Connection ID
CONN_ID=$(curl -s -D - -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}' \
  | grep -i 'acp-connection-id' | awk '{print $2}' | tr -d '\r')

echo "Connection ID: $CONN_ID"

# Step 2: Open the SSE stream (run in background)
curl -N http://localhost:8765/acp \
  -H "Acp-Connection-Id: $CONN_ID" &
SSE_PID=$!

# Step 3: Create a session
curl -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: $CONN_ID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/workspace"}}'

# Step 4: Send a prompt
curl -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: $CONN_ID" \
  -d '{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"...","prompt":[{"type":"text","text":"Hello"}]}}'

# Step 5: Close the connection
curl -X DELETE http://localhost:8765/acp \
  -H "Acp-Connection-Id: $CONN_ID"

# Clean up background SSE process
kill $SSE_PID 2>/dev/null
```

:::tip
La réponse `initialize` est retournée de manière synchrone (dans un délai de 30 secondes). Toutes les réponses suivantes arrivent exclusivement via le flux SSE ouvert à l'étape 2.
:::
