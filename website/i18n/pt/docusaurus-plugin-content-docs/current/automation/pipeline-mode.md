---
title: Modo pipeline
description: Use o modo pipeline, executado passo a passo, para orientar tarefas complexas de infraestrutura.
---

# Modo pipeline

O modo pipeline é um modo interativo que executa o trabalho passo a passo. Ele é útil para tarefas de infraestrutura mais longas ou mais sujeitas a erro do que uma solicitação normal de chat: entender o requisito, planejar uma abordagem, gerar artefatos, pedir confirmação ao usuário e continuar com as próximas ações.

Pipeline em si é uma capacidade geral. A implementação integrada disponível hoje é o pipeline `selling`. `selling` é voltado para cenários de infraestrutura da Alibaba Cloud e pode conduzir uma solicitação de implantação por arquiteturas candidatas, modelos ROS, estimativas de custo e implantação após confirmação.

Bons exemplos de solicitação para o modo pipeline incluem:

```text
Selecionar uma VPC existente e criar um VSwitch
```

```text
Projetar uma implantação de baixo custo para uma aplicação web na Alibaba Cloud e gerar um modelo
```

## Iniciar o modo pipeline

Atualmente o modo pipeline requer o REPL interativo. Ele não pode ser combinado com `--prompt`.

No macOS ou Linux:

```bash
IAC_CODE_MODE=pipeline iac-code
```

No PowerShell:

```powershell
$env:IAC_CODE_MODE = "pipeline"
iac-code
```

O nome padrão do pipeline é `selling`. Para deixar explícito:

```bash
IAC_CODE_MODE=pipeline IAC_CODE_PIPELINE_NAME=selling iac-code
```

## Relação entre Pipeline e selling

| Nome | Significado |
|---|---|
| Modo pipeline | Modo geral de execução passo a passo do IaC Code para fluxos longos, pontos de confirmação, recuperação e exibição de progresso. |
| Pipeline `selling` | Pipeline integrado atual para design de infraestrutura da Alibaba Cloud, geração de modelos, estimativa de custos e implantação. |

Se mais pipelines forem adicionados no futuro, eles poderão ser selecionados com `IAC_CODE_PIPELINE_NAME`. A versão atual inclui `selling`.

## Variáveis de ambiente

| Variável | Finalidade |
|---|---|
| `IAC_CODE_MODE=pipeline` | Ativa o modo pipeline. Qualquer outro valor volta para o modo normal. |
| `IAC_CODE_PIPELINE_NAME` | Seleciona a definição de pipeline. O padrão é `selling`. |
| `IAC_CODE_CWD` | Substitui o diretório de trabalho usado pelo pipeline. |
| `IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING` | Ativa a etapa opcional de revisão do modelo no pipeline `selling`. |

## O que acontece no pipeline selling

O pipeline `selling` divide uma solicitação de infraestrutura em etapas compreensíveis para o usuário:

| Etapa | O que você verá |
|---|---|
| Entender o requisito | O IaC Code verifica se a solicitação é uma tarefa de infraestrutura da Alibaba Cloud. Se faltarem detalhes importantes, ele pergunta antes de gerar um plano. |
| Planejar arquiteturas | O IaC Code propõe uma ou mais arquiteturas candidatas para que você possa comparar alternativas. |
| Gerar e avaliar | O IaC Code gera modelos ROS para os planos candidatos e estima os custos dos recursos. |
| Confirmar um plano | O IaC Code mostra detalhes dos candidatos e espera você escolher o plano para continuar. |
| Implantar | Depois que um plano é selecionado, o IaC Code entra na etapa de implantação e trata ferramentas ou operações de maior risco de acordo com a política de permissões. |

Se você mencionar restrições como “usar uma VPC existente” ou “não criar este tipo de recurso”, o pipeline `selling` tentará respeitá-las nos planos e modelos seguintes. Você não precisa conhecer campos internos; basta escrever as restrições na solicitação.

## Interação e recuperação

O modo pipeline pode pausar e aguardar entrada do usuário, por exemplo:

- O requisito não está claro e o IaC Code precisa de objetivo, escala, região ou orçamento.
- Há vários planos candidatos e você precisa escolher um.
- Uma ferramenta ou ação de implantação exige aprovação de permissões.
- A execução foi interrompida e precisa ser recuperada ou continuada.

Se o processo terminar ou a sessão for interrompida, o IaC Code salva o estado do pipeline. Quando você voltar mais tarde para essa sessão com `--resume`, poderá verificar o progresso anterior e continuar a partir de um ponto recuperável.

Depois que o pipeline é concluído, falha, sai antecipadamente ou é cancelado, o IaC Code volta para o chat normal. Então você pode fazer perguntas de acompanhamento, ajustar o plano ou lidar com problemas pós-implantação.

## Integrações de automação

Atualmente o modo pipeline é voltado principalmente para o REPL interativo. O modo servidor A2A pode expor progresso do pipeline, artefatos, resultados de permissão e informações de recuperação, o que é útil para conectar um pipeline a um console externo ou sistema de tarefas.

ACP atualmente não oferece suporte ao modo pipeline. `--prompt` / o [modo não interativo](./non-interactive-mode.md) executa uma solicitação normal de uma só vez e não executa etapas de Pipeline.

## Limitações atuais

- A versão atual inclui apenas o pipeline `selling`, principalmente para fluxos de infraestrutura da Alibaba Cloud.
- O modo pipeline requer o REPL interativo. `--prompt` é rejeitado quando `IAC_CODE_MODE=pipeline`.
- O modo pipeline aceita entrada de texto. Imagens coladas no REPL são ignoradas enquanto o pipeline está ativo.
- Durante o pipeline, shell escapes, gatilhos de skills e a maioria dos slash commands são restritos, a menos que a definição do pipeline os permita explicitamente. Comandos básicos como `/help`, `/status`, `/resume` e `/exit` continuam disponíveis.
