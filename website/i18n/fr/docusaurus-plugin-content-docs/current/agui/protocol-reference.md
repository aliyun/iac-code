---
sidebar_position: 3
title: Référence du protocole
description: Référence des requêtes, événements, interruptions, reprises, annulations et de la persistance AG-UI d’iac-code.
---

# Référence du protocole AG-UI

Cette page décrit l’interface HTTP/SSE exposée par `iac-code agui` et les champs d’extension iac-code transportés dans les enveloppes AG-UI standard. Consultez d’abord la [présentation](./overview.md) et le [guide de démarrage](./getting-started.md).

## Points d’accès HTTP

| Méthode et chemin | Rôle |
|-------------------|------|
| `GET /health` | État du service et versions du protocole |
| `POST /` | Envoyer `RunAgentInput` et recevoir un flux SSE |
| `POST /extensions/iac-code/v1/executions/{executionId}/cancel` | Extension d’annulation avec espace de noms |

Le corps de `POST /` doit être en JSON et le client doit demander SSE :

```http
Content-Type: application/json
Accept: text/event-stream
```

Si `IAC_CODE_AGUI_AUTH_TOKEN` est configuré :

```http
Authorization: Bearer <token>
```

L’en-tête standard `Accept-Language` sert de langue de repli pour les erreurs. `forwardedProps.iacCode.preferredLanguage` est prioritaire et est aussi transmis au runtime A2A.

## RunAgentInput

Exemple minimal d’exécution normale :

```json
{
  "threadId": "8473547e-c8ed-4aef-a84c-603a6a8d42da",
  "runId": "32c263f2-b0b0-42ac-905c-524a0a9bb652",
  "state": {},
  "messages": [
    {"id": "message-1", "role": "user", "content": "Créer un modèle VPC"}
  ],
  "tools": [],
  "context": [],
  "forwardedProps": {
    "iacCode": {
      "schemaVersion": 1,
      "rosInvocationId": "invocation-1",
      "cwd": "/workspace/session-1",
      "runMode": "normal"
    }
  }
}
```

### Champs standard

| Champ | Exigence | Comportement d’iac-code |
|-------|----------|-------------------------|
| `threadId` | Chaîne non vide obligatoire | Identité stable de conversation, associée à un contexte A2A et une session iac-code |
| `runId` | Chaîne non vide obligatoire | Une exécution HTTP/SSE ; ne peut pas être réutilisée dans le thread |
| `parentRunId` | Facultatif | Recopié dans `RUN_STARTED` |
| `state` | Obligatoire | Conservé dans l’enveloppe standard, mais non utilisé comme état runtime d’iac-code |
| `messages` | Obligatoire | Une nouvelle exécution utilise le dernier message utilisateur ; une reprise n’a pas besoin d’en ajouter |
| `tools` | Obligatoire et vide | Les outils définis par le client ne sont pas pris en charge |
| `context` | Obligatoire | Conservé dans l’enveloppe, mais pas encore converti en contexte de prompt |
| `forwardedProps` | Obligatoire | Doit contenir l’extension `iacCode` |
| `resume` | Pour une reprise | Une réponse pour chaque interruption en attente |

Les messages utilisateur acceptent les chaînes, les parties `text` et les parties `image` contenant une source `data` base64 intégrée. Les URL d’image distantes, l’audio, la vidéo, les documents et les binaires génériques ne sont pas pris en charge. Une image décodée est limitée à 8 Mio, l’ensemble des images à 10 Mio et la requête HTTP complète à 12 Mio.

## `forwardedProps.iacCode`

Le schéma est strict : les champs inconnus sont refusés.

