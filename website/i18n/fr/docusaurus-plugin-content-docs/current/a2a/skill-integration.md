---
sidebar_position: 7
title: Installer et utiliser le Skill IaC Code
description: Téléchargez et installez le Skill IaC Code afin qu'un agent externe puisse gérer des ressources cloud Alibaba Cloud.
---

# Installer et utiliser le Skill IaC Code

Le Skill IaC Code s'adresse aux agents externes qui prennent en charge les Skills. Une fois installé, il permet à un
agent hôte de déléguer à IaC Code la conception d'architectures cloud, la génération et la vérification de modèles ROS
ou Terraform, l'estimation des coûts, le choix des ressources, les opérations sur les stacks et le déploiement. Le
Skill utilise un pont écrit uniquement avec la bibliothèque standard de Python pour démarrer un Runtime A2A local et
authentifié. Il n'est pas nécessaire d'installer IaC Code avec pip, et l'agent hôte ne doit pas se rabattre sur des
commandes headless.

## Télécharger le Skill

### Dernière version stable

Téléchargez directement la dernière version stable :

[Télécharger iac-code-skill.zip](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

Cette URL fixe pointe toujours vers le paquet du Skill promu sur le canal stable. Elle convient au téléchargement
depuis un navigateur et à l'installation manuelle, et ne change pas à chaque nouvelle version.

Les programmes d'installation qui ont besoin de la version, de la taille du fichier, de l'empreinte SHA-256 et de
l'URL immuable propre à la version peuvent consulter les métadonnées du canal stable :

[Consulter latest.json](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/latest.json)

Ce document contient :

- `skillVersion` : version stable actuelle du Skill ;
- `skill.url` : URL immuable du fichier ZIP correspondant à cette version ;
- `skill.sha256` et `skill.size` : valeurs utilisées pour vérifier le téléchargement ;
- `manifest.url` : manifeste de publication immuable correspondant à cette version.

Pour une vérification stricte ou une installation automatisée reproductible, lisez `latest.json`, téléchargez
`skill.url`, puis vérifiez `skill.sha256`. Ne construisez pas vous-même une URL à partir du numéro de version.

## Installer le Skill

### Prérequis

- L'agent hôte prend en charge les Skills locaux définis par un fichier `SKILL.md`.
- CPython 3.8 à 3.14 est installé. Utilisez `python3` sous macOS/Linux et, de préférence, `py -3` sous Windows.
- L'environnement peut accéder aux URL OSS ci-dessus afin de télécharger le fichier ZIP du Skill et le Runtime requis
  lors de la première utilisation.
- La configuration du service de modèles est disponible. Une identité Alibaba Cloud avec le principe du moindre
  privilège est également requise pour les tâches qui consultent ou gèrent des ressources cloud.

Les versions officielles du Skill Runtime prennent en charge les plateformes suivantes :

| Système d'exploitation | Architecture |
|---|---|
| macOS | Apple Silicon (arm64) |
| Linux | x86_64 |
| Windows | x86_64 |

Les versions minimales du système d'exploitation et de la glibc sous Linux sont définies par le manifeste du Runtime
épinglé par le Skill. Le pont vérifie la compatibilité avant le téléchargement. Sur une plateforme non prise en
charge, il renvoie une erreur au lieu de télécharger un artefact destiné à une autre plateforme ou ABI.

### Extraire le paquet dans le répertoire des Skills de l'agent hôte

Extrayez directement le fichier ZIP à la racine des Skills de l'agent hôte. L'emplacement exact dépend du produit ;
consultez la documentation de l'agent hôte. L'arborescence finale doit être la suivante :

```text
<Racine des Skills de l'agent>/
└── iac-code/
    ├── SKILL.md
    ├── agents/
    │   └── openai.yaml
    └── scripts/
        └── iac_code.py
```

Le fichier ZIP contient déjà le répertoire de premier niveau `iac-code/`. N'ajoutez pas un second répertoire du même
nom. Après une installation ou une mise à jour, redémarrez l'agent hôte ou ouvrez une nouvelle session afin qu'il
détecte à nouveau le Skill.

### Vérifier l'installation

Dans le répertoire `iac-code` extrait, exécutez la commande suivante sous macOS ou Linux :

```bash
python3 scripts/iac_code.py ensure-runtime
```

Dans Windows PowerShell, exécutez :

```powershell
py -3 scripts\iac_code.py ensure-runtime
```

Lors de la première exécution, cette commande télécharge le Runtime correspondant à la plateforme, vérifie sa taille
et son empreinte SHA-256, puis affiche un objet JSON contenant `skillVersion`, `runtimeTag` et le chemin
d'installation. Un Runtime déjà vérifié et mis en cache est réutilisé sans nouveau téléchargement.

## Configurer le modèle et l'identité Alibaba Cloud

Le Skill Runtime utilise le même répertoire de configuration que les autres modes de IaC Code : `~/.iac-code/` par
défaut. Si IaC Code est déjà configuré via le REPL, l'application Web ou l'application Desktop, le Skill peut
réutiliser ces paramètres. Définissez `IAC_CODE_CONFIG_DIR` pour employer un autre répertoire de configuration.

Dans les environnements automatisés, fournissez les variables suivantes au moyen d'une solution de gestion des
secrets :

| Catégorie | Variable d'environnement | Description |
|---|---|---|
| Modèle | `IAC_CODE_PROVIDER` | Fournisseur du modèle |
| Modèle | `IAC_CODE_MODEL` | Nom du modèle |
| Modèle | `IAC_CODE_API_KEY` | Clé API du service de modèles |
| Modèle | `IAC_CODE_BASE_URL` | Remplacement facultatif de l'endpoint compatible |
| Alibaba Cloud | `ALIBABA_CLOUD_ACCESS_KEY_ID` | ID de l'AccessKey |
| Alibaba Cloud | `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | Secret de l'AccessKey |
| Alibaba Cloud | `ALIBABA_CLOUD_SECURITY_TOKEN` | Jeton de sécurité pour les identifiants STS |
| Alibaba Cloud | `ALIBABA_CLOUD_REGION_ID` | Région par défaut |

Ne placez jamais d'identifiants réels dans `SKILL.md`, les prompts de l'agent hôte, les fichiers du projet ou
l'historique du shell. Privilégiez les identifiants temporaires, les rôles RAM ou OAuth, et n'accordez que les
autorisations d'API cloud nécessaires à la tâche. Pour les instructions complètes, consultez
[Fournisseurs de LLM](../configuration/llm-providers.md) et
[Identifiants Alibaba Cloud](../configuration/alibaba-cloud-credentials.md).

## Première utilisation

Après l'installation et la configuration, ouvrez une nouvelle session dans l'agent hôte et décrivez directement une
tâche d'infrastructure Alibaba Cloud. Par exemple :

```text
Utilise iac-code pour vérifier le modèle ROS de ce projet. Répertorie les risques de sécurité et les modifications recommandées sans modifier le fichier.
```

Les agents hôtes qui prennent en charge une syntaxe explicite peuvent sélectionner le Skill avec `$iac-code`.
L'agent hôte lit `SKILL.md`, écrit la demande complète dans un fichier UTF-8 de l'espace de travail, puis utilise le
pont pour créer et suivre une seule tâche. L'utilisateur n'a pas besoin de démarrer manuellement un serveur A2A.

Déroulement attendu :

1. Le pont vérifie que la configuration du modèle et d'Alibaba Cloud est prête.
2. Lors de la première utilisation, il télécharge et vérifie le Runtime IaC Code épinglé par le Skill.
3. Le Runtime écoute uniquement sur un port aléatoire de `127.0.0.1` et génère un jeton Bearer propre au processus.
4. L'agent hôte présente la progression, les questions, les plans candidats et les demandes d'autorisation renvoyés
   par IaC Code.
5. Une fois la tâche terminée, l'agent hôte renvoie le résultat final et les fichiers générés dans l'espace de travail.

## Mettre à jour et désinstaller

Pour effectuer une mise à jour manuelle, téléchargez à nouveau `skill/stable/iac-code-skill.zip` et remplacez
l'intégralité du répertoire `iac-code/` dans la racine des Skills de l'agent hôte. Un programme de mise à jour
automatique peut comparer la valeur `skillVersion` de `latest.json`, puis télécharger et vérifier le nouveau paquet à
l'aide de son URL immuable et de son empreinte SHA-256. Chaque Skill officiel est épinglé à un Runtime vérifié. Ne
remplacez pas uniquement `scripts/iac_code.py` et ne modifiez pas manuellement l'URL ou l'empreinte du Runtime.

Pour désinstaller le Skill, supprimez `iac-code/` de la racine des Skills de l'agent hôte. Le cache du Runtime n'est
pas supprimé avec le répertoire du Skill. N'exécutez `cache list` et `cache clean` que si l'utilisateur demande
explicitement de supprimer ce cache.

## Cache du Runtime

Le Runtime téléchargé lors de la première utilisation est mis en cache dans
`<IAC_CODE_CONFIG_DIR ou ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/` et réutilisé automatiquement. En usage
normal, il n'est pas nécessaire de gérer ce répertoire. Pour examiner l'espace disque utilisé ou supprimer
d'anciennes versions, utilisez :

- `python3 scripts/iac_code.py cache list` — répertorie les Runtimes installés et les paquets candidats ;
- `python3 scripts/iac_code.py cache clean [--runtime-tag <tag>] [--candidates] --confirm` — supprime les caches du
  Runtime ou les paquets candidats ; l'option `--confirm` est obligatoire.

Le Runtime actuel et tout Runtime utilisé par un processus actif sont protégés contre le nettoyage. Le format du
paquet et les contraintes du Runtime sont définis par `skill-runtime/skill-package-contract.json` dans le dépôt
source ; les utilisateurs n'ont pas à modifier ce fichier.

## Résolution des problèmes

### La configuration est incomplète

Le Skill vérifie la configuration avant de créer une tâche, mais ne lit ni ne renvoie jamais les valeurs secrètes :

| Situation | Résultat |
|---|---|
| Le fournisseur de LLM ou la clé API est incomplet | Renvoie `llm_not_configured` et ne crée pas la tâche |
| Les identifiants Alibaba Cloud sont incomplets pour le Pipeline de vente | Renvoie `cloud_credentials_not_configured` et ne crée pas la tâche |
| Les identifiants Alibaba Cloud sont incomplets en mode normal | Les tâches qui n'appellent pas d'API cloud peuvent continuer avec un avertissement préalable |

### Pourquoi l'exécution se met en pause

IaC Code se met en pause lorsqu'il attend une autorisation, une information complémentaire ou le choix d'un plan.
L'agent hôte présente directement la demande :

- une demande d'autorisation pour un outil ou un déploiement (`permission`) ;
- une question à choix multiple ou une demande d'informations (`ask_user_question`) ;
- le choix d'un plan candidat du Pipeline (`candidate_selection`).

Avant de confirmer, vérifiez la ressource cible, la région, l'impact prévu et le prix. L'agent hôte ne peut pas passer
outre un refus de IaC Code. Dans le protocole, une autorisation ponctuelle est représentée par `allow_once`.

> **Note pour l'intégration de l'agent hôte**
>
> Lorsqu'un résultat du pont contient `inputRequired`, l'agent hôte doit présenter la demande en cours et attendre une
> réponse. `boundaryReached` indique une limite d'affichage ou d'interaction, et non la fin de la tâche ; l'agent hôte
> doit afficher la mise à jour et continuer à suivre la même tâche.

## Sécurité

- Le Runtime écoute uniquement sur un port aléatoire de `127.0.0.1`. Chaque démarrage génère un nouveau jeton Bearer,
  transmis avec chaque requête du pont.
- Le pont conserve les artefacts et les résultats dans l'espace de travail de la tâche. Les résultats sont enregistrés
  dans `.iac-code-skill-results/`.
- Les champs affichés lors des vérifications préalables et des demandes d'autorisation sont nettoyés ; aucun secret ni
  identifiant n'y apparaît.

## Documentation associée

- [Présentation du protocole A2A](./overview.md)
- [Référence du protocole A2A](./protocol-reference.md)
- [Fournisseurs de LLM](../configuration/llm-providers.md)
- [Identifiants Alibaba Cloud](../configuration/alibaba-cloud-credentials.md)
- [Configuration du Runtime](../configuration/runtime-configuration.md)
