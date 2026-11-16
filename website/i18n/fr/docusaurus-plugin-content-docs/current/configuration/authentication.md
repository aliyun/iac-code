---
title: Authentification
description: Configurer les identifiants LLM et cloud avec le flux d'authentification.
---

# Authentification

Lancez `iac-code` dans un terminal pour démarrer l’application web locale et ouvrir le navigateur. À la première utilisation, configurez le fournisseur LLM et les identifiants Alibaba Cloud dans les paramètres.

L’interface de terminal reste accessible avec `iac-code tui`, mais ne reçoit plus de mises à jour ni de maintenance. Pour la suite, utilisez l’application web ou de bureau.

Utilisez `/auth` en mode interactif pour configurer à la fois l'accès au fournisseur de modèles et l'accès à Alibaba Cloud.

```bash
iac-code tui
```

```text
/auth
```

Le flux d'authentification vous guide à travers la sélection du fournisseur, la saisie de la clé API, la sélection du modèle et la configuration des identifiants Alibaba Cloud.

La configuration d'exécution est stockée dans le répertoire de configuration utilisateur :

```text
~/.iac-code/
```

Les fichiers importants comprennent :

| Fichier | Fonction |
|---|---|
| `.credentials.yml` | Identifiants du fournisseur LLM |
| `.cloud-credentials.yml` | Identifiants Alibaba Cloud |
| `settings.yml` | Paramètres d'exécution |
