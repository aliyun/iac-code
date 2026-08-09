---
title: Credenciais da Alibaba Cloud
description: Configure credenciais da Alibaba Cloud, incluindo autenticação por função RAM do ECS.
---

# Credenciais da Alibaba Cloud

As credenciais da Alibaba Cloud sao necessarias para operacoes que inspecionam ou gerenciam recursos na nuvem.

## Função RAM do ECS

Use **ECS RAM Role** quando o IaC Code for executado em uma instância ECS da Alibaba Cloud com uma função RAM associada. O IaC Code obtém credenciais STS temporárias do serviço de metadados da instância ECS (IMDS), atualiza-as automaticamente e não salva AccessKey ID, AccessKey Secret nem token STS na configuração.

Você pode configurar esse modo em todas as interfaces de usuário:

- No REPL, execute `/auth`, escolha **Configurar serviço de nuvem IaC**, depois **Alibaba Cloud** e **ECS RAM Role**.
- No aplicativo Web ou Desktop, abra **Configurações > Credenciais de nuvem**, escolha **Alibaba Cloud** e selecione **ECS RAM Role** como método de autenticação.

Selecione a região usada nas chamadas de API de nuvem. O nome da função RAM do ECS é opcional: deixe-o em branco para detectar, por meio do IMDS, a função associada à instância. O nome salvo no IaC Code tem prioridade sobre `ALIBABA_CLOUD_ECS_METADATA`; se nenhum dos dois estiver definido, o IaC Code solicita ao IMDS que detecte o nome da função.

A configuração equivalente em `.cloud-credentials.yml` é:

```yaml
aliyun:
  mode: EcsRamRole
  region_id: cn-beijing
  ram_role_name: MyEcsRole # Opcional; omita ou deixe vazio para detecção automática
```

O IaC Code também reconhece o perfil ativo em `~/.aliyun/config.json` quando o `mode` é `EcsRamRole`; `ram_role_name` também é opcional nesse arquivo.

A configuração pode ser salva em qualquer máquina, mas as chamadas de API de nuvem só funcionam quando o ECS IMDS está acessível e a instância tem uma função RAM correspondente. As políticas RAM associadas à função determinam quais APIs são permitidas.

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
| `ALIBABA_CLOUD_ECS_METADATA` | Nome opcional da função RAM do ECS; usado quando o modo já é `EcsRamRole` e nenhum nome foi salvo, mas não seleciona o modo por si só |
| `ALIBABA_CLOUD_ECS_METADATA_DISABLED` | Defina como `true` para desabilitar as credenciais de metadados da instância ECS |
| `ALIBABA_CLOUD_IMDSV1_DISABLED` | Defina como `true` para exigir IMDSv2 e impedir fallback para IMDSv1 |

Use credenciais de teste ou temporarias ao experimentar. Nao cole segredos de producao no historico do shell, capturas de tela, logs ou relatorios de problemas.
