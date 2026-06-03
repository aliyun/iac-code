---
title: Commandes slash
description: Référence complète des commandes interactives intégrées.
---

# Commandes slash

Les commandes slash contrôlent IaC Code depuis l'intérieur d'une session interactive. Tapez `/` pour voir les commandes disponibles, puis continuez à taper pour filtrer la liste. Une commande n'est reconnue que lorsqu'elle apparaît au début de votre message.

La liste `/` inclut à la fois les commandes intégrées et toutes les skills que vous avez configurées. Pour restreindre les suggestions aux skills uniquement, utilisez `$` à la place — `$<nom>` liste et invoque exclusivement des skills, et taper `$` suivi du nom d'une commande intégrée (par exemple `$help`) affiche une erreur pointant vers l'équivalent `/`.

Le texte après le nom de la commande est transmis comme arguments. Dans le tableau ci-dessous, `<arg>` indique un argument obligatoire et `[arg]` indique un argument optionnel.

| Commande | Fonction |
|---|---|
| `/auth` | Configurer l'accès au fournisseur de modèles et les identifiants Alibaba Cloud via le flux d'authentification interactif. Utilisez cette commande lors de la première configuration d'IaC Code, du changement de clés API, du changement de fournisseur ou de la mise à jour de l'accès cloud. Alias : `/login`. |
| `/clear` | Effacer l'historique de conversation actuel et réinitialiser le gestionnaire de contexte actif. En mode interactif, cela efface également l'écran du terminal et réaffiche la bannière d'accueil. Utilisez cette commande lorsque vous souhaitez démarrer une nouvelle requête sans quitter le REPL. |
| `/compact` | Résumer la conversation actuelle pour réduire l'utilisation du contexte tout en préservant les échanges récents. Utilisez cette commande après une longue session lorsque vous souhaitez continuer à travailler avec moins de contexte accumulé. Si la conversation est vide ou trop courte, la commande signale qu'il n'y a rien à compacter. |
| `/debug [on\|off\|status]` | Inspecter ou modifier la journalisation de débogage à l'exécution pour la session active. `/debug` et `/debug status` indiquent si la journalisation est activée et, lorsqu'elle est activée, le chemin du fichier journal. `/debug on` active la journalisation pour la session en cours. `/debug off` la désactive. |
| `/effort [level]` | Afficher ou modifier l'effort de réflexion pour le modèle actif lorsque le modèle sélectionné prend en charge le contrôle d'effort. Avec un niveau, il applique la valeur demandée si elle est valide pour le modèle. Sans niveau, il ouvre un sélecteur interactif dans le REPL, ou affiche l'effort actuel dans les contextes non interactifs. |
| `/exit` | Quitter le REPL interactif. Alias : `/quit`, `/q`. |
| `/help` | Afficher les commandes disponibles et les raccourcis clavier courants dans le REPL. Alias : `/?`. |
| `/memory [<nom>\|search <requête>\|delete <nom>\|help]` | Lister, afficher, rechercher ou supprimer les mémoires enregistrées. La création de mémoires en langage naturel reste gérée par l'assistant via l'outil de mémoire lorsque vous lui demandez de se souvenir de quelque chose. |
| `/model [model_name]` | Afficher ou changer le modèle actif. Avec `model_name`, il bascule directement vers ce modèle pour le fournisseur actif. Sans argument, il ouvre un sélecteur de modèle interactif lorsqu'un fournisseur est configuré, ou affiche le modèle actuel lorsqu'aucune interface console n'est disponible. |
| `/rename <nom>` | Nommer la session actuelle. Les noms apparaissent dans la bannière d'accueil, l'indication de sortie et le sélecteur `/resume`, et peuvent être utilisés avec `/resume` ou `--resume` lorsqu'ils identifient une session de façon unique. |
| `/resume [id-de-session\|préfixe-id-unique\|nom-de-session-unique]` | Reprendre une session précédente. Avec un argument, IaC Code le résout comme identifiant exact, préfixe d'identifiant unique ou nom de session unique. Sans argument, il ouvre le sélecteur de session interactif. Les sessions inter-projets affichent une commande `cd ... && iac-code --resume <id>` au lieu de basculer le projet courant à chaud. |
| `/skills` | Ouvrir le sélecteur de gestion des compétences. Recherchez par nom ou description, triez par nom/source/taille et activez ou désactivez les compétences utilisateur ou projet. Les compétences intégrées restent verrouillées et activées. |
| `/status` | Afficher l'ID de session actuel, le fournisseur, le modèle, la région Alibaba Cloud, le répertoire de travail, l'utilisation enregistrée des tokens d'API, le nombre de tours et l'utilisation du contexte. |

La liste exacte des commandes peut varier entre les versions. Utilisez `/help` ou tapez `/` dans le REPL pour inspecter les commandes disponibles dans votre version installée.
