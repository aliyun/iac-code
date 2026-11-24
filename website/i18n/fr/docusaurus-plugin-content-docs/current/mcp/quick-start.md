---
sidebar_position: 2
title: Demarrage rapide MCP
description: Ajouter et connecter des serveurs MCP dans le panneau Plugins de Desktop ou Web.
---

# Demarrage rapide MCP

Dans les applications de bureau et web, ajoutez et gérez les serveurs dans l’onglet **MCP** de **Modules d’extension**. Les commandes CLI et fichiers de configuration ci-dessous servent à l’automatisation et aux réglages avancés.

## Connecter un serveur HTTP distant

1. Ouvrez **Modules d’extension → MCP**, choisissez le projet et cliquez sur **Ajouter un serveur**.

2. Saisissez le **Nom**, choisissez la **Portée** et le transport **http (distant)**.

3. Renseignez l’**URL** et les en-têtes requis ; référencez les secrets par variables d’environnement.

4. Cliquez sur **Créer**. Si la configuration de projet attend une approbation, vérifiez-la et cliquez sur **Approuver**.

5. Si nécessaire, cliquez sur **S’authentifier** et terminez l’autorisation dans le navigateur, puis revenez au panneau.

6. Cliquez sur **Vérifier la connexion**, puis **Voir les capacités**. Vérifiez la présence des outils, ressources ou prompts attendus avant utilisation.

## Connecter un serveur stdio local

1. Ouvrez **Modules d’extension → MCP**, choisissez le projet et cliquez sur **Ajouter un serveur**.

2. Renseignez le **Nom**, la **Portée** et sélectionnez **stdio (commande locale)**.

3. Indiquez la **Commande**, un argument par ligne dans **Arguments**, et les variables d’environnement nécessaires. L’exécutable doit exister sur la machine qui exécute IaC Code.

4. Cliquez sur **Créer** et approuvez la configuration de projet si nécessaire.

5. Utilisez **Vérifier la connexion** et **Voir les capacités** pour confirmer la connexion et la découverte.

:::note

La portée utilisateur couvre tous les projets ; la portée locale reste privée au répertoire choisi ; la portée projet écrit un `.mcp.json` partageable et exige une approbation locale. Pour modifier un serveur, ouvrez l’édition puis cliquez sur **Enregistrer**.

:::

## Automatisation CLI

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
iac-code mcp get yuque --check
```

```bash
iac-code mcp add local-files --scope user -- npx -y @modelcontextprotocol/server-filesystem ./templates
iac-code mcp get local-files --config-only
```

## Etapes suivantes

- Consultez [Configuration MCP](./configuration.md) pour les portées, fichiers de projet, en-têtes, options OAuth et formes JSON avancées.
- Consultez [OAuth et sécurité](./oauth-and-security.md) pour le stockage des jetons, la réinitialisation et les approbations.
- Consultez le [Dépannage](./troubleshooting.md) en cas d’approbation ou d’authentification requise, ou d’échec de connexion.
