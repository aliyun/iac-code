---
sidebar_position: 2
title: Installer et utiliser le Skill IaC Code
description: Ajoutez IaC Code à un agent compatible avec les Skills pour gérer l'infrastructure Alibaba Cloud.
---

# Installer et utiliser le Skill IaC Code

Le Skill IaC Code permet à un agent compatible de déléguer à IaC Code la conception d'architectures cloud, la
génération et la révision de templates ROS ou Terraform, l'estimation des coûts, la sélection de ressources, les
opérations sur les stacks ROS et le déploiement. Le paquet inclut un Runtime IaC Code vérifié ; aucune installation
séparée d'IaC Code n'est nécessaire.

## Télécharger

[Télécharger le dernier iac-code-skill.zip stable](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

Cette URL fixe désigne toujours la dernière version stable. Un installateur automatique peut lire
[latest.json](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/latest.json)
pour obtenir la version, l'URL immuable, la taille et le SHA-256, puis vérifier `skill.url` avec `skill.sha256`.

## Installer

Vérifiez que l'agent accepte les Skills locaux définis par `SKILL.md`, que CPython 3.8 à 3.14 est disponible et que
l'environnement peut accéder à l'URL de téléchargement. Utilisez `python3` sous macOS/Linux et `py -3` sous Windows.
Les Runtimes officiels prennent en charge macOS Apple Silicon, Linux x86_64 et Windows x86_64 ; le système et l'ABI
sont contrôlés avant le téléchargement.

Décompressez le ZIP dans le répertoire de Skills indiqué par l'agent. L'archive contient déjà `iac-code/` :

```text
<Racine des Skills de l'agent>/
└── iac-code/
    ├── SKILL.md
    ├── agents/openai.yaml
    └── scripts/iac_code.py
```

Emplacements courants :

- **Codex** : `~/.agents/skills/iac-code/` pour tous les projets, ou
  `<dépôt>/.agents/skills/iac-code/` pour un dépôt. Consultez la
  [documentation Codex Skills](https://developers.openai.com/codex/skills#where-codex-loads-local-skills).
- **Claude Code** : `~/.claude/skills/iac-code/` pour tous les projets, ou
  `<dépôt>/.claude/skills/iac-code/` pour un dépôt. Consultez la
  [documentation Claude Code Skills](https://code.claude.com/docs/en/skills#where-skills-live).

Redémarrez l'agent ou ouvrez une nouvelle session. Pour vérifier le Runtime depuis le répertoire `iac-code` :

```bash
python3 scripts/iac_code.py ensure-runtime
```

Sous Windows PowerShell, utilisez `py -3 scripts\iac_code.py ensure-runtime`. Au premier lancement, le Runtime adapté
est téléchargé et sa taille ainsi que son SHA-256 sont vérifiés ; la copie locale est ensuite réutilisée.

## Configurer le modèle et l'identité Alibaba Cloud

Le Skill utilise par défaut `~/.iac-code/` et réemploie les réglages du REPL, de l'application Web ou Desktop.
`IAC_CODE_CONFIG_DIR` permet de choisir un autre répertoire. Dans les environnements automatisés, injectez les réglages
du modèle et les identifiants Alibaba Cloud avec un gestionnaire de secrets. Ne placez aucun identifiant dans
`SKILL.md`, les prompts, les fichiers du projet ou l'historique du shell. Préférez les identifiants temporaires, les
rôles RAM ou OAuth avec le minimum de droits. Consultez [Fournisseurs LLM](../configuration/llm-providers.md) et
[Identifiants Alibaba Cloud](../configuration/alibaba-cloud-credentials.md).

## Choisir le mode de travail

- Le **mode normal** est utilisé par défaut pour consulter ou modifier des ressources, travailler sur des templates,
  diagnostiquer un problème et déployer une cible clairement définie.
- Le **mode Pipeline** est choisi à votre demande, ou lorsqu'un parcours guidé doit comparer des architectures et des
  coûts avant la confirmation et le déploiement.

Décrivez simplement le résultat attendu. Mentionnez Pipeline seulement si vous souhaitez comparer des solutions.

## Première utilisation

Dans une nouvelle session de l'agent hôte, saisissez par exemple :

```text
Utilise iac-code pour réviser le template ROS de ce projet. Liste les risques de sécurité et les améliorations sans modifier le fichier.
```

Sélectionnez explicitement le Skill avec `$iac-code` dans Codex ou `/iac-code` dans Claude Code. La vérification de la
configuration et le démarrage du Runtime sont automatiques ; aucun serveur A2A ne doit être lancé manuellement.

IaC Code peut s'arrêter pour vous demander :

- d'autoriser ou refuser une opération (`permission`) ;
- de répondre à une question (`ask_user_question`) ;
- de choisir une architecture (`candidate_selection`) ;
- de vérifier la solution, le prix et les paramètres, puis confirmer, ajuster, resélectionner ou annuler
  (`deployment_confirmation`).

Vérifiez les ressources, la région, l'impact et le prix avant de répondre. Une demande initiale de déploiement ne vaut
pas confirmation ultérieure. Après la fin d'une tâche, poursuivez dans la même session : le contexte IaC Code est
conservé. Les mises à jour sont disponibles en anglais, chinois simplifié, espagnol, français, allemand, japonais et
portugais.

## Mettre à jour et désinstaller

Pour mettre à jour, téléchargez à nouveau le ZIP stable et remplacez tout le dossier `iac-code/`, puis redémarrez
l'agent. Ne remplacez pas uniquement le pont et ne modifiez pas l'URL ou l'empreinte du Runtime. Pour désinstaller,
supprimez `iac-code/`. Les Runtimes restent en cache ; pour les supprimer, consultez d'abord `cache list`, puis lancez
`cache clean ... --confirm`.

## Dépannage

- `llm_not_configured` : complétez la configuration du modèle.
- `cloud_credentials_not_configured` : configurez les identifiants requis par Pipeline. Le mode normal peut continuer
  les tâches sans API cloud avec un avertissement.
- `incompatible_host` : exécutez `ensure-runtime`, puis vérifiez Python, le système, l'architecture, le réseau et le
  proxy. Mettez à niveau ou changez d'hôte au lieu de contourner le contrôle.
- Tâche en pause : elle attend une réponse, une autorisation, une sélection ou une confirmation de déploiement. Si la
  session existe encore après une interruption, demandez à l'agent de poursuivre la même tâche.

Utilisez `python3 scripts/iac_code.py cache list` pour inspecter le cache,
`cache clean --runtime-tag <tag> --confirm` pour supprimer une ancienne version et
`cache clean --candidates --confirm` pour les paquets candidats. Le Runtime courant ou actif est protégé.

## Sécurité

- Le Runtime écoute uniquement sur un port aléatoire de `127.0.0.1` avec un Bearer token propre au processus.
- Les résultats restent dans l'espace de travail, notamment sous `.iac-code-skill-results/` le cas échéant.
- Les états de préparation et résumés d'autorisation n'exposent aucune valeur d'identifiant.

## Documentation associée

- [Présentation des Skills IaC Code officiels](./skill-overview.md)
- [Référence d'intégration hôte du Skill IaC Code](./skill-host-integration.md)
- [Présentation du protocole A2A](./overview.md)
- [Configuration du Runtime](../configuration/runtime-configuration.md)
