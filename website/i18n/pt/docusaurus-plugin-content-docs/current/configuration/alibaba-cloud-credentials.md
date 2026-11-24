---
title: Credenciais da Alibaba Cloud
description: Configure credenciais da Alibaba Cloud, incluindo autenticação por função RAM do ECS.
---

# Credenciais da Alibaba Cloud

Os passos valem para Desktop e Web, que usam a mesma interface de conversa.

## Configurar acesso à nuvem no Desktop ou Web

1. Abra **Configurações → Credenciais de nuvem** e escolha **Alibaba Cloud**.

2. Selecione a região de consulta e implantação, como `cn-hangzhou`.

3. Escolha o método de autenticação e preencha os campos correspondentes na tabela.

4. Para AccessKey, STS ou funções RAM/ECS RAM, clique em **Salvar credenciais de nuvem**. Para OAuth, siga o login por navegador abaixo.

5. Volte à conversa e descreva a tarefa. Salvar credenciais configura o acesso; não cria recursos nem acrescenta permissões RAM.

| Método | Campos necessários |
|---|---|
| AccessKey | `AccessKeyId` e `AccessKeySecret`. |
| STS Token | `AccessKeyId`, `AccessKeySecret` e `StsToken` da mesma credencial temporária. |
| Função RAM | Chaves de origem `AccessKeyId` e `AccessKeySecret`, destino `RamRoleArn` e `RamSessionName` opcional. |
| Função ECS RAM | Nome de função opcional; em branco, é descoberto na instância ECS que executa o IaC Code. |
| OAuth por navegador | Escolha o site da conta e autorize no navegador, sem digitar tokens manualmente. |

As credenciais da Alibaba Cloud sao necessarias para operacoes que inspecionam ou gerenciam recursos na nuvem.

As ferramentas `resolve_cloud_resource_selector` e `select_cloud_resource` usam a mesma decisão de credencial efetiva que `aliyun_api`. No A2A, as credenciais da solicitação ou sessão têm prioridade; quando ausentes, aplica-se a cadeia normal de configuração local, variáveis de ambiente e credenciais da Alibaba Cloud CLI. Sem uma credencial efetiva válida, nem `aliyun_api` nem as duas ferramentas de seleção são registradas.

## Função RAM do ECS

Use **ECS RAM Role** quando o IaC Code for executado em uma instância ECS da Alibaba Cloud com uma função RAM associada. O IaC Code obtém credenciais STS temporárias do serviço de metadados da instância ECS (IMDS), atualiza-as automaticamente e não salva AccessKey ID, AccessKey Secret nem token STS na configuração.

No aplicativo Web ou Desktop, abra **Configurações > Credenciais de nuvem**, escolha **Alibaba Cloud** e selecione **ECS RAM Role** como método de autenticação.

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

1. Abra **Configurações → Credenciais de nuvem**, escolha **Alibaba Cloud** e **Login OAuth pelo navegador**.

2. Selecione a região e o **Site de login** da conta: China ou Internacional.

3. Clique em **Entrar com o navegador** e conclua o login e a autorização no Alibaba Cloud.

4. Volte ao IaC Code e aguarde a confirmação. O fluxo salva as credenciais e o site selecionado.

As credenciais OAuth são renovadas automaticamente quando o token de acesso ou as credenciais STS estão próximos de expirar. Se o token de renovação expirar ou for revogado, autorize novamente em **Configurações → Credenciais de nuvem**.

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
