---
title: Autenticacao
description: Configurar o acesso ao modelo e à nuvem nas configurações do Desktop ou Web.
---

# Autenticacao

Abra o Desktop ou execute `iac-code` em um terminal interativo para abrir o Web. Configure o modelo e as credenciais de nuvem necessárias nas configurações.

- [Configurar um provedor no Desktop ou Web](./llm-providers.md)
- [Configurar acesso à nuvem no Desktop ou Web](./alibaba-cloud-credentials.md)

Web, Desktop e as execuções headless compartilham essas credenciais salvas.

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
