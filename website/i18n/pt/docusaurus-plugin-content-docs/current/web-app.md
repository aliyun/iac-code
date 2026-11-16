---
title: Aplicativo web
description: Execute o IaC Code como um aplicativo web local com o mesmo motor da CLI.
---

# Aplicativo web

Execute `iac-code` em um terminal para iniciar o aplicativo web local e abrir o navegador. No primeiro uso, configure o provedor LLM e as credenciais da Alibaba Cloud nas configurações.

O aplicativo web lê e grava no mesmo armazenamento de sessões da CLI, então uma conversa iniciada de um lado pode ser retomada no outro.

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
| `--host` | `127.0.0.1` | Host do servidor HTTP. Apenas endereços de loopback são aceitos. |
| `--port` | `8766` | Porta do servidor HTTP. |
| `--open` / `--no-open` | `--open` | Abre o navegador ao iniciar. Use `--no-open` para desativar. |

```bash
iac-code web --port 9000 --no-open
```

### Segurança

O servidor web vincula-se apenas a interfaces de loopback (`127.0.0.1`, `localhost` ou `::1`). Ele foi projetado para uso na sua própria máquina e rejeita endereços de vínculo públicos. Não o exponha diretamente em uma rede; coloque-o atrás do seu próprio proxy autenticado caso precise de acesso remoto.

## Visão geral da interface

### Barra lateral de sessões

A barra lateral lista as conversas do projeto selecionado. A partir daqui, você pode:

- Iniciar um **novo chat** ou trocar de projeto com o seletor de projetos.
- **Pesquisar** conversas ou abrir a paleta de comandos para executar um comando.
- **Fixar**, **renomear** ou **arquivar** uma conversa e navegar pelas conversas arquivadas.

Como as sessões são compartilhadas com a CLI, uma conversa que você retoma com `iac-code tui --resume` também aparece aqui. Consulte [Sessões](./cli/sessions.md) para entender como o armazenamento de sessões funciona.

### Área de composição (composer)

A área de composição é onde você escreve suas solicitações. Ela oferece os mesmos controles que a CLI expõe por meio de comandos de barra e sinalizadores:

- A seleção de **modelo e provedor** para a sessão ativa.
- Um botão de **Pensamento** para ativar ou desativar o raciocínio estendido em modelos compatíveis.
- Um controle de **modo de permissão** que define como as ações das ferramentas são aprovadas.
- **Anexos de imagem** para modelos multimodais.
- **Comandos de barra** (digitados com `/`) e **referências de arquivo `@`** para apontar arquivos do seu espaço de trabalho.

### Chat normal e modo pipeline

Uma sessão é executada como chat normal ou no modo **pipeline**. O chat normal transmite em linha as respostas do assistente, as chamadas de ferramentas e os resultados. O modo pipeline adiciona um espaço de trabalho que exibe as linhas do tempo das etapas, os diagnósticos, os diagramas, o progresso da implantação, a limpeza e os detalhes de transferência conforme o pipeline é executado. Consulte [Modo pipeline](./automation/pipeline-mode.md) para saber o que os pipelines fazem.

O [pipeline `selling_solution_first`](./automation/solution-first-pipeline.md) usa esse espaço de trabalho para um fluxo de compra em três etapas: comparar arquiteturas candidatas, implementar a solução selecionada e implantá-la após a confirmação. As aprovações de ferramentas aparecem como cartões de permissão localizados sob a etapa que as solicitou; as aprovações pendentes retornam à mesma etapa quando a sessão é restaurada.

### Ferramentas e aprovações

As chamadas de ferramentas são exibidas como cartões na transcrição. Quando uma ferramenta exige sua aprovação, uma solicitação de aprovação aparece em linha; o modo de permissão definido na área de composição determina quando você é consultado.

### Configurações

A área de configurações reúne a mesma configuração gerenciada pela CLI:

- **Credenciais de nuvem** para o Alibaba Cloud (consulte [Credenciais do Alibaba Cloud](./configuration/alibaba-cloud-credentials.md)).
- **Modelos** e configuração de provedores (consulte [Provedores de LLM](./configuration/llm-providers.md)).
- **Plugins MCP** (consulte [Integração MCP](./mcp/overview.md)).
- Inspeção e gerenciamento da **memória**.

### Idioma da interface

O aplicativo web está disponível em sete idiomas —English, 简体中文, 日本語, Français, Deutsch, Español e Português— selecionáveis nas configurações. Sua escolha é salva para sessões futuras.

## Relação com a CLI

O aplicativo web é uma interface alternativa, não um produto separado. Ele usa os mesmos provedores, credenciais, habilidades, ferramentas e armazenamento de sessões do terminal. Configure os provedores e as credenciais uma única vez com `/auth` na CLI, ou pelas configurações do aplicativo web, e ambas as interfaces os compartilharão.
