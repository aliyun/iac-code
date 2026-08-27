---
sidebar_position: 2
title: Bien démarrer
description: Installer, démarrer et appeler l’adaptateur AG-UI d’iac-code.
---

# Bien démarrer avec AG-UI

## Prérequis

1. Python 3.10 ou une version ultérieure est installé.
2. Un fournisseur LLM est configuré pour iac-code. Consultez [Authentification](../configuration/authentication.md).
3. Si la tâche accède à Alibaba Cloud, configurez des identifiants cloud ou fournissez des identifiants temporaires dans la requête.
4. Vous disposez d’un chemin absolu vers un espace de travail accessible en lecture et écriture par iac-code.

Installez les dépendances AG-UI :

```bash
pip install "iac-code[agui]"
```

Pour développer depuis le dépôt source :

```bash
uv sync --extra agui
```

## Option 1 : démarrer un noyau A2A local géré

Pour la configuration locale la plus simple, omettez `--a2a-url` :

```bash
iac-code agui --host 127.0.0.1 --port 41243
```

L’adaptateur choisit un port de bouclage disponible, démarre un processus enfant `iac-code a2a` et l’arrête à sa fermeture. L’enfant hérite de la configuration et de l’environnement d’exécution actuels.

Ce mode convient au développement local et à une gestion unifiée du cycle de vie. En production, utilisez l’option suivante si le superviseur doit gérer les deux services séparément.

## Option 2 : se connecter à un noyau A2A indépendant

Démarrez d’abord le serveur A2A :

```bash
iac-code a2a --host 127.0.0.1 --port 41242 --thinking-exposure all
```

Puis démarrez l’adaptateur AG-UI :

```bash
iac-code agui \
  --host 0.0.0.0 \
  --port 41243 \
  --a2a-url http://127.0.0.1:41242
```

Les services conservent des responsabilités et des ports distincts. A2A peut continuer à servir ses propres clients, tandis que l’adaptateur l’appelle uniquement par l’interface de bouclage.

`--thinking-exposure all` permet de convertir le raisonnement brut en événements standard `REASONING_*`. Ne l’activez que pour des clients de confiance. Conservez la valeur A2A par défaut, `tool-trace`, si le contenu du raisonnement ne doit pas être exposé.

Si le serveur A2A utilise un jeton bearer :

```bash
export IACCODE_A2A_HTTP_TOKEN="secret-a2a-local"
iac-code a2a --host 127.0.0.1 --port 41242
```

Fournissez le même jeton amont à l’adaptateur :

```bash
export IAC_CODE_AGUI_A2A_TOKEN="secret-a2a-local"
iac-code agui --port 41243 --a2a-url http://127.0.0.1:41242
```

## Configuration YAML

Les paramètres statiques peuvent être enregistrés dans un fichier YAML :

```yaml title="agui-server.yml"
host: 0.0.0.0
port: 41243
a2a-url: http://127.0.0.1:41242
interrupt-ttl: 540
state-dir: /var/lib/iac-code/agui
idle-shutdown: 0
debug: false
log-stdout: true
```

Démarrez l’adaptateur avec :

```bash
iac-code agui --config agui-server.yml
```

Les arguments CLI explicites remplacent le YAML. Injectez les valeurs sensibles, comme les jetons, par variables d’environnement plutôt que dans le fichier.

| CLI / YAML | Valeur par défaut | Signification |
|------------|-------------------|---------------|
| `--host` / `host` | `127.0.0.1` | Adresse d’écoute HTTP AG-UI |
| `--port` / `port` | `8000` | Port HTTP AG-UI ; les exemples de déploiement utilisent `41243` |
| `--a2a-url` / `a2a-url` | vide | URL A2A locale ; vide démarre un enfant géré |
| `--interrupt-ttl` / `interrupt-ttl` | `540` | Durée en secondes pendant laquelle une interruption peut être reprise |
| `--state-dir` / `state-dir` | `<config-dir>/agui` | Répertoire d’état des threads AG-UI |
| `--idle-shutdown` / `idle-shutdown` | `0` | Arrêt après inactivité ; `0` le désactive |
| `--debug` / `debug` | `false` | Journalisation de débogage |
| `--log-stdout` / `log-stdout` | `false` | Dupliquer les journaux sur stdout |

Variables d’environnement associées :

| Variable | Rôle |
|----------|------|
| `IAC_CODE_AGUI_HOST` | Adresse d’écoute AG-UI |
| `IAC_CODE_AGUI_PORT` | Port AG-UI |
| `IAC_CODE_AGUI_A2A_URL` | URL locale du service A2A amont |
| `IAC_CODE_AGUI_A2A_TOKEN` | Jeton bearer du service A2A amont |
| `IAC_CODE_AGUI_AUTH_TOKEN` | Jeton bearer protégeant le point d’accès AG-UI |
| `IAC_CODE_AGUI_INTERRUPT_TTL` | Durée de vie des interruptions |
| `IAC_CODE_AGUI_STATE_DIR` | Répertoire d’état des threads AG-UI |
| `IAC_CODE_AGUI_ALLOWED_CWDS` | Racines d’espace de travail autorisées, séparées par le séparateur de chemins du système |
| `IAC_CODE_CONFIG_DIR` | Racine de configuration d’iac-code et parent par défaut de l’état AG-UI |

