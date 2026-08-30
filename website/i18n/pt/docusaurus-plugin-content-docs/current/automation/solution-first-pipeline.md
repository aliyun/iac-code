---
title: Pipeline com solução primeiro
description: Escolha uma arquitetura antes de gerar e implantar seu modelo ROS.
---

# Pipeline com solução primeiro

`selling_solution_first` é um pipeline de compra do Alibaba Cloud que permite comparar arquiteturas antes que o IaC Code gere um modelo ROS. Somente a solução escolhida é implementada e orçada, reduzindo o trabalho com candidatos que não serão implantados.

O pipeline `selling` continua disponível e permanece como padrão. O novo pipeline é uma alternativa selecionada explicitamente e não altera sessões `selling` existentes.

## Quando usar

Use `selling_solution_first` quando quiser:

- comparar arquiteturas, produtos, custos, vantagens e riscos antes da implementação;
- esclarecer região, escala, rede, disponibilidade ou orçamento antes de definir um modelo;
- gerar, visualizar e orçar somente a arquitetura escolhida;
- revisar os parâmetros ROS finais e a cotação exata antes de criar recursos de nuvem.

| Pipeline | Ordem do trabalho |
|---|---|
| `selling` | Gera e avalia modelos candidatos, permite escolher um e depois o implanta. |
| `selling_solution_first` | Planeja e permite escolher uma arquitetura, implementa apenas essa escolha e depois a implanta. |

## Iniciar o pipeline

No terminal interativo:

```bash
IAC_CODE_MODE=pipeline \
IAC_CODE_PIPELINE_NAME=selling_solution_first \
iac-code
```

No aplicativo Web local, selecione o modo Pipeline ao criar a conversa e inicie o servidor com o nome do pipeline:

```bash
IAC_CODE_PIPELINE_NAME=selling_solution_first iac-code web
```

Com A2A, o chamador pode selecionar o modo e o pipeline em cada mensagem sem alterar o padrão do servidor:

```json
{
  "metadata": {
    "iac_code": {
      "run_mode": "pipeline",
      "pipeline_name": "selling_solution_first",
      "preferredLanguage": "pt",
      "candidatePresentation": "rich-v1"
    }
  }
}
```

`pipeline_name` aceita `selling` e `selling_solution_first`. Um valor não vazio e sem suporte é rejeitado, em vez de executar silenciosamente outro pipeline. Para continuar um pipeline salvo, reutilize o mesmo `contextId` A2A; a identidade armazenada no snapshot durável é a fonte autorizada.

## As três etapas

### 1. Planejar e escolher uma solução

Primeiro, o IaC Code verifica se a solicitação é uma tarefa de infraestrutura do Alibaba Cloud compatível. Ele pode fazer perguntas específicas quando informações ausentes alterariam significativamente os produtos, a topologia ou o preço.

Em seguida, apresenta de uma a três soluções comparáveis. Uma solução pode incluir:

- diagrama de arquitetura e topologia;
- produtos do Alibaba Cloud e inventário de recursos;
- especificações recomendadas e restrições obrigatórias;
- cenários aplicáveis e problemas resolvidos;
- estimativa mensal aproximada para comparação;
- vantagens, desvantagens, riscos e justificativa da recomendação.

Você pode escolher uma solução, ajustar o requisito e gerar um novo conjunto ou cancelar. Nenhum modelo ROS nem recurso de nuvem é criado nesta etapa.

### 2. Implementar a solução selecionada

O IaC Code trabalha somente na solução escolhida. Ele gera e grava o modelo ROS, valida o modelo, resolve parâmetros obrigatórios, executa `PreviewStack` e solicita uma estimativa precisa de preço do ROS.

Antes da implantação, a interface mostra a arquitetura final, os parâmetros do modelo e a cotação. Você pode:

- confirmar a implantação;
- alterar parâmetros permitidos e recalcular;
- voltar à primeira etapa para escolher ou planejar outra solução;
- cancelar sem criar recursos de nuvem.

A estimativa aproximada da etapa 1 e a cotação precisa do ROS da etapa 2 são valores diferentes. A confirmação da implantação usa a cotação precisa e os parâmetros atuais do modelo.

### 3. Implantar

Após a confirmação, o IaC Code cria a pilha ROS, transmite o progresso oficial da pilha, aguarda o estado terminal e registra o ID e as saídas. Falhas de implantação permanecem disponíveis para diagnóstico e recuperação.

## Confirmação da implantação e permissão da ferramenta

A confirmação da implantação e a permissão da ferramenta são dois limites de segurança separados:

1. **Confirmação da implantação** significa que você aceita a solução, os parâmetros e o custo cotado.
2. **Permissão da ferramenta** autoriza, para esta execução, uma chamada concreta que modifica a nuvem, como `ros:CreateStack` ou `vpc:CreateVpc`.

Aprovar a primeira não aprova automaticamente a segunda. Quando uma ferramenta exige permissão, o IaC Code pausa naquele ponto e apresenta uma solicitação segura. Operações de leitura, alteração e exclusão são diferenciadas. Os detalhes da API podem incluir produto, API, região, sequência de chamadas e parâmetros ocultados; credenciais, tokens, assinaturas e outros valores confidenciais nunca aparecem nos campos de exibição.

O usuário pode escolher **Permitir uma vez** ou **Negar**. A decisão é correlacionada à solicitação exata e gravada no log de auditoria. Se o registro de auditoria necessário não puder ser persistido, uma permissão falha de forma segura.

## Pausa, recuperação e transição

A seleção, as perguntas, a confirmação da implantação e as permissões são esperas recuperáveis. O IaC Code persiste um snapshot antes de depender da continuação pelo chamador. Após reiniciar o processo ou recarregar a conversa, a interface reconstrói as etapas concluídas e restaura cada entrada pendente em sua posição original.

Para integrações A2A:

- os eventos `permission_requested` e `permission_resolved` preservam a etapa proprietária e as coordenadas do candidato;
- `pendingPermissions` expõe solicitações não resolvidas em um snapshot restaurado;
- uma resposta de permissão pelo canal lateral retoma a tarefa e o contexto originais;
- repetir a mesma decisão é idempotente, enquanto uma decisão conflitante é rejeitada.

Quando o pipeline termina, falha, sai antecipadamente ou é cancelado, ele transfere o mesmo contexto para o chat normal. As solicitações seguintes podem usar a solução escolhida, o modelo gerado, o resultado da implantação e o estado da limpeza sem iniciar outra conversa.

## Interfaces e idiomas

O pipeline funciona no terminal interativo, aplicativo Web local, contêiner Web do Desktop, modo de processo SDK e modo de servidor A2A. Os recursos de apresentação variam — por exemplo, A2A pode solicitar candidatos estruturados `rich-v1` —, mas o estado e os limites de segurança são compartilhados.

O texto visível oferece suporte a inglês, chinês simplificado, espanhol, francês, alemão, japonês e português. Chamadores A2A escolhem o idioma de uma solicitação com `metadata.iac_code.preferredLanguage`; nomes de campos, valores de enumeração, IDs e estruturas JSON não são traduzidos.

## Documentação relacionada

- [Modo Pipeline](./pipeline-mode.md)
- [Aplicativo Web](../web-app.md)
- [Referência do protocolo A2A](../a2a/protocol-reference.md)
- [Credenciais do Alibaba Cloud](../configuration/alibaba-cloud-credentials.md)
