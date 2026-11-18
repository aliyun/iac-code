---
title: Authentification
description: Configurer les identifiants LLM et cloud avec le flux d'authentification.
---

# Authentification

Lancez `iac-code` dans un terminal pour démarrer l’application web locale et ouvrir le navigateur. À la première utilisation, configurez le fournisseur LLM et les identifiants Alibaba Cloud dans les paramètres.

- **Paramètres → Modèles** : choisissez le fournisseur, saisissez sa clé API et sélectionnez un modèle.
- **Paramètres → Identifiants cloud** : choisissez Alibaba Cloud, la méthode d’authentification et la région.

Web, Desktop et les exécutions headless partagent ces identifiants enregistrés.

Pour configurer depuis le terminal, lancez `iac-code tui`, puis exécutez `/auth`.

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