| Champ | Type | Obligatoire | Signification |
|-------|------|-------------|---------------|
| `schemaVersion` | `1` | Oui | Version de l’extension iac-code |
| `rosInvocationId` | chaîne | Oui | Identité du demandeur pour l’exécution courante, 256 caractères maximum |
| `cwd` | chaîne | Oui | Chemin absolu de l’espace de travail |
| `model` | chaîne | Non | Modèle choisi pour cette requête |
| `llmApiKey` | chaîne | Non | Clé du fournisseur LLM pour cette requête |
| `thinking.enabled` | booléen | Non | Demander la sortie du raisonnement |
| `thinking.effort` | chaîne | Non | Effort de raisonnement propre au fournisseur |
| `thinking.budget` | entier positif | Non | Budget de raisonnement propre au fournisseur |
| `userId` | chaîne | Non | Identité de télémétrie et de liaison du demandeur |
| `channel` | chaîne | Non | Métadonnées du canal appelant |
| `preferredLanguage` | chaîne | Non | Langue d’affichage locale à la requête, par exemple `fr` |
| `candidatePresentation` | `standard` ou `rich` | Non | Présentation des candidats du Pipeline |
| `runMode` | `normal` ou `pipeline` | Non | Mode d’exécution, sinon choisi par A2A |
| `pipelineName` | chaîne | Non | Nom du Pipeline, par exemple `selling` |
| `cleanupOnly` | booléen | Non | Demander uniquement le nettoyage du Pipeline |
| `alibabaCloud.accessKeyId` | chaîne | Non | AccessKey ID locale à la requête |
| `alibabaCloud.accessKeySecret` | chaîne | Non | Secret AccessKey local à la requête |
| `alibabaCloud.securityToken` | chaîne | Non | Jeton STS local à la requête |
| `alibabaCloud.regionId` | chaîne | Non | Région par défaut locale à la requête |

L’exécution initiale et ses reprises doivent conserver le même `rosInvocationId`. Un tour normal ultérieur peut utiliser une nouvelle valeur. L’annulation doit employer celle de l’exécution courante.

Le `threadId` est lié aux `cwd` et `userId` de la première requête ; les requêtes suivantes ne peuvent pas déplacer le thread vers un autre espace de travail ou un autre demandeur.

## SSE et heartbeat

Chaque événement AG-UI est envoyé dans un enregistrement SSE `data:`. Après 15 secondes sans événement, le serveur envoie :

```text
: heartbeat
```

Il s’agit d’un commentaire SSE, pas d’un événement AG-UI `CUSTOM`. Les clients conformes l’ignorent tout en maintenant la connexion HTTP active.

## Correspondance des événements standard

| Signal A2A/iac-code | Sortie AG-UI |
|---------------------|--------------|
| Requête acceptée | `RUN_STARTED` |
| Texte de l’agent | `TEXT_MESSAGE_START/CONTENT/END` |
| Raisonnement brut | `REASONING_START`, `REASONING_MESSAGE_*`, `REASONING_END` |
| Démarrage et arguments d’un outil | `TOOL_CALL_START/ARGS/END` |
| Résultat d’un outil | `TOOL_CALL_RESULT` |
| Cycle de vie d’une étape de Pipeline | `STEP_STARTED/STEP_FINISHED` |
| Instantané de reprise du Pipeline | `ACTIVITY_SNAPSHOT` |
| Fin normale | `RUN_FINISHED` avec `outcome.type = "success"` |
| Saisie utilisateur requise | `RUN_FINISHED` avec `outcome.type = "interrupt"` |
| Erreur de l’adaptateur ou d’A2A | `RUN_ERROR` |

`RUN_FINISHED` termine une exécution AG-UI, pas nécessairement tout le Pipeline. Un Pipeline interrompu plusieurs fois possède plusieurs exécutions, chacune avec ses propres `RUN_STARTED` et `RUN_FINISHED`. La fin métier du Pipeline est indiquée par `pipeline_completed`, `pipeline_error` et les événements apparentés.

Pour équilibrer les spans AG-UI, l’adaptateur ferme les messages, raisonnements, outils et étapes ouverts avant qu’une interruption ne termine l’exécution. La reprise rouvre les étapes durables encore actives. Une trace brute peut donc montrer la même étape métier se fermer dans une exécution puis se rouvrir dans la suivante ; l’ordre métier n’est pas inversé.

## Événements personnalisés iac-code

### `iac-code.session.v1`

Expose l’association courante entre l’adaptateur et A2A : `threadId`, `aguiRunId`, `executionId`, `contextId`, `taskId`, `rosInvocationId` et `sessionId`. Utilisez `executionId` avec l’extension d’annulation. Un client générique peut ignorer cet événement.

