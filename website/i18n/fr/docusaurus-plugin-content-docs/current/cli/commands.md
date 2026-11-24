---
title: Opérations courantes
description: Utiliser les paramètres, les menus de conversation et le champ de saisie sur bureau et web.
---

# Opérations courantes

Ces étapes s’appliquent à Desktop et à Web, qui utilisent la même interface de conversation.

## Démarrer une conversation

1. Ouvrez Desktop ou lancez `iac-code` dans un terminal interactif pour ouvrir Web.

2. Cliquez sur **Nouvelle discussion** dans la barre latérale. Sous la saisie, choisissez le répertoire du projet contenant vos fichiers ou destiné aux modèles générés.

3. Avant le premier envoi, choisissez **Mode normal** ou **Mode pipeline**. Pour Pipeline, sélectionnez aussi le pipeline.

4. Saisissez votre demande et envoyez-la. La conversation apparaît sous son projet dans la barre latérale ; vous pouvez y poursuivre le travail.

## Joindre un fichier existant ou une image

1. Ouvrez une conversation du projet contenant le fichier.

2. Tapez `@` puis une partie du nom du fichier et sélectionnez le résultat. Une référence apparaît près de la saisie. Pour une image, utilisez le bouton de pièce jointe.

3. Ajoutez une instruction, par exemple « Vérifie ce modèle ROS et explique ses paramètres », contrôlez les références sélectionnées et envoyez.

## Choisir le modèle et le raisonnement

1. Suivez d’abord les [étapes de configuration du modèle](../configuration/llm-providers.md).

2. Dans la conversation, ouvrez le sélecteur de modèle sous la saisie et choisissez un fournisseur et un modèle disponibles.

3. Si le modèle le permet, sélectionnez l’effort de raisonnement et activez ou désactivez **Réflexion** pour cette conversation.

4. Vérifiez le modèle affiché avant d’envoyer la demande suivante. Les options de raisonnement dépendent du modèle.

## Répondre à une demande d’autorisation

1. Avant l’envoi, ouvrez le contrôle des permissions dans la saisie et choisissez le mode voulu.

2. Lorsqu’une carte d’approbation apparaît, vérifiez le nom de l’outil et l’opération proposée, notamment le fichier ou l’API cloud ciblé.

3. Approuvez ou refusez dans la carte. Une approbation permet la poursuite de l’opération ; un refus est transmis à l’assistant. Répondez séparément aux cartes de questions.

## Modifier les instructions et gérer la mémoire

1. Ouvrez **Paramètres → Mémoire** et choisissez le projet en haut.

2. Dans **Mémoire du projet**, saisissez les instructions propres à ce projet et cliquez sur son bouton **Enregistrer**.

3. Dans **Mémoire de l’utilisateur**, saisissez les préférences communes aux projets et cliquez sur le bouton **Enregistrer** correspondant. Vérifiez le message de confirmation.

4. Activez ou désactivez **Mémoire automatique → Enregistrement automatique** pour permettre à l’assistant de mémoriser des faits utiles. Le changement est enregistré automatiquement.

5. Recherchez les entrées dans la **Bibliothèque de mémoire** pour les consulter. Pour retirer une entrée obsolète, utilisez sa commande de suppression et confirmez.

## Changer la langue ou l’apparence

1. Ouvrez **Paramètres → Général**.

2. Choisissez la **Langue**. Elle est enregistrée automatiquement, puis la page se recharge.

3. Cliquez sur un thème dans **Palette de couleurs**. L’interface change et enregistre ce choix automatiquement.

## Autres tâches

Consultez les procédures de [gestion des sessions](./sessions.md), des [skills](./skills.md) et le [démarrage MCP](../mcp/quick-start.md).

## Raccourcis facultatifs

Dans une conversation normale existante, `/` affiche quelques raccourcis :

| Raccourci | Fonction |
|---|---|
| `/status` | Afficher l’état de la session. |
| `/compact` | Résumer la conversation pour réduire le contexte. |
| `/mcp` | Afficher l’état MCP ; gérez les connexions dans **Modules d’extension → MCP**. |

Pipeline masque `/compact` ; ces raccourcis ne sont pas proposés dans le brouillon d’une nouvelle conversation. Les suggestions proposent aussi les skills : sélectionnez-en un, ajoutez votre demande et envoyez. Voir [Skills](./skills.md).
