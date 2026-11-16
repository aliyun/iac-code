---
title: Aperçu CLI
description: Démarrer IaC Code depuis le terminal et choisir le bon flux de travail.
---

# Aperçu CLI

Lancez `iac-code` dans un terminal pour démarrer l’application web locale et ouvrir le navigateur. À la première utilisation, configurez le fournisseur LLM et les identifiants Alibaba Cloud dans les paramètres.

```bash
iac-code
iac-code web --no-open
```

[Mode interactif](./interactive-mode.md):

```bash
iac-code tui
iac-code tui --help
```

`iac-code` ouvre l’application web par défaut. Placez les options du terminal après `iac-code tui`, par exemple `iac-code tui --resume <id>`. Les options headless `--prompt` et stream-json restent sur la commande principale. Sans terminal interactif, démarrez explicitement le serveur avec `iac-code web --no-open`.

```bash
iac-code --prompt "Create an OSS Bucket"
iac-code --input-format stream-json --output-format stream-json
```