### `iac-code.artifact.v1`

Transporte une projection structurée d’un artefact de tâche A2A, pour un aperçu, un téléchargement ou un diagnostic facultatif.

### `iac-code.tool-progress.v1`

Transporte la progression intermédiaire d’un outil sans équivalent standard. Le démarrage, les arguments et le résultat final restent des événements standard `TOOL_CALL_*` et ne sont pas dupliqués ici.

### `iac-code.pipeline.v1`

Seules les informations utiles sans équivalent standard complet sont émises. Valeurs `eventType` actuelles :

- Pipeline : `pipeline_started`, `pipeline_resumed`, `pipeline_completed`, `pipeline_error`, `pipeline_warning`, `backup_blocked` ;
- candidats : `candidate_started`, `candidate_completed`, `candidate_failed`, `candidate_interrupted`, `candidate_restart_requested`, `candidate_selected`, `candidate_detail_shown`, `candidate_step_failed` ;
- sous-Pipelines et erreurs d’étape : `sub_pipeline_started`, `sub_pipeline_completed`, `sub_step_failed`, `step_failed` ;
- stacks et nettoyage : `stack_progress`, `stack_instances_progress`, `stack_current_changed`, `cleanup_started`, `cleanup_progress`, `cleanup_completed`, `cleanup_failed` ;
- rollback : `rollback_triggered`, `rollback_completed` ;
- contexte : `context_compaction_started`, `context_compacted`, `context_compaction_failed`, `fields_marked_stale` ;
- présentation et outils : `diagram_shown`, `mcp_status`, `tool_progress`.

Les signaux disposant d’une correspondance standard ne sont pas dupliqués en `CUSTOM` : `text_delta` devient `TEXT_MESSAGE_*`, `thinking_delta` devient `REASONING_*`, `tool_started/tool_result` deviennent `TOOL_CALL_*`, `usage` devient `RUN_FINISHED.usage` et les cycles d’étapes deviennent `STEP_*`.

Les clients devraient dédupliquer les événements de Pipeline rejoués avec `(name, value.eventId)` ou la séquence du Pipeline, et tolérer les événements personnalisés inconnus avec espace de noms.

## Interruption

Une exécution nécessitant une saisie se termine par `RUN_FINISHED.outcome.type = "interrupt"`. Chaque interruption contient :

- `id` et `reason` ;
- un `message` destiné à l’utilisateur ;
- un `toolCallId` facultatif ;
- un `responseSchema` JSON ;
- `expiresAt` ;
- des métadonnées comme `title`, `purpose`, `safeSummary`, `options` et `toolName`.

Pour une demande d’autorisation, le schéma accepte généralement :

```json
{"decision": "allow_once"}
```

ou :

```json
{"decision": "deny"}
```

Affichez `message`, `responseSchema` et les métadonnées descriptives au lieu de déduire l’interface uniquement depuis `reason`. Les questions et choix d’options peuvent utiliser d’autres schémas.

## Reprise

Une reprise est un nouveau `POST /` avec le même `threadId`, un nouveau `runId`, le même `rosInvocationId` et une entrée par interruption en attente :

```json
{
  "resume": [
    {
      "interruptId": "permission-1",
      "status": "resolved",
      "payload": {"decision": "allow_once"}
    }
  ]
}
```

Règles :

- répondre exactement une fois à chaque interruption en attente ;
- les identifiants dupliqués ou inconnus sont refusés ;
- `resolved` exige un payload conforme au schéma ;
- `cancelled` arrête l’interruption et correspond à `deny` pour une autorisation ;
- l’état durable n’est supprimé qu’après acceptation par A2A ;
- une erreur de schéma produit `RUN_ERROR` sans empêcher une nouvelle tentative ;
- répéter une réponse déjà acceptée ne réexécute pas l’outil.

Avant d’appliquer une reprise, l’adaptateur peut demander à A2A de restaurer la session iac-code, vérifie l’identité de la tâche et du contexte A2A, puis récupère les événements de Pipeline manquants.

## Tours et identités

