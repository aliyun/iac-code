<p align="center">
  <img src="../website/static/img/logo-with-front.png" alt="iac-code" width="200">
</p>
<p align="center">
  <em>Assistant d'Infrastructure as Code (IaC) propulsé par l'IA qui génère et gère des templates d'infrastructure cloud via une interaction en langage naturel. Il prend actuellement en charge les workflows Alibaba Cloud ROS et Terraform.</em>
</p>
<p align="center">
  <a href="https://github.com/aliyun/iac-code/actions/workflows/test.yml"><img src="https://github.com/aliyun/iac-code/actions/workflows/test.yml/badge.svg" alt="Test"></a>
  <a href="https://pypi.org/project/iac-code"><img src="https://img.shields.io/pypi/v/iac-code?color=%2334D058&label=pypi%20package" alt="PyPI Package"></a>
  <a href="https://pypi.org/project/iac-code"><img src="https://img.shields.io/pypi/pyversions/iac-code?color=%2334D058&label=python" alt="Python"></a>
</p>
<p align="center">
  <strong>Language</strong>: <a href="../README.md">English</a> | <a href="README.zh.md">中文</a> | <a href="README.es.md">Español</a> | Français | <a href="README.de.md">Deutsch</a> | <a href="README.ja.md">日本語</a> | <a href="README.pt.md">Português</a>
</p>

> **Documentation** : [https://aliyun.github.io/iac-code/](https://aliyun.github.io/iac-code/fr/)

<p align="center">
  <a href="https://github.com/aliyun/iac-code/releases/latest"><img src="https://img.shields.io/badge/T%C3%A9l%C3%A9charger-IaC%20Code%20Desktop-5268f2?style=for-the-badge" alt="Télécharger IaC Code Desktop"></a>
  <br>
  <a href="https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-macos-arm64.dmg">macOS Apple Silicon</a> ·
  <a href="https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-windows-x64.exe">Windows x64</a> ·
  <a href="https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-linux-x64.AppImage">Linux AppImage</a> ·
  <a href="https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/desktop/stable/iac-code-linux-x64.deb">Linux deb</a> ·
  <a href="https://github.com/aliyun/iac-code/releases/latest">Tous les fichiers de la version</a>
</p>

## Application de bureau

Pas besoin de Python, de pip ni de configuration terminal : téléchargez l'application native et commencez immédiatement à construire votre infrastructure cloud.

Pour utiliser IaC Code comme une application native, téléchargez le paquet correspondant à votre plateforme depuis la [dernière version GitHub](https://github.com/aliyun/iac-code/releases/latest) :

- Mac avec puce Apple : `.dmg`
- Windows x64 : programme d'installation `.exe`
- Linux x64 : `.AppImage` ou `.deb`

L'application de bureau utilise le même moteur IaC Code et partage les fournisseurs de modèles, les identifiants cloud, les réglages, les projets et les sessions avec la CLI et l'application web. Au premier lancement, sélectionnez le dossier de projet dans lequel IaC Code doit travailler. Sous Windows, l'application vérifie également la présence de Git Bash et propose de vous guider dans son installation si nécessaire.

<p align="center">
  <img src="../website/static/img/screenshots/iac-code-desktop-en.jpg" alt="Application de bureau IaC Code" width="100%">
</p>

Les versions macOS, Windows et AppImage peuvent rechercher et appliquer dans l'application des mises à jour signées cryptographiquement. Le paquet deb se met à jour en installant une version plus récente. Les paquets macOS stables sont signés avec Apple Developer ID et notariés par Apple ; les paquets Windows stables portent une signature d'éditeur Authenticode. Téléchargez toujours les paquets depuis la page officielle de la version et vérifiez le fichier `SHA256SUMS` fourni. Consultez le [guide de l'application de bureau](https://aliyun.github.io/iac-code/fr/docs/desktop-app) pour les instructions et le dépannage.

## Installation

Le CLI et l'application web ci-dessous fonctionnent avec Python 3.10 ou ultérieur, sur macOS, Linux et Windows. Si vous utilisez uniquement l'application bureau, vous pouvez ignorer cette section.

> **Note Windows** : Sous Windows, [Git for Windows](https://gitforwindows.org/) doit être installé pour fournir l'environnement shell bash utilisé par l'exécution des outils. Si Git Bash est installé mais n'est pas dans le PATH, définissez la variable d'environnement `IAC_CODE_GIT_BASH_PATH`. S'il n'est pas encore installé, exécutez `iac-code install-git-bash` pour installer Git for Windows automatiquement (téléchargé via le miroir npmmirror).

```bash
pip install iac-code
```

## Utilisation

Lors de la première utilisation, configurez le fournisseur LLM et le service cloud IaC en saisissant `/auth` en mode interactif.

### Mode Interactif

Exécutez directement pour accéder au REPL interactif :

```bash
iac-code
```

<p align="center">
  <img src="../website/static/img/demo_en.gif" alt="iac-code demo" width="100%">
</p>

### Mode Non Interactif

Passez un prompt unique via `--prompt` :

```bash
iac-code --prompt "Créer un VPC et deux instances ECS"
```

La lecture depuis stdin est également prise en charge :

```bash
echo "Créer un bucket OSS" | iac-code --prompt -
```

### Application web

Vous préférez une interface graphique ? Lancez l'application web locale, qui exécute le même moteur que la CLI et partage les mêmes sessions. L'application web nécessite l'extra `http`, installez-le d'abord :

```bash
pip install 'iac-code[http]'
iac-code web
```

Par défaut, elle ouvre `http://127.0.0.1:8766` dans votre navigateur (bouclage uniquement). Consultez le [guide de l'application web](https://aliyun.github.io/iac-code/fr/web-app) pour plus de détails.

<p align="center">
  <img src="../website/static/img/screenshots/iac-code-web-en.jpg" alt="Application web IaC Code" width="100%">
</p>

### Agent Skill

Ajoutez IaC Code à un agent compatible pour concevoir des architectures cloud, travailler sur des templates ROS ou Terraform, estimer les coûts, gérer des stacks et déployer des ressources Alibaba Cloud depuis la conversation. Téléchargez le [dernier Skill stable](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip) ou comparez les distributions dans la [présentation des Skills IaC Code officiels](https://aliyun.github.io/iac-code/fr/docs/a2a/skill-overview).

## Contribuer

Installez [uv](https://docs.astral.sh/uv/getting-started/installation/), puis :

```bash
make install   # installer les dépendances et les hooks pre-commit
make dev       # exécuter en mode débogage
make test      # exécuter les tests
make lint      # exécuter les linters
make format    # formater le code
```

Consultez le [Guide de contribution](https://aliyun.github.io/iac-code/fr/getting-started/contributing) pour plus de détails.

## Contactez-nous

| [DingTalk](https://qr.dingtalk.com/action/joingroup?code=v1,k1,ubm/77U7qRh/STFZUNBP26X4PNg2z6+uhiPcLGtDNfU=&_dt_no_comment=1&origin=11) | [Discord](https://discord.gg/qECFuFBwF) |
| :----------------------------------------------------------: | :----------------------------------------------------------: |
| [<img src="../website/static/img/qrcode-dingtalk.jpg" width="120" height="120" alt="DingTalk">](https://qr.dingtalk.com/action/joingroup?code=v1,k1,ubm/77U7qRh/STFZUNBP26X4PNg2z6+uhiPcLGtDNfU=&_dt_no_comment=1&origin=11) | [<img src="../website/static/img/qrcode-discord.jpg" width="120" height="120" alt="Discord">](https://discord.gg/qECFuFBwF) |
