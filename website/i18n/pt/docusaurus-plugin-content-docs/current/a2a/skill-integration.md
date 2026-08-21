---
sidebar_position: 7
title: Instalar e usar o Skill do IaC Code
description: Baixe e instale o Skill do IaC Code para que um agente externo possa gerenciar recursos do Alibaba Cloud.
---

# Instalar e usar o Skill do IaC Code

O Skill do IaC Code foi desenvolvido para agentes externos compatíveis com Skills. Depois da instalação, um agente
host pode delegar ao IaC Code o planejamento de arquiteturas de nuvem, a geração e revisão de templates ROS ou
Terraform, a estimativa de custos, a seleção de recursos, as operações com stacks e o deploy. O Skill usa uma ponte
escrita apenas com a biblioteca padrão do Python para iniciar um Runtime A2A local e autenticado. Não é necessário
instalar o IaC Code com pip, e o host não deve recorrer a comandos headless.

## Baixar o Skill

### Versão estável mais recente

Baixe diretamente a versão estável mais recente:

[Baixar iac-code-skill.zip](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

Essa URL fixa sempre aponta para o pacote do Skill promovido ao canal estável. Ela é adequada para downloads pelo
navegador e instalações manuais e não muda quando uma nova versão é publicada.

Instaladores que precisam da versão, do tamanho do arquivo, do hash SHA-256 e da URL imutável específica da versão
podem consultar os metadados do canal estável:

[Ver latest.json](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/latest.json)

O documento contém:

- `skillVersion`: versão estável atual do Skill;
- `skill.url`: URL imutável do ZIP dessa versão;
- `skill.sha256` e `skill.size`: valores usados para verificar o download;
- `manifest.url`: manifesto de release imutável dessa versão.

Para uma verificação rigorosa ou uma instalação automatizada reproduzível, leia `latest.json`, baixe `skill.url` e
verifique `skill.sha256`. Não monte manualmente uma URL com base no número da versão.

## Instalar o Skill

### Pré-requisitos

- O agente host é compatível com Skills locais definidos por `SKILL.md`.
- O CPython 3.8–3.14 está instalado. Use `python3` no macOS/Linux e, de preferência, `py -3` no Windows.
- O ambiente consegue acessar as URLs OSS acima para baixar o ZIP do Skill e o Runtime necessário no primeiro uso.
- A configuração do serviço de modelo está disponível. Para tarefas que consultam ou gerenciam recursos de nuvem,
  também é necessária uma identidade do Alibaba Cloud com o mínimo de privilégios.

Os releases oficiais do Skill Runtime são compatíveis com estas plataformas:

| Sistema operacional | Arquitetura |
|---|---|
| macOS | Apple Silicon (arm64) |
| Linux | x86_64 |
| Windows | x86_64 |

As versões mínimas do sistema operacional e da glibc no Linux são definidas pelo manifesto do Runtime fixado pelo
Skill. A ponte verifica a compatibilidade antes de baixar. Em uma plataforma não compatível, ela retorna um erro em
vez de baixar um artefato destinado a outra plataforma ou ABI.

### Extrair no diretório de Skills do agente host

Extraia o ZIP diretamente na raiz de Skills do agente host. O local exato varia de acordo com o produto; consulte a
documentação do agente host. A estrutura final deve ser:

```text
<Raiz de Skills do agente>/
└── iac-code/
    ├── SKILL.md
    ├── agents/
    │   └── openai.yaml
    └── scripts/
        └── iac_code.py
```

O ZIP já contém o diretório de nível superior `iac-code/`. Não adicione outro diretório com o mesmo nome. Depois de
instalar ou atualizar, reinicie o agente host ou abra uma nova sessão para que ele detecte o Skill novamente.

### Verificar a instalação

No diretório `iac-code` extraído, execute este comando no macOS ou Linux:

```bash
python3 scripts/iac_code.py ensure-runtime
```

No Windows PowerShell, execute:

```powershell
py -3 scripts\iac_code.py ensure-runtime
```

Na primeira execução, o comando baixa o Runtime da plataforma atual, verifica o tamanho e o hash SHA-256 e imprime um
objeto JSON com `skillVersion`, `runtimeTag` e o caminho de instalação. Um Runtime verificado que já esteja no cache é
reutilizado sem um novo download.

## Configurar o modelo e a identidade do Alibaba Cloud

O Skill Runtime usa o mesmo diretório de configuração que os outros modos do IaC Code: `~/.iac-code/` por padrão. Se
você já configurou o IaC Code pelo REPL, pelo aplicativo Web ou pelo aplicativo Desktop, o Skill pode reutilizar essas
configurações. Defina `IAC_CODE_CONFIG_DIR` para usar outro diretório de configuração.

Em ambientes automatizados, forneça estas variáveis por meio de uma solução de gerenciamento de segredos:

| Categoria | Variável de ambiente | Descrição |
|---|---|---|
| Modelo | `IAC_CODE_PROVIDER` | Provedor do modelo |
| Modelo | `IAC_CODE_MODEL` | Nome do modelo |
| Modelo | `IAC_CODE_API_KEY` | Chave de API do serviço de modelo |
| Modelo | `IAC_CODE_BASE_URL` | Substituição opcional do endpoint compatível |
| Alibaba Cloud | `ALIBABA_CLOUD_ACCESS_KEY_ID` | ID da AccessKey |
| Alibaba Cloud | `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | Segredo da AccessKey |
| Alibaba Cloud | `ALIBABA_CLOUD_SECURITY_TOKEN` | Token de segurança para credenciais STS |
| Alibaba Cloud | `ALIBABA_CLOUD_REGION_ID` | Região padrão |

Nunca coloque credenciais reais em `SKILL.md`, nos prompts do agente host, nos arquivos do projeto ou no histórico do
shell. Prefira credenciais temporárias, funções RAM ou OAuth e conceda apenas as permissões de API de nuvem necessárias
para a tarefa. Consulte [Provedores de LLM](../configuration/llm-providers.md) e
[Credenciais do Alibaba Cloud](../configuration/alibaba-cloud-credentials.md) para obter instruções completas.

## Primeiro uso

Depois da instalação e da configuração, abra uma nova sessão no agente host e descreva diretamente uma tarefa de
infraestrutura do Alibaba Cloud. Por exemplo:

```text
Use o iac-code para revisar o template ROS deste projeto. Liste os riscos de segurança e as alterações recomendadas sem modificar o arquivo.
```

Hosts compatíveis com uma sintaxe explícita de Skills podem selecionar o Skill usando `$iac-code`. O agente host lê
`SKILL.md`, grava a solicitação completa em um arquivo UTF-8 dentro do workspace e usa a ponte para criar e acompanhar
uma única tarefa. O usuário não precisa iniciar manualmente um servidor A2A.

Fluxo esperado:

1. A ponte verifica se a configuração do modelo e do Alibaba Cloud está pronta.
2. No primeiro uso, ela baixa e verifica o Runtime do IaC Code fixado pelo Skill.
3. O Runtime escuta apenas em uma porta aleatória de `127.0.0.1` e gera um token Bearer específico do processo.
4. O agente host apresenta o progresso, as perguntas, os planos candidatos e as solicitações de permissão retornados
   pelo IaC Code.
5. Quando a tarefa termina, o agente host retorna o resultado final e os arquivos gerados no workspace.

## Atualizar e desinstalar

Para fazer uma atualização manual, baixe `skill/stable/iac-code-skill.zip` novamente e substitua todo o diretório
`iac-code/` na raiz de Skills do host. Um atualizador automático pode comparar o valor `skillVersion` de `latest.json`
e, em seguida, baixar e verificar o novo pacote usando a URL imutável e o hash SHA-256. Cada Skill oficial é fixado a
um Runtime verificado. Não substitua apenas `scripts/iac_code.py` nem altere manualmente a URL ou o hash do Runtime.

Para desinstalar, remova `iac-code/` da raiz de Skills do agente host. O cache do Runtime não é removido com o
diretório do Skill. Execute `cache list` e `cache clean` somente quando o usuário solicitar explicitamente a remoção.

## Cache do Runtime

O Runtime baixado no primeiro uso é armazenado em
`<IAC_CODE_CONFIG_DIR ou ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/` e reutilizado automaticamente. No uso
normal, não é necessário gerenciar esse diretório. Para verificar o uso do disco ou remover versões antigas, use:

- `python3 scripts/iac_code.py cache list` — lista os Runtimes instalados e os pacotes candidatos;
- `python3 scripts/iac_code.py cache clean [--runtime-tag <tag>] [--candidates] --confirm` — remove caches do Runtime
  ou pacotes candidatos; `--confirm` é obrigatório.

O Runtime atual e qualquer Runtime usado por um processo ativo são protegidos contra a limpeza. O formato do pacote e
as restrições do Runtime são definidos por `skill-runtime/skill-package-contract.json` no repositório de código-fonte;
os usuários não precisam modificar esse arquivo.

## Solução de problemas

### A configuração está incompleta

O Skill verifica a configuração antes de criar uma tarefa, mas nunca lê nem retorna valores secretos:

| Situação | Resultado |
|---|---|
| O provedor de LLM ou a chave de API está incompleto | Retorna `llm_not_configured` e não cria a tarefa |
| As credenciais do Alibaba Cloud estão incompletas para o Pipeline de vendas | Retorna `cloud_credentials_not_configured` e não cria a tarefa |
| As credenciais do Alibaba Cloud estão incompletas no modo normal | Tarefas que não chamam APIs de nuvem podem continuar com um aviso prévio |

### Por que a execução é pausada

O IaC Code pausa quando precisa de permissão, informações adicionais ou da seleção de um plano. O agente host apresenta
a solicitação diretamente:

- uma solicitação de permissão para uma ferramenta ou um deploy (`permission`);
- uma pergunta de múltipla escolha ou uma solicitação de mais informações (`ask_user_question`);
- a seleção de um plano candidato do Pipeline (`candidate_selection`).

Antes de confirmar, revise o recurso de destino, a região, o impacto esperado e o preço. O agente host não pode anular
uma recusa do IaC Code. Uma aprovação única é representada no protocolo como `allow_once`.

> **Observação sobre a integração do agente host**
>
> Quando um resultado da ponte contém `inputRequired`, o agente host deve apresentar a solicitação atual e aguardar
> uma resposta. `boundaryReached` indica um limite de apresentação ou interação, e não a conclusão da tarefa; o host
> deve mostrar a atualização e continuar acompanhando a mesma tarefa.

## Segurança

- O Runtime escuta apenas em uma porta aleatória de `127.0.0.1`. Cada inicialização gera um novo token Bearer, e cada
  solicitação da ponte inclui esse token.
- A ponte mantém artefatos e resultados no workspace da tarefa. Os resultados são gravados em
  `.iac-code-skill-results/`.
- Os campos exibidos na verificação prévia e nas solicitações de permissão são higienizados; segredos e credenciais
  não aparecem nesses campos.

## Documentação relacionada

- [Visão geral do protocolo A2A](./overview.md)
- [Referência do protocolo A2A](./protocol-reference.md)
- [Provedores de LLM](../configuration/llm-providers.md)
- [Credenciais do Alibaba Cloud](../configuration/alibaba-cloud-credentials.md)
- [Configuração do Runtime](../configuration/runtime-configuration.md)
