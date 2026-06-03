---
title: Credenciais da Alibaba Cloud
description: Configure credenciais AccessKey ou STS da Alibaba Cloud.
---

# Credenciais da Alibaba Cloud

As credenciais da Alibaba Cloud sao necessarias para operacoes que inspecionam ou gerenciam recursos na nuvem.

## Login OAuth no navegador

O caminho de configuração interativa recomendado é `/auth`:

```text
/auth
```

Escolha **Configurar serviço de nuvem IaC**, depois **Alibaba Cloud** e então **OAuth Login (Browser)**. O IaC Code abre um fluxo de autorização no navegador, aguarda o callback local, troca o código de autorização com PKCE e salva credenciais temporárias baseadas em OAuth em `.cloud-credentials.yml`, no diretório de configuração do IaC Code.

Durante a configuração, você pode escolher o site OAuth da China ou o internacional. O IaC Code salva o site escolhido junto com o refresh token para que atualizações futuras usem o mesmo endpoint.

As credenciais OAuth são atualizadas automaticamente quando o access token ou as credenciais STS estão perto de expirar. Se o refresh token expirar ou for revogado, execute `/auth` novamente e escolha OAuth Login (Browser).

## Variáveis de ambiente

Variaveis de ambiente suportadas:

| Variavel | Descricao |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | Token STS; muda o modo de credencial para STS quando definido |
| `ALIBABA_CLOUD_REGION_ID` | Regiao padrao |

Use credenciais de teste ou temporarias ao experimentar. Nao cole segredos de producao no historico do shell, capturas de tela, logs ou relatorios de problemas.