## Vérification de l’état

```bash
curl http://127.0.0.1:41243/health
```

Exemple de réponse :

```json
{
  "status": "ok",
  "protocol": "ag-ui",
  "protocolPackageVersion": "0.1.20",
  "executionKernel": "a2a-1.0",
  "serverVersion": "version actuelle d’iac-code"
}
```

## Utiliser le client JavaScript officiel

Installez la version vérifiée :

```bash
pnpm add @ag-ui/client@0.0.58
```

Cet exemple se connecte directement à `iac-code agui` avec le `HttpAgent` standard et fournit les propriétés d’exécution dans `forwardedProps` :

```javascript
import { HttpAgent, randomUUID } from "@ag-ui/client";

const threadId = randomUUID();
const rosInvocationId = randomUUID();
const agent = new HttpAgent({
  url: "http://127.0.0.1:41243/",
  threadId,
  // Si IAC_CODE_AGUI_AUTH_TOKEN est configuré :
  // headers: { Authorization: `Bearer ${process.env.AG_UI_TOKEN}` },
});

const forwardedProps = {
  iacCode: {
    schemaVersion: 1,
    rosInvocationId,
    cwd: process.cwd(),
    runMode: "normal",
    preferredLanguage: "fr",
  },
};

agent.addMessage({
  id: randomUUID(),
  role: "user",
  content: "Crée un modèle VPC avec deux vSwitches.",
});

const subscriber = {
  onTextMessageContentEvent({ event }) {
    process.stdout.write(event.delta);
  },
  onToolCallStartEvent({ event }) {
    console.log(`\n[outil] ${event.toolCallName}`);
  },
  onStepStartedEvent({ event }) {
    console.log(`\n[étape] ${event.stepName}`);
  },
  onRunErrorEvent({ event }) {
    console.error(`\n${event.code}: ${event.message}`);
  },
};

await agent.runAgent({ forwardedProps }, subscriber);
```

Avec un jeton bearer, transmettez `Authorization` dans `HttpAgent.headers`. Une application web passe normalement par un backend de même origine ou un proxy inverse ; l’adaptateur n’ajoute pas de politique CORS.

## Traiter les interruptions

Le client officiel conserve `RUN_FINISHED.outcome.interrupts` dans `agent.pendingInterrupts`. Construisez chaque réponse à partir de son `responseSchema`, puis envoyez-la dans une nouvelle exécution :

```javascript
const responses = agent.pendingInterrupts.map((interrupt) => ({
  interruptId: interrupt.id,
  status: "resolved",
  payload: { decision: "allow_once" },
}));

await agent.runAgent({ forwardedProps, resume: responses }, subscriber);
```

Ce payload ne s’applique qu’aux autorisations dont le schéma exige `decision`. Les questions et choix d’options ont leurs propres schémas.

Une reprise doit utiliser le `threadId` d’origine, un nouveau `runId`, conserver le `rosInvocationId` de l’exécution interrompue, répondre en une seule requête à toutes les interruptions en attente et respecter chaque `responseSchema`. Utilisez `status: "cancelled"` lorsque l’utilisateur abandonne.

## Démarrer un Pipeline

Définissez `runMode` sur `pipeline` et choisissez éventuellement un Pipeline :

```javascript
const forwardedProps = {
  iacCode: {
    schemaVersion: 1,
    rosInvocationId: randomUUID(),
    cwd: process.cwd(),
    runMode: "pipeline",
    pipelineName: "selling",
    candidatePresentation: "rich",
  },
};
```

Les clients devraient traiter `STEP_*`, `TOOL_CALL_*`, `ACTIVITY_SNAPSHOT` et `CUSTOM`. Un client générique qui ignore les événements personnalisés d’iac-code continue de traiter normalement tous les événements standard.

## Espace de travail et identifiants temporaires

`cwd` n’est pas fixé au démarrage du serveur. Chaque requête doit fournir un chemin absolu sous une racine autorisée par `IAC_CODE_AGUI_ALLOWED_CWDS` ou `IACCODE_A2A_ALLOWED_CWDS`.

Le demandeur peut fournir, par requête, un modèle, une clé LLM et des identifiants Alibaba Cloud temporaires via `forwardedProps.iacCode`. L’adaptateur ne les écrit pas dans son état ; il les transmet au noyau A2A selon les règles habituelles de surcharge de requête.

## Répertoire d’état

Disposition par défaut :

```text
<IAC_CODE_CONFIG_DIR>/agui/
  threads/
    <threadId>.json
```

Chaque thread est écrit indépendamment et le démarrage ne parcourt pas l’historique. Les UUID normaux restent lisibles ; les identifiants dangereux sont encodés et les identifiants très longs utilisent une clé de fichier de longueur fixe. Le document JSON conserve et vérifie toujours le `threadId` original.

Ce répertoire contient uniquement les associations, interruptions et données d’idempotence de l’adaptateur. Il ne contient ni conversation ni identifiants de requête. Ne modifiez pas ces fichiers JSON manuellement.

## Étapes suivantes

- [Présentation d’AG-UI](./overview.md)
- [Référence du protocole](./protocol-reference.md)
