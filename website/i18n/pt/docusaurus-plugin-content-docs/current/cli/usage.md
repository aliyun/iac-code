---
title: Visao geral do CLI
description: Inicie o IaC Code a partir do terminal e escolha o fluxo de trabalho adequado.
---

# Visao geral do CLI

Execute `iac-code` em um terminal para iniciar o aplicativo web local e abrir o navegador. No primeiro uso, configure o provedor LLM e as credenciais da Alibaba Cloud nas configurações.

```bash
iac-code
iac-code web --no-open
```

A interface de terminal continua disponível com `iac-code tui`, mas não recebe mais atualizações nem manutenção. Para continuar usando o produto, prefira o aplicativo web ou desktop.

```bash
iac-code tui
iac-code tui --help
```

`iac-code` abre o aplicativo web por padrão. Coloque as opções de terminal depois de `iac-code tui`, por exemplo `iac-code tui --resume <id>`. As opções headless `--prompt` e stream-json continuam no comando principal. Sem um terminal interativo, inicie a web explicitamente com `iac-code web --no-open`.

```bash
iac-code --prompt "Create an OSS Bucket"
iac-code --input-format stream-json --output-format stream-json
```
