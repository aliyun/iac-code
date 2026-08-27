---
sidebar_position: 1
title: Protocole AG-UI
description: Architecture, fonctionnalités et cas d’usage de l’intégration AG-UI d’iac-code.
---

# Protocole AG-UI

## Qu’est-ce qu’AG-UI ?

Le [protocole d’interaction agent-utilisateur (AG-UI)](https://docs.ag-ui.com/concepts/architecture) est un protocole de flux d’événements reliant des agents à des applications destinées aux utilisateurs. Un client démarre une exécution avec `RunAgentInput`, puis reçoit par HTTP Server-Sent Events (SSE) des événements structurés pour le texte, le raisonnement, les appels d’outils, les étapes, l’état et les interruptions.

AG-UI convient aux consoles web, clients de chat, extensions d’IDE et autres applications qui doivent afficher l’exécution d’un agent en temps réel. Au lieu de recevoir uniquement le texte final, le client peut présenter séparément la sortie du modèle, les arguments et résultats des outils, les étapes d’un Pipeline et les opérations en attente de confirmation.

## Architecture d’iac-code

iac-code utilise un **noyau d’exécution A2A associé à un adaptateur de protocole AG-UI** :

```text
Client AG-UI
    ↓ RunAgentInput / SSE
iac-code agui
    ↓ A2A 1.0 HTTP
iac-code a2a
    ↓
Boucle de l’agent / Pipeline / LLM / API Alibaba Cloud
```

`iac-code a2a` est l’unique noyau d’exécution. Il gère :

- les conversations normales et l’exécution des Pipelines ;
- les sessions iac-code ainsi que les contextes et tâches A2A ;
- les autorisations d’outils, questions, choix d’options et reprises ;
- le cycle de vie et l’annulation des exécutions ;
- les appels au LLM et aux API Alibaba Cloud.

`iac-code agui` ne crée pas un second runtime Agent et n’exécute pas directement les Pipelines. Il se limite à :

- convertir `RunAgentInput` en requêtes A2A ;
- projeter les événements A2A en événements AG-UI standard ;
- associer `threadId/runId` à `contextId/taskId` ;
- convertir `resume[]` en reprise d’entrée A2A ;
- persister les associations de protocole et les interruptions en attente ;
- transmettre les annulations à A2A.

AG-UI et A2A partagent donc les mêmes règles d’exécution. Le choix du modèle, les identifiants cloud, les autorisations et le comportement du Pipeline restent gérés par le runtime A2A.

## Protocole standard et extensions iac-code

Le flux externe utilise les événements AG-UI standard :

- `RUN_STARTED`, `RUN_FINISHED` et `RUN_ERROR` ;
- `TEXT_MESSAGE_*` ;
- `REASONING_*` ;
- `TOOL_CALL_*` ;
- `STEP_STARTED` et `STEP_FINISHED` ;
- `ACTIVITY_SNAPSHOT`.

Seules les informations de Pipeline utiles qui n’ont pas d’équivalent standard sont envoyées dans des événements `CUSTOM` avec espace de noms. Un client AG-UI générique peut les ignorer sans perturber le texte, les outils, les interruptions ou le cycle de vie de l’exécution.

Les requêtes conservent l’enveloppe standard `RunAgentInput`. Le champ standard `forwardedProps` transporte l’espace de travail, le mode d’exécution et les autres données nécessaires :

```json
{
  "forwardedProps": {
    "iacCode": {
      "schemaVersion": 1,
      "rosInvocationId": "identite-requete",
      "cwd": "/chemin/absolu/espace-travail",
      "runMode": "normal"
    }
  }
}
```

Un client générique peut donc consommer directement les événements standard d’iac-code. Pour appeler `iac-code agui` directement, il doit néanmoins fournir les données d’exécution obligatoires, notamment `cwd`, sous `forwardedProps.iacCode`.

## Interactions prises en charge

### Conversations normales en plusieurs tours

Conservez le même `threadId` pour toute la conversation et utilisez un nouveau `runId` pour chaque tour utilisateur. L’adaptateur lie le thread à une session iac-code. Une fois un tour terminé, le message suivant ouvre une nouvelle requête HTTP/SSE ; il ne prolonge jamais l’ancienne réponse SSE déjà terminée.

### Pipeline

Définissez `forwardedProps.iacCode.runMode` sur `pipeline`. Le noyau A2A exécute toujours le Pipeline. Les étapes principales deviennent des événements standard `STEP_*`, tandis que le texte, le raisonnement et les outils utilisent leurs événements standard respectifs. Les informations sur les candidats, la progression des stacks et le nettoyage sans équivalent standard sont envoyées via `iac-code.pipeline.v1`.

Les sous-Pipelines parallèles utilisent des identités de messages et d’étapes distinctes, afin que le texte de plusieurs boucles d’agent ne soit pas fusionné.

### Interruption et reprise

Lorsqu’une autorisation, une question ou un choix exige une réponse, l’exécution courante se termine ainsi :

```json
{
  "type": "RUN_FINISHED",
  "outcome": {
    "type": "interrupt",
    "interrupts": []
  }
}
```

L’interruption est persistée avant d’être visible par le client. Après avoir recueilli les réponses, celui-ci démarre une nouvelle requête avec le même `threadId`, un nouveau `runId` et `resume[]`. Le flux de reprise appartient à cette nouvelle requête et ne se reconnecte pas à l’ancien flux.

### État de l’adaptateur

L’adaptateur conserve les associations de protocole, les données d’idempotence et les interruptions en attente dans un fichier par thread. Ce répertoire ne contient ni texte de conversation, ni clés LLM, ni identifiants cloud, et ne sert pas à exporter les conversations.

## Quel protocole choisir ?

| Besoin | Mode recommandé |
|--------|-----------------|
| Créer une interface de chat avec texte, raisonnement, outils et étapes en direct | **AG-UI** |
| Gérer les autorisations, questions et choix dans une interface | **AG-UI** |
| Permettre à un autre agent ou orchestrateur d’appeler directement iac-code | **A2A** |
| Intégrer un IDE/éditeur avec sessions ACP et terminal | **ACP** |
| Utiliser iac-code manuellement | **REPL interactif ou Web/Desktop** |

AG-UI et A2A peuvent fonctionner simultanément. Ils exposent des points d’accès HTTP distincts tout en partageant la même implémentation d’exécution.

## Limites actuelles

- Le transport AG-UI repose sur HTTP POST et SSE.
- Le service A2A en amont doit utiliser une adresse de bouclage ; l’adaptateur refuse les URL A2A distantes arbitraires.
- `cwd` est obligatoire pour chaque requête et doit se trouver sous une racine d’espace de travail autorisée.
- Les `tools` définis par le client ne sont pas encore acceptés ; iac-code contrôle l’ensemble des outils.
- Les messages utilisateur acceptent le texte et les images base64 intégrées, mais pas les URL de médias distants.
- Si le client se déconnecte d’une exécution SSE active avant une interruption, l’adaptateur annule la tâche A2A correspondante.
- Le flux SSE envoie un commentaire heartbeat toutes les 15 secondes. Les clients conformes l’ignorent.

## Étapes suivantes

- [Bien démarrer](./getting-started.md) — installer, démarrer et connecter un premier client.
- [Référence du protocole](./protocol-reference.md) — champs de requête, événements, interruptions, reprise, persistance et erreurs.
