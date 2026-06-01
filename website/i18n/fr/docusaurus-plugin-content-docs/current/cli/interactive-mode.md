---
title: Mode interactif
description: Utiliser le REPL pour un travail itératif sur l'infrastructure.
---

# Mode interactif

Lancez sans arguments pour entrer dans le REPL interactif :

```bash
iac-code
```

Le mode interactif est utile lorsque vous souhaitez affiner les exigences d'infrastructure sur plusieurs échanges.

Commencez par l'authentification :

```text
/auth
```

Puis décrivez ce que vous souhaitez construire :

```text
Create a VPC, two ECS instances, and a security group that allows SSH from my office IP.
```

## Modifier la saisie

Utilisez `Shift+Enter` pour insérer une nouvelle ligne sans envoyer le prompt. Appuyez sur `Enter` seul pour envoyer le prompt complet.

Si votre terminal ne signale pas `Shift+Enter` séparément, appuyez sur `Esc` puis sur `Enter` pour insérer une nouvelle ligne. Les prompts multilignes sont enregistrés comme une seule entrée d'historique, donc `Up` restaure le prompt complet.

## Shell escapes

Préfixez une ligne avec `!` pour exécuter une commande shell locale depuis le REPL via l'outil `bash` intégré :

```text
!pwd
!git status --short
```

IaC Code applique les vérifications de permissions d'outil habituelles, exécute la commande dans le contexte du projet actuel et affiche la sortie dans le terminal. La commande n'est pas envoyée au modèle comme message de chat.
