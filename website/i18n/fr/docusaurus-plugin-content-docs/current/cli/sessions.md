---
title: Sessions
description: Conserver et reprendre les conversations entre les exécutions.
---

# Sessions

Dans Web et Desktop, sélectionnez le projet et ouvrez une conversation dans la barre latérale. Utilisez son menu pour la renommer. Les commandes de terminal ci-dessous utilisent le même stockage de sessions.

IaC Code enregistre automatiquement chaque conversation sur disque. Vous pouvez reprendre n'importe quelle session précédente pour continuer là où vous vous étiez arrêté.

## Reprendre des sessions

### Interactif : `/resume`

Dans le REPL, utilisez la commande `/resume` :

```text
/resume
```

Cela ouvre un sélecteur interactif qui affiche les sessions récentes du projet courant. Si un nom de session est défini, il sert de titre ; sinon le dernier prompt, ou à défaut le premier prompt, est utilisé.

Pour reprendre une session précise par identifiant exact, préfixe d'identifiant unique ou nom de session unique :

```text
/resume abc123
```

### Nommer les sessions

Utilisez `/rename` pour donner à la session active un nom stable et lisible :

```text
/rename deploy-prod
```

Le nom est stocké dans les métadonnées de session. Il apparaît dans la bannière d'accueil lors de la reprise, dans l'indication de sortie et dans le sélecteur `/resume`.

Vous pouvez reprendre par nom lorsqu'il identifie une session de façon unique :

```text
/resume deploy-prod
iac-code tui --resume deploy-prod
```

### CLI : `--resume` et `--continue`

Reprendre une session précise depuis la ligne de commande par identifiant exact, préfixe d'identifiant unique ou nom de session unique :

```bash
iac-code tui --resume <id-ou-nom-de-session>
```

Reprendre la session la plus récente :

```bash
iac-code tui --continue
```

Les options courtes `-r` et `-c` sont également disponibles :

```bash
iac-code tui -r <id-ou-nom-de-session>
iac-code tui -c
```

### Sessions inter-projets

Lorsqu'une session appartient à un autre répertoire de projet, IaC Code ne change pas le répertoire de travail à chaud. Il affiche plutôt la commande permettant de reprendre dans le bon contexte :

```text
cd /path/to/other/project && iac-code tui --resume <session-id>
```

Cette commande est aussi copiée dans le presse-papiers lorsque c'est possible.

## Récupération après interruption

Si une session a été interrompue pendant l'exécution, par exemple parce que le processus a été tué pendant qu'un outil tournait, IaC Code détecte les appels d'outil orphelins à la reprise et ajoute des résultats d'erreur synthétiques. Le modèle peut ainsi se rétablir proprement sans rester bloqué en attendant une sortie d'outil qui n'arrivera jamais.

## Sélecteur de sessions

Le sélecteur `/resume` affiche :

| Colonne | Description |
|---------|-------------|
| Titre | Nom de session s'il existe ; sinon dernier ou premier prompt utilisateur |
| Branche | Branche Git au moment de la session |
| Heure | Dernière modification |

Les sessions sont triées de la plus récente à la plus ancienne. Vous pouvez taper du texte pour filtrer par contenu du titre.
