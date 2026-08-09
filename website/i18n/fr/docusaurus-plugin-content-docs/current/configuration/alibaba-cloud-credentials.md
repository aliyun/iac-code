---
title: Identifiants Alibaba Cloud
description: Configurer les identifiants Alibaba Cloud, y compris l'authentification par rôle RAM ECS.
---

# Identifiants Alibaba Cloud

Les identifiants Alibaba Cloud sont requis pour les opérations qui inspectent ou gèrent des ressources cloud.

## Rôle RAM ECS

Utilisez **ECS RAM Role** lorsque IaC Code s'exécute sur une instance ECS Alibaba Cloud à laquelle un rôle RAM est associé. IaC Code obtient des identifiants STS temporaires auprès du service de métadonnées de l'instance ECS (IMDS), les renouvelle automatiquement et n'enregistre aucun AccessKey ID, AccessKey Secret ou jeton STS dans sa configuration.

Vous pouvez configurer ce mode depuis toutes les interfaces utilisateur :

- Dans le REPL, exécutez `/auth`, choisissez **Configurer le service cloud IaC**, puis **Alibaba Cloud** et **ECS RAM Role**.
- Dans l'application Web ou Desktop, ouvrez **Paramètres > Identifiants cloud**, choisissez **Alibaba Cloud**, puis sélectionnez **ECS RAM Role** comme méthode d'authentification.

Sélectionnez la région utilisée pour les appels aux API cloud. Le nom du rôle RAM ECS est facultatif : laissez-le vide pour détecter via IMDS le rôle associé à l'instance. Le nom enregistré dans IaC Code est prioritaire sur `ALIBABA_CLOUD_ECS_METADATA` ; si aucun des deux n'est défini, IaC Code demande à IMDS de détecter le nom du rôle.

La configuration `.cloud-credentials.yml` équivalente est la suivante :

```yaml
aliyun:
  mode: EcsRamRole
  region_id: cn-beijing
  ram_role_name: MyEcsRole # Facultatif ; omettez-le ou laissez-le vide pour la détection automatique
```

IaC Code reconnaît également le profil actif de `~/.aliyun/config.json` lorsque son `mode` vaut `EcsRamRole` ; `ram_role_name` y reste facultatif.

La configuration peut être enregistrée sur n'importe quelle machine, mais les appels aux API cloud n'aboutissent que si IMDS d'ECS est accessible et si un rôle RAM correspondant est associé à l'instance. Les politiques RAM associées au rôle déterminent les API autorisées.

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
| `ALIBABA_CLOUD_ECS_METADATA` | Nom facultatif du rôle RAM ECS ; utilisé lorsque le mode est déjà `EcsRamRole` et qu'aucun nom n'est enregistré, mais ne sélectionne pas le mode à lui seul |
| `ALIBABA_CLOUD_ECS_METADATA_DISABLED` | Définir à `true` pour désactiver les identifiants issus des métadonnées de l'instance ECS |
| `ALIBABA_CLOUD_IMDSV1_DISABLED` | Définir à `true` pour exiger IMDSv2 et interdire le fallback vers IMDSv1 |

Utilisez des identifiants de test ou temporaires lors de vos expérimentations. Ne collez pas de secrets de production dans l'historique du shell, les captures d'écran, les journaux ou les rapports de problèmes.
