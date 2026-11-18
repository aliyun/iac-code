---
title: Autenticacao
description: Configure as credenciais de LLM e nuvem com o fluxo de autenticacao.
---

# Autenticacao

Execute `iac-code` em um terminal para iniciar o aplicativo web local e abrir o navegador. No primeiro uso, configure o provedor LLM e as credenciais da Alibaba Cloud nas configurações.

- **Configurações → Modelos**: escolha o provedor, informe a chave de API e selecione um modelo.
- **Configurações → Credenciais de nuvem**: escolha Alibaba Cloud, o método de autenticação e a região.

Web, Desktop e as execuções headless compartilham essas credenciais salvas.

Para configurar pelo terminal, inicie `iac-code tui` e execute `/auth`.

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
