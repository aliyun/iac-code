---
title: Identifiants Alibaba Cloud
description: Configurer les identifiants AccessKey ou STS d'Alibaba Cloud.
---

# Identifiants Alibaba Cloud

Les identifiants Alibaba Cloud sont requis pour les opérations qui inspectent ou gèrent des ressources cloud.

## Connexion OAuth dans le navigateur

Le chemin de configuration interactive recommandé est `/auth` :

```text
/auth
```

Choisissez **Configurer le service cloud IaC**, puis **Alibaba Cloud**, puis **OAuth Login (Browser)**. IaC Code ouvre un flux d'autorisation dans le navigateur, attend le callback local, échange le code d'autorisation avec PKCE et enregistre des identifiants temporaires adossés à OAuth dans `.cloud-credentials.yml`, dans le répertoire de configuration d'IaC Code.

Pendant la configuration, vous pouvez choisir le site OAuth Chine ou international. IaC Code enregistre le site choisi avec le refresh token afin que les actualisations ultérieures utilisent le même endpoint.

Les identifiants OAuth sont actualisés automatiquement lorsque l'access token ou les identifiants STS arrivent bientôt à expiration. Si le refresh token expire ou est révoqué, exécutez de nouveau `/auth` et choisissez OAuth Login (Browser).

## Variables d'environnement

Variables d'environnement prises en charge :

| Variable | Description |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | Jeton STS ; bascule le mode d'identification vers STS lorsqu'il est défini |
| `ALIBABA_CLOUD_REGION_ID` | Région par défaut |

Utilisez des identifiants de test ou temporaires lors de vos expérimentations. Ne collez pas de secrets de production dans l'historique du shell, les captures d'écran, les journaux ou les rapports de problèmes.