```text
threadId (conversation stable)
  ├─ runId-1 (tour utilisateur)
  ├─ runId-2 (reprise d’interruption)
  ├─ runId-3 (autre reprise)
  └─ runId-4 (message normal suivant)
```

Chaque requête HTTP/SSE utilise un `runId` unique. Une reprise est une nouvelle exécution. Après un tour normal, le message suivant crée une nouvelle exécution tout en réutilisant la session iac-code du thread. L’idempotence est limitée à `(threadId, runId)`.

## Extension d’annulation

```http
POST /extensions/iac-code/v1/executions/<executionId>/cancel
Content-Type: application/json
```

```json
{"threadId": "thread-1", "rosInvocationId": "invocation-1"}
```

Résultats possibles : `cancelled`, `already_terminal`, ou HTTP `404` avec `EXECUTION_NOT_FOUND`. L’annulation supprime les interruptions en attente sans modifier le format des événements AG-UI standard.

## Persistance et reprise après arrêt

Répertoire par défaut :

```text
<config-dir>/agui/threads/<thread-key>.json
```

Chaque fichier contient la liaison thread/contexte/espace de travail, les identités de session, tâche et exécution, les positions de reprise du Pipeline, les interruptions en attente et les données d’idempotence. L’adaptateur charge à la demande un seul thread et remplace atomiquement uniquement son petit fichier.

Les clés LLM, secrets AccessKey et jetons STS n’y sont jamais enregistrés. Ce répertoire sert aux associations de l’adaptateur, pas aux conversations ni aux artefacts. A2A gère sa propre persistance de sessions et de tâches ; consultez la [documentation A2A](../a2a/overview.md).

Lors de l’accès suivant, une interruption expirée est refusée, son état en attente est supprimé et l’adaptateur tente d’annuler la tâche A2A correspondante.

## Déconnexions

- Une exécution terminée proprement par une interruption ne dépend plus de sa connexion SSE.
- Une reprise crée une nouvelle connexion SSE.
- Déconnecter une exécution ordinaire active conduit l’adaptateur à annuler la tâche A2A.
- Une déconnexion après une interruption ne supprime pas son état de reprise persistant.

## Erreurs

Les erreurs antérieures au démarrage de SSE utilisent une enveloppe JSON HTTP. Pendant l’exécution, elles utilisent les événements standard `RUN_ERROR`.

| Code | Signification |
|------|---------------|
| `INVALID_INPUT` | Enveloppe, extension, message ou espace de travail invalide |
| `DUPLICATE_RUN_ID` | Même empreinte de requête avec un run ID existant |
| `RUN_ID_CONFLICT` | Une requête différente réutilise un run ID |
| `THREAD_BUSY` | Le thread exécute déjà une requête |
| `THREAD_BINDING_CONFLICT` | Espace de travail ou demandeur incompatible avec la liaison du thread |
| `RESUME_REQUIRED` | Le thread attend des réponses d’interruption |
| `INCOMPLETE_RESUME` | Interruptions manquantes ou identifiants dupliqués |
| `UNKNOWN_INTERRUPT` | Interruption inconnue dans la reprise |
| `RESUME_PAYLOAD_INVALID` | Payload absent ou non conforme au schéma |
| `RESUME_ALREADY_APPLIED` | Réponse déjà appliquée ou en conflit |
| `EXECUTION_EXPIRED` | Interruption expirée |
| `EXECUTION_LOST` | Impossible de restaurer l’adaptateur, la tâche A2A ou la session iac-code |
| `STATE_PERSISTENCE_FAILED` | Impossible de persister un état critique pour la reprise |
| `A2A_UNAVAILABLE` | Service d’exécution A2A local indisponible |
| `A2A_PROTOCOL_ERROR` | Identité tâche/contexte/session incompatible avec l’association |
| `A2A_EXECUTION_FAILED` | Échec de la tâche A2A |
| `CANCELLED` | Exécution annulée |

Les écritures critiques pour la reprise échouent de manière sûre. L’adaptateur n’annonce pas une tâche, une session ou une interruption récupérable avant que son association soit persistée, et annule la tâche A2A correspondante si nécessaire.
