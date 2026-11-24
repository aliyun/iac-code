---
title: Visao geral do CLI
description: Inicie o IaC Code a partir do terminal e escolha o fluxo de trabalho adequado.
---

# Visao geral do CLI

Execute `iac-code` em um terminal para iniciar o aplicativo web local e abrir o navegador. No primeiro uso, configure o provedor LLM e as credenciais do Alibaba Cloud nas Configurações.

```bash
iac-code
iac-code web --no-open
```

Ao criar uma conversa no Web ou Desktop, escolha o chat normal ou Pipeline. Para automação, use um prompt único ou um subprocesso stream-json:

```bash
iac-code --prompt "Gerar um modelo de VPC"
iac-code --input-format stream-json --output-format stream-json
```

Consulte as [opções de linha de comando](./command-line-options.md) e a [automação headless](../automation/non-interactive-mode.md). Sem um terminal interativo, inicie a web explicitamente com `iac-code web --no-open`.

Para fluxos de terminal anteriores, consulte a [entrada de compatibilidade](./interactive-mode.md).
