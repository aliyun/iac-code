---
title: Identifiants Alibaba Cloud
description: Configurer les identifiants Alibaba Cloud, y compris l'authentification par rôle RAM ECS.
---

# Identifiants Alibaba Cloud

Ces étapes s’appliquent à Desktop et à Web, qui utilisent la même interface de conversation.

## Configurer l’accès cloud dans Desktop ou Web

1. Ouvrez **Paramètres → Identifiants cloud** et choisissez **Alibaba Cloud**.

2. Choisissez la région de consultation et de déploiement, par exemple `cn-hangzhou`.

3. Sélectionnez la méthode d’authentification et renseignez les champs ci-dessous.

4. Pour AccessKey, STS ou un rôle RAM/ECS RAM, cliquez sur **Enregistrer les identifiants cloud**. Pour OAuth, suivez les étapes de connexion par navigateur ci-dessous.

5. Revenez à la conversation et décrivez la tâche. L’enregistrement configure l’accès ; il ne crée aucune ressource et n’ajoute aucune permission RAM.

| Méthode | Champs requis |
|---|---|
| AccessKey | `AccessKeyId` et `AccessKeySecret`. |
| STS Token | `AccessKeyId`, `AccessKeySecret` et `StsToken` issus des mêmes identifiants temporaires. |
| Rôle RAM | Clés source `AccessKeyId` et `AccessKeySecret`, cible `RamRoleArn` et `RamSessionName` facultatif. |
| Rôle ECS RAM | Nom de rôle facultatif ; vide, il est découvert sur l’instance ECS qui exécute IaC Code. |
| OAuth par navigateur | Choisissez le site du compte et terminez l’autorisation dans le navigateur, sans saisir de jetons. |

Les identifiants Alibaba Cloud sont requis pour les opérations qui inspectent ou gèrent des ressources cloud.

Les outils `resolve_cloud_resource_selector` et `select_cloud_resource` suivent la même décision d'identifiants effectifs que `aliyun_api`. Dans A2A, les identifiants de la requête ou de la session sont prioritaires ; à défaut, la chaîne habituelle utilise la configuration locale, les variables d'environnement puis les identifiants Alibaba Cloud CLI. Sans identifiants effectifs valides, ni `aliyun_api` ni les deux outils de sélection ne sont enregistrés.

## Rôle RAM ECS

Utilisez **ECS RAM Role** lorsque IaC Code s'exécute sur une instance ECS Alibaba Cloud à laquelle un rôle RAM est associé. IaC Code obtient des identifiants STS temporaires auprès du service de métadonnées de l'instance ECS (IMDS), les renouvelle automatiquement et n'enregistre aucun AccessKey ID, AccessKey Secret ou jeton STS dans sa configuration.

Dans l'application Web ou Desktop, ouvrez **Paramètres > Identifiants cloud**, choisissez **Alibaba Cloud**, puis sélectionnez **ECS RAM Role** comme méthode d'authentification.

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

1. Ouvrez **Paramètres → Identifiants cloud**, choisissez **Alibaba Cloud** puis **Connexion OAuth par navigateur**.

2. Choisissez la région et le **Site de connexion** du compte : Chine ou International.

3. Cliquez sur **Se connecter via le navigateur**, puis connectez-vous et autorisez l’accès sur la page Alibaba Cloud.

4. Revenez à IaC Code et attendez la confirmation. Le flux enregistre les identifiants et le site sélectionné.

Les identifiants OAuth sont renouvelés automatiquement lorsque le jeton d’accès ou les identifiants STS approchent de leur expiration. Si le jeton de renouvellement expire ou est révoqué, autorisez à nouveau l’accès depuis **Paramètres → Identifiants cloud**.

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
