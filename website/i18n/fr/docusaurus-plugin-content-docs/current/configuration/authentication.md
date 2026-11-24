---
title: Authentification
description: Configurer les accès au modèle et au cloud dans les paramètres de Desktop ou Web.
---

# Authentification

Ouvrez Desktop, ou lancez `iac-code` dans un terminal interactif pour ouvrir Web. Configurez le modèle et les identifiants cloud nécessaires dans les paramètres.

- [Configurer un fournisseur dans Desktop ou Web](./llm-providers.md)
- [Configurer l’accès cloud dans Desktop ou Web](./alibaba-cloud-credentials.md)

Web, Desktop et les exécutions headless partagent ces identifiants enregistrés.

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
