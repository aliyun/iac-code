---
title: Aplicativo web
description: Iniciar o aplicativo Web local e usar configurações, conversas e controles de entrada.
---

# Aplicativo web

Execute `iac-code` em um terminal para iniciar o aplicativo web local e abrir o navegador. No primeiro uso, configure o provedor LLM e as credenciais da Alibaba Cloud nas configurações.

Desktop e Web compartilham conversas e configurações. Conversas criadas por outras interfaces podem ser de somente leitura; veja [Sessões](./cli/sessions.md) sobre exibição e continuação.

## Instalação

A instalação padrão inclui as dependências do aplicativo web e do ambiente HTTP. O extra `[http]` permanece como um alias de compatibilidade; não é mais necessário instalá-lo separadamente. No repositório, basta executar `uv sync`.

```bash
pip install iac-code
```

## Iniciar o aplicativo web

Inicie o servidor pelo terminal:

```bash
iac-code web
```

Por padrão, ele se vincula a `127.0.0.1:8766` e abre o seu navegador padrão em `http://127.0.0.1:8766`.

| Opção | Padrão | Descrição |
|---|---|---|
| `--host` | `127.0.0.1` | Host do servidor HTTP. O modo local aceita endereços de loopback; outras IPs exigem `--access-token-file`. |
| `--port` | `8766` | Porta do servidor HTTP. |
| `--open` / `--no-open` | `--open` | Abre o navegador ao iniciar. Use `--no-open` para desativar. |
| `--debug`, `-d` | `false` | Ativar os registros de depuração do servidor nesta execução. |
| `--access-token-file <path>` | — | Arquivo de token para acesso remoto explícito com transporte de API criptografado. |

```bash
iac-code web --port 9000 --no-open
```

### Segurança

Sem `--access-token-file`, o servidor aceita apenas hosts de loopback (`127.0.0.1`, `localhost` ou `::1`) e verifica as solicitações locais. O modo explícito de token também aceita outras IPs e protege as solicitações de API pelo transporte de token criptografado.

## Implantar um modelo ROS existente

1. Configure as [credenciais de nuvem](./configuration/alibaba-cloud-credentials.md) e abra uma conversa no projeto do modelo.

2. Digite `@`, selecione o arquivo e peça uma verificação do modelo e dos parâmetros necessários.

3. Informe região, nome da pilha e valores dos parâmetros; peça validação antes da criação.

4. Confira o resultado e as mudanças propostas e depois solicite a criação da pilha.

5. Se aparecer um cartão de permissão de API de nuvem, confira a operação e aprove ou negue. Uma criação aprovada pode gerar recursos cobrados.

6. Veja o ID da pilha, progresso e resultado. Em caso de falha, peça o diagnóstico do erro na mesma conversa.

## Visão geral da interface

### Barra lateral de sessões

A barra lateral agrupa as conversas salvas por projeto. Você pode:

- Iniciar uma **Nova conversa** e escolher o projeto abaixo da entrada.
- **Pesquisar** conversas ou abrir a paleta de comandos para executar um comando.
- **Fixar**, **renomear** ou **arquivar** uma conversa e navegar pelas conversas arquivadas.

Web, Desktop e headless compartilham as conversas salvas. Consulte [Sessões](./cli/sessions.md) para retomar ou renomear uma conversa.

### Área de composição (composer)

Descreva a tarefa na entrada e use os controles da interface para escolher como a conversa funciona:

- A seleção de **modelo e provedor** para a sessão ativa.
- Um botão de **Pensamento** para ativar ou desativar o raciocínio estendido em modelos compatíveis.
- Um controle de **modo de permissão** que define como as ações das ferramentas são aprovadas.
- **Anexos de imagem** para modelos multimodais.
- **Habilidades** selecionados nas sugestões `/` ou `$` e **referências `@` a arquivos** do workspace.

### Chat normal e modo pipeline

Escolha o modo ao criar uma conversa e também o pipeline se selecionar o modo pipeline. As conversas no mesmo aplicativo podem usar modos diferentes sem reiniciar o servidor.

Uma sessão é executada como chat normal ou no modo **pipeline**. O chat normal transmite em linha as respostas do assistente, as chamadas de ferramentas e os resultados. O modo pipeline adiciona um espaço de trabalho que exibe as linhas do tempo das etapas, os diagnósticos, os diagramas, o progresso da implantação, a limpeza e os detalhes de transferência conforme o pipeline é executado. Consulte [Modo pipeline](./automation/pipeline-mode.md) para saber o que os pipelines fazem.

O [pipeline `selling_solution_first`](./automation/solution-first-pipeline.md) usa esse espaço de trabalho para um fluxo de compra em três etapas: comparar arquiteturas candidatas, implementar a solução selecionada e implantá-la após a confirmação. As aprovações de ferramentas aparecem como cartões de permissão localizados sob a etapa que as solicitou; as aprovações pendentes retornam à mesma etapa quando a sessão é restaurada.

### Ferramentas e aprovações

As chamadas de ferramentas são exibidas como cartões na transcrição. Quando uma ferramenta exige sua aprovação, uma solicitação de aprovação aparece em linha; o modo de permissão definido na área de composição determina quando você é consultado.

### Configurações

Desktop e Web compartilham a mesma interface. Use as configurações e os controles visíveis no trabalho diário.

- **Credenciais de nuvem** para o Alibaba Cloud (consulte [Credenciais do Alibaba Cloud](./configuration/alibaba-cloud-credentials.md)).
- **Modelos** e configuração de provedores (consulte [Provedores de LLM](./configuration/llm-providers.md)).
- **Plugins MCP** (consulte [Integração MCP](./mcp/overview.md)).
- Inspeção e gerenciamento da **memória**.

### Idioma da interface

O aplicativo web está disponível em sete idiomas —English, 简体中文, 日本語, Français, Deutsch, Español e Português— selecionáveis nas configurações. Sua escolha é salva para sessões futuras.

## Relação com a CLI

Web, Desktop e headless compartilham provedores, credenciais, skills, ferramentas e sessões. Configure o modelo e o acesso à nuvem uma vez em Configurações para reutilizá-los nessas entradas.
