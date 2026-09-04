---
sidebar_position: 2
title: Instalar e usar o Skill do IaC Code
description: Adicione o IaC Code a um agente compatível com Skills para gerenciar infraestrutura Alibaba Cloud.
---

# Instalar e usar o Skill do IaC Code

O Skill do IaC Code permite que um agente compatível delegue ao IaC Code o planejamento de arquiteturas em nuvem,
a geração ou revisão de templates ROS e Terraform, estimativas de custo, seleção de recursos, operações de stacks ROS
e implantações. O pacote inclui um Runtime verificado do IaC Code; não é necessário instalar o IaC Code separadamente.

## Download

[Baixar o iac-code-skill.zip estável mais recente](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

Essa URL fixa sempre aponta para a versão estável mais recente. Instaladores automáticos podem ler
[latest.json](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/latest.json)
para obter versão, URL imutável, tamanho e SHA-256, e verificar `skill.url` com `skill.sha256`.

## Instalação

Confirme que o agente aceita Skills locais definidos por `SKILL.md`, que o CPython 3.8 a 3.14 está disponível e que o
ambiente acessa o download no primeiro uso. Use `python3` no macOS/Linux e `py -3` no Windows. Os Runtimes oficiais
oferecem suporte a macOS Apple Silicon, Linux x86_64 e Windows x86_64; o sistema e a ABI são verificados antes do
download.

Extraia o ZIP no diretório de Skills indicado pelo agente. O arquivo já contém `iac-code/`:

```text
<Raiz de Skills do agente>/
└── iac-code/
    ├── SKILL.md
    ├── agents/openai.yaml
    └── scripts/iac_code.py
```

Locais comuns:

- **Codex**: `~/.agents/skills/iac-code/` para todos os projetos ou
  `<repositório>/.agents/skills/iac-code/` para um repositório. Consulte a
  [documentação de Codex Skills](https://developers.openai.com/codex/skills#where-codex-loads-local-skills).
- **Claude Code**: `~/.claude/skills/iac-code/` para todos os projetos ou
  `<repositório>/.claude/skills/iac-code/` para um repositório. Consulte a
  [documentação de Claude Code Skills](https://code.claude.com/docs/en/skills#where-skills-live).

Reinicie o agente ou abra uma nova sessão. Para verificar o Runtime no diretório `iac-code`:

```bash
python3 scripts/iac_code.py ensure-runtime
```

No Windows PowerShell, use `py -3 scripts\iac_code.py ensure-runtime`. No primeiro uso, o Runtime correto é baixado e
seu tamanho e SHA-256 são verificados; tarefas posteriores reutilizam a cópia local validada.

## Configurar o modelo e a identidade Alibaba Cloud

O Skill usa `~/.iac-code/` por padrão e reutiliza as configurações do REPL e dos aplicativos Web ou Desktop. Escolha
outro diretório com `IAC_CODE_CONFIG_DIR`. Em automações, injete configurações do modelo e credenciais do Alibaba Cloud
por um gerenciador de segredos. Não as grave em `SKILL.md`, prompts, arquivos do projeto ou histórico do shell. Prefira
credenciais temporárias, funções RAM ou OAuth com privilégios mínimos. Consulte
[Provedores LLM](../configuration/llm-providers.md) e
[Credenciais do Alibaba Cloud](../configuration/alibaba-cloud-credentials.md).

## Escolher o modo de trabalho

- O **modo normal** é o padrão para consultar ou alterar recursos, trabalhar com templates, solucionar problemas e
  implantar um objetivo claro.
- O **modo Pipeline** é usado quando solicitado ou quando é necessário um fluxo guiado com arquiteturas candidatas,
  comparação de custos, confirmação e implantação.

Normalmente, basta descrever o resultado. Mencione Pipeline apenas quando quiser comparar soluções.

## Primeiro uso

Abra uma nova sessão no agente host e escreva, por exemplo:

```text
Use o iac-code para revisar o template ROS deste projeto. Liste riscos de segurança e melhorias sem alterar o arquivo.
```

Selecione o Skill explicitamente com `$iac-code` no Codex ou `/iac-code` no Claude Code. A verificação da configuração e a inicialização do
Runtime são automáticas; não é preciso iniciar um A2A Server manualmente. O IaC Code pode pausar para solicitar:

- aprovação ou recusa de uma operação (`permission`);
- resposta a uma pergunta (`ask_user_question`);
- escolha de uma arquitetura (`candidate_selection`);
- revisão da solução, preço e parâmetros, seguida de confirmação, ajuste, nova seleção ou cancelamento
  (`deployment_confirmation`).

Revise recursos, região, impacto e preço antes de responder. O pedido inicial de implantação não aprova antecipadamente
a confirmação posterior. Após a conclusão, continue na mesma sessão para preservar o contexto. Progresso e perguntas
podem ser retornados em inglês, chinês simplificado, espanhol, francês, alemão, japonês e português.

## Atualizar e desinstalar

Para atualizar, baixe novamente o ZIP estável, substitua todo o diretório `iac-code/` e reinicie o agente. Não substitua
apenas o script de ponte nem edite a URL ou o hash do Runtime. Para desinstalar, remova `iac-code/`. Os Runtimes
permanecem em cache; para removê-los também, consulte `cache list` e depois execute `cache clean ... --confirm`.

## Solução de problemas

- `llm_not_configured`: conclua a configuração do modelo.
- `cloud_credentials_not_configured`: configure as credenciais exigidas pelo Pipeline. O modo normal pode continuar
  tarefas sem API de nuvem com um aviso.
- `incompatible_host`: execute `ensure-runtime` e verifique Python, sistema, arquitetura, rede e proxy. Atualize ou mude
  o host, em vez de contornar a verificação.
- Tarefa pausada: ela aguarda uma resposta, permissão, seleção ou confirmação. Se a sessão ainda existir após uma
  interrupção, solicite que o agente continue a mesma tarefa.

Use `python3 scripts/iac_code.py cache list` para inspecionar o cache,
`cache clean --runtime-tag <tag> --confirm` para remover uma versão antiga e
`cache clean --candidates --confirm` para pacotes candidatos. O Runtime atual ou ativo é protegido.

## Segurança

- O Runtime escuta apenas em uma porta aleatória de `127.0.0.1` e usa um Bearer token novo por processo.
- Os resultados permanecem no workspace, em `.iac-code-skill-results/` quando aplicável.
- Estados de prontidão e resumos de permissão não incluem valores de credenciais.

## Documentação relacionada

- [Visão geral dos Skills oficiais do IaC Code](./skill-overview.md)
- [Referência de integração do Skill do IaC Code para hosts](./skill-host-integration.md)
- [Visão geral do A2A](./overview.md)
- [Configuração do Runtime](../configuration/runtime-configuration.md)
