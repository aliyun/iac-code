---
title: Contribuer
description: Comment configurer un environnement local et contribuer à IaC Code.
---

# Contribuer

## Prérequis

- Python 3.10+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## Installation

```bash
git clone https://github.com/aliyun/iac-code.git
cd iac-code
make install
```

`make install` installe toutes les dépendances et configure les hooks pre-commit (vérifications lint et format à chaque commit).

## Flux de développement

Exécuter en mode débogage :

```bash
make dev
```

Exécuter les tests :

```bash
make test           # version Python par défaut
make test PY=3.12   # version spécifique
make test PY=all    # toutes les versions supportées (3.10–3.14)
```

Qualité du code :

```bash
make lint      # ruff check + ty check
make format    # ruff format
```

Couverture :

```bash
make coverage
```

## Structure du projet

```
src/iac_code/       # code source
tests/              # tests
website/            # site de documentation (Docusaurus)
```

## Soumettre des modifications

1. Forkez le dépôt et créez une branche de fonctionnalité.
2. Effectuez vos modifications avec des tests.
3. Exécutez `make format`, puis assurez-vous que `make lint` et `make test` passent.
4. Ouvrez une pull request vers `main`.
