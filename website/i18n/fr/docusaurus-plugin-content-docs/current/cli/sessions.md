---
title: Sessions
description: Conserver et reprendre les conversations entre les exécutions.
---

# Sessions

Web, bureau et headless partagent le même stockage de sessions. Les conversations sont enregistrées automatiquement dans le répertoire de configuration du runtime.

## Web et bureau

### Poursuivre une conversation

1. Dans la barre latérale, ouvrez la conversation sous son projet ou retrouvez-la avec **Rechercher**.

2. Consultez les messages et fichiers enregistrés.

3. Saisissez la demande suivante et envoyez-la. Pour une tâche indépendante, utilisez **Nouvelle discussion**.

### Renommer ou épingler une conversation

1. Ouvrez la conversation, puis son menu **…**.

2. Choisissez **Renommer la conversation**, saisissez le titre et cliquez sur **Enregistrer**. La barre latérale affiche le nouveau titre.

3. Choisissez **Épingler la conversation** dans le même menu pour la retrouver facilement. L’action inverse retire l’épingle.

### Archiver et restaurer une conversation

1. Choisissez **Archiver la conversation** dans le menu **…**. Elle quitte la liste active.

2. Ouvrez **Paramètres → Conversations archivées** et retrouvez-la avec la recherche et le filtre de projet.

3. Cliquez sur **Désarchiver**, puis ouvrez-la dans la barre latérale pour continuer.

:::note

Les conversations créées via CLI, A2A ou ACP sont masquées par défaut. Pour les consulter, activez l’option de lecture seule correspondante dans **Paramètres → Général → Visibilité des sessions externes**. Le stockage partagé ne permet pas de les modifier dans Web/Desktop.

:::

## Continuer en mode headless

Exécutez la commande depuis le répertoire du projet de la session. `--resume` reprend une session précise ; `--continue` utilise la plus récente de ce projet. Les formes courtes sont `-r` et `-c` et ne peuvent pas être combinées.

```bash
iac-code --resume deploy-prod --prompt "Générer un modèle VPC"
iac-code --continue --prompt "Générer un modèle VPC"
```

## Autres projets

Dans Web ou Desktop, ouvrez la conversation sous son projet dans la barre latérale. En headless, placez-vous d’abord dans le répertoire du projet :

```bash
cd /path/to/project
iac-code --resume <session-id> --prompt "Générer un modèle VPC"
```

## Conversations interrompues

Rouvrez la conversation enregistrée pour examiner l’historique et continuer. La récupération traite les appels d’outils inachevés. La progression et les décisions en attente suivent la [récupération Pipeline](../automation/pipeline-mode.md). Une exécution unique `--prompt` n’exécute pas de pipeline.
