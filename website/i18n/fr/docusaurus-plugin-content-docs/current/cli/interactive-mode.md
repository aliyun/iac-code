---
title: Ancienne interface de terminal
description: Point d’entrée de compatibilité pour les anciens usages du terminal.
---

# Ancienne interface de terminal

TUI est l’ancienne interface permettant de converser et d’utiliser les outils directement dans un terminal. Cette page s’adresse aux utilisateurs de ces flux existants.

:::note

TUI ne reçoit plus de mises à jour ni de maintenance. Les nouvelles fonctions d’interface ciblent [Web](../web-app.md) et [Desktop](../desktop-app.md) ; les mêmes contrôles et présentations ne sont pas garantis dans le terminal.

:::

## Démarrer et utiliser TUI

1. Installez le [paquet Python](../getting-started/installation.md), puis configurez un [fournisseur de modèle](../configuration/llm-providers.md) et, si nécessaire, les [identifiants Alibaba Cloud](../configuration/alibaba-cloud-credentials.md). TUI peut réutiliser cette configuration enregistrée.

2. Ouvrez un terminal interactif, placez-vous dans le projet et lancez :

   ```bash
   cd /path/to/project
   iac-code tui
   ```

3. Après l’avis de fin de maintenance et l’apparition de la saisie, envoyez votre demande. Les réponses et l’activité des outils s’affichent dans le terminal. Répondez aux questions et demandes d’autorisation qui y apparaissent.

4. Utilisez `/help` pour voir les commandes de la version installée, `/status` pour consulter la session et `/exit` pour quitter.

TUI exige un terminal interactif. Pour les scripts ou la CI sans terminal, utilisez l’[automatisation headless](../automation/non-interactive-mode.md). Sans sous-commande, `iac-code` ouvre Web par défaut.

## Poursuivre une conversation du terminal

Exécutez la commande depuis le répertoire du projet de la conversation. Pour choisir une session par ID ou nom unique :

```bash
iac-code tui --resume <session-id-or-name>
```

Pour continuer la session la plus récente du projet courant :

```bash
iac-code tui --continue
```

N’utilisez pas `--resume` et `--continue` ensemble. Consultez `iac-code tui --help` pour les options de votre version.

## Passer à Web ou Desktop

1. Lancez `iac-code web` ou ouvrez Desktop, puis choisissez le même répertoire de projet.

2. Avec le même utilisateur système et `IAC_CODE_CONFIG_DIR`, les modèles, identifiants cloud et instructions mémorisées peuvent être réutilisés. Gérez-les ensuite dans les paramètres ; consultez les [opérations courantes](./commands.md).

3. Créez une nouvelle conversation pour poursuivre le travail. Pour consulter une ancienne conversation du terminal, activez l’option de lecture seule dans **Paramètres → Général → Visibilité des sessions externes**. Le stockage partagé ne rend pas ces conversations modifiables dans Web/Desktop ; voir [Sessions](./sessions.md).
