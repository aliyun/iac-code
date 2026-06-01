<p align="center">
  <img src="../website/static/img/logo-with-front.png" alt="iac-code" width="200">
</p>
<p align="center">
  <em>Assistant d'Infrastructure as Code (IaC) propulsé par l'IA qui génère et gère des modèles d'orchestration de ressources Alibaba Cloud (ROS / Terraform) via une interaction en langage naturel.</em>
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
  <img src="../website/static/img/demo_en.gif" alt="iac-code demo" width="100%">
</p>

## Installation

IaC Code nécessite Python 3.10 ou supérieur. Il est compatible avec macOS, Linux et Windows.

> **Note Windows** : Sous Windows, [Git for Windows](https://gitforwindows.org/) doit être installé pour fournir l'environnement shell bash utilisé par l'exécution des outils. Si Git Bash est installé mais n'est pas dans le PATH, définissez la variable d'environnement `IAC_CODE_GIT_BASH_PATH`.

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

### Mode Non Interactif

Passez un prompt unique via `--prompt` :

```bash
iac-code --prompt "Créer un VPC et deux instances ECS"
```

La lecture depuis stdin est également prise en charge :

```bash
echo "Créer un bucket OSS" | iac-code --prompt -
```

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
