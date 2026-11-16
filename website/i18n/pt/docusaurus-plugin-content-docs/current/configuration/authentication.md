---
title: Autenticacao
description: Configure as credenciais de LLM e nuvem com o fluxo de autenticacao.
---

# Autenticacao

Execute `iac-code` em um terminal para iniciar o aplicativo web local e abrir o navegador. No primeiro uso, configure o provedor LLM e as credenciais da Alibaba Cloud nas configurações.

A interface de terminal continua disponível com `iac-code tui`, mas não recebe mais atualizações nem manutenção. Para continuar usando o produto, prefira o aplicativo web ou desktop.

Use `/auth` no modo interativo para configurar tanto o acesso ao provedor de modelos quanto o acesso a Alibaba Cloud.

```bash
iac-code tui
```

```text
/auth
```

O fluxo de autenticacao guia-o pela selecao do provedor, entrada da chave de API, selecao do modelo e configuracao das credenciais da Alibaba Cloud.

A configuracao em tempo de execucao e armazenada no diretorio de configuracao do utilizador:

```text
~/.iac-code/
```

Os arquivos importantes incluem:

| Arquivo | Finalidade |
|---|---|
| `.credentials.yml` | Credenciais do provedor de LLM |
| `.cloud-credentials.yml` | Credenciais da Alibaba Cloud |
| `settings.yml` | Configuracoes em tempo de execucao |
