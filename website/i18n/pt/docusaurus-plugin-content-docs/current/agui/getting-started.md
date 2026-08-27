---
sidebar_position: 2
title: Primeiros passos
description: Instale, inicie e chame o adaptador AG-UI do iac-code.
---

# Primeiros passos com AG-UI

## Pré-requisitos

1. Python 3.10 ou posterior está instalado.
2. Um provedor de LLM está configurado para o iac-code. Consulte [Autenticação](../configuration/authentication.md).
3. Se a tarefa acessar o Alibaba Cloud, configure credenciais de nuvem ou forneça credenciais temporárias em cada solicitação.
4. Há um caminho absoluto de workspace disponível para leitura e gravação pelo iac-code.

Instale as dependências AG-UI:

```bash
pip install "iac-code[agui]"
```

Para desenvolver a partir do repositório-fonte:

```bash
uv sync --extra agui
```

## Opção 1: iniciar um núcleo A2A local gerenciado

Para a configuração local mais simples, omita `--a2a-url`:

```bash
iac-code agui --host 127.0.0.1 --port 41243
```

O adaptador escolhe uma porta de loopback disponível, inicia um processo filho `iac-code a2a` gerenciado e o encerra quando o adaptador termina. O filho herda a configuração e o ambiente de runtime atuais do iac-code.

Esse modo é adequado para desenvolvimento local e gerenciamento conjunto do ciclo de vida. Use a opção seguinte quando o supervisor de produção precisar gerenciar os dois serviços separadamente.

## Opção 2: conectar a um núcleo A2A independente

Primeiro, inicie o servidor A2A:

```bash
iac-code a2a --host 127.0.0.1 --port 41242 --thinking-exposure all
```

Depois, inicie o adaptador AG-UI:

```bash
iac-code agui \
  --host 0.0.0.0 \
  --port 41243 \
  --a2a-url http://127.0.0.1:41242
```

Os serviços mantêm responsabilidades e portas separadas. O A2A pode continuar atendendo clientes A2A, enquanto o adaptador o acessa apenas pela interface de loopback.

`--thinking-exposure all` permite converter o raciocínio bruto em eventos padrão `REASONING_*`. Habilite-o somente para clientes confiáveis. Mantenha o padrão A2A, `tool-trace`, quando o conteúdo de raciocínio não deve ser exposto.

Se o servidor A2A usar um token bearer:

```bash
export IACCODE_A2A_HTTP_TOKEN="segredo-a2a-local"
iac-code a2a --host 127.0.0.1 --port 41242
```

Forneça ao adaptador o mesmo token do upstream:

```bash
export IAC_CODE_AGUI_A2A_TOKEN="segredo-a2a-local"
iac-code agui --port 41243 --a2a-url http://127.0.0.1:41242
```

## Configuração YAML

Configurações estáticas de inicialização podem ser armazenadas em YAML:

```yaml title="agui-server.yml"
host: 0.0.0.0
port: 41243
a2a-url: http://127.0.0.1:41242
interrupt-ttl: 540
state-dir: /var/lib/iac-code/agui
idle-shutdown: 0
debug: false
log-stdout: true
```

Inicie o adaptador com:

```bash
iac-code agui --config agui-server.yml
```

Argumentos explícitos da CLI substituem o YAML. Injete valores confidenciais, como tokens, por variáveis de ambiente em vez de armazená-los no arquivo.

| CLI / YAML | Padrão | Significado |
|------------|--------|-------------|
| `--host` / `host` | `127.0.0.1` | Endereço HTTP de escuta do AG-UI |
| `--port` / `port` | `8000` | Porta HTTP do AG-UI; os exemplos de implantação usam `41243` |
| `--a2a-url` / `a2a-url` | vazio | URL A2A local; vazio inicia um filho gerenciado |
| `--interrupt-ttl` / `interrupt-ttl` | `540` | Segundos durante os quais uma interrupção pode ser retomada |
| `--state-dir` / `state-dir` | `<config-dir>/agui` | Diretório de estado dos threads AG-UI |
| `--idle-shutdown` / `idle-shutdown` | `0` | Atraso para desligamento ocioso; `0` o desabilita |
| `--debug` / `debug` | `false` | Logs de depuração |
| `--log-stdout` / `log-stdout` | `false` | Repetir os logs em stdout |

Variáveis de ambiente relacionadas:

| Variável | Finalidade |
|----------|------------|
| `IAC_CODE_AGUI_HOST` | Endereço de escuta do AG-UI |
| `IAC_CODE_AGUI_PORT` | Porta do AG-UI |
| `IAC_CODE_AGUI_A2A_URL` | URL do upstream A2A local |
| `IAC_CODE_AGUI_A2A_TOKEN` | Token bearer do upstream A2A |
| `IAC_CODE_AGUI_AUTH_TOKEN` | Token bearer que protege o endpoint AG-UI |
| `IAC_CODE_AGUI_INTERRUPT_TTL` | Vida útil da interrupção |
| `IAC_CODE_AGUI_STATE_DIR` | Diretório de estado dos threads AG-UI |
| `IAC_CODE_AGUI_ALLOWED_CWDS` | Raízes de workspace permitidas, separadas pelo separador de caminhos do sistema operacional |
| `IAC_CODE_CONFIG_DIR` | Raiz de configuração do iac-code e diretório pai padrão do estado AG-UI |

## Verificação de integridade

```bash
curl http://127.0.0.1:41243/health
```

Exemplo de resposta:

```json
{
  "status": "ok",
  "protocol": "ag-ui",
  "protocolPackageVersion": "0.1.20",
  "executionKernel": "a2a-1.0",
  "serverVersion": "versão atual do iac-code"
}
```

## Usar o cliente JavaScript oficial

Instale a versão de cliente verificada:

```bash
pnpm add @ag-ui/client@0.0.58
```

Este exemplo se conecta diretamente a `iac-code agui`, usa o `HttpAgent` padrão e fornece as propriedades de runtime em `forwardedProps`:

```javascript
import { HttpAgent, randomUUID } from "@ag-ui/client";

const threadId = randomUUID();
const rosInvocationId = randomUUID();
const agent = new HttpAgent({
  url: "http://127.0.0.1:41243/",
  threadId,
  // Quando IAC_CODE_AGUI_AUTH_TOKEN estiver configurado:
  // headers: { Authorization: `Bearer ${process.env.AG_UI_TOKEN}` },
});

const forwardedProps = {
  iacCode: {
    schemaVersion: 1,
    rosInvocationId,
    cwd: process.cwd(),
    runMode: "normal",
    preferredLanguage: "pt",
  },
};

agent.addMessage({
  id: randomUUID(),
  role: "user",
  content: "Crie um modelo de VPC com dois vSwitches.",
});

const subscriber = {
  onTextMessageContentEvent({ event }) {
    process.stdout.write(event.delta);
  },
  onToolCallStartEvent({ event }) {
    console.log(`\n[ferramenta] ${event.toolCallName}`);
  },
  onStepStartedEvent({ event }) {
    console.log(`\n[etapa] ${event.stepName}`);
  },
  onRunErrorEvent({ event }) {
    console.error(`\n${event.code}: ${event.message}`);
  },
};

await agent.runAgent({ forwardedProps }, subscriber);
```

Quando houver token bearer, passe `Authorization` em `HttpAgent.headers`. Uma aplicação web normalmente se conecta por um backend de mesma origem ou proxy reverso; o adaptador não adiciona uma política CORS.

## Tratar interrupções

O cliente oficial mantém `RUN_FINISHED.outcome.interrupts` em `agent.pendingInterrupts`. Construa cada resposta a partir de seu `responseSchema` e envie-a em uma nova execução:

```javascript
const responses = agent.pendingInterrupts.map((interrupt) => ({
  interruptId: interrupt.id,
  status: "resolved",
  payload: { decision: "allow_once" },
}));

await agent.runAgent({ forwardedProps, resume: responses }, subscriber);
```

Esse payload se aplica apenas a interrupções de permissão cujo esquema exige `decision`. Perguntas e seleção de opções têm esquemas próprios.

Uma retomada deve usar o `threadId` original, um novo `runId`, manter o `rosInvocationId` da execução interrompida, responder a todas as interrupções pendentes em uma única solicitação e fornecer um payload compatível com cada `responseSchema`. Use `status: "cancelled"` quando o usuário não quiser continuar.

## Iniciar um Pipeline

Defina `runMode` como `pipeline` e, opcionalmente, selecione um Pipeline:

```javascript
const forwardedProps = {
  iacCode: {
    schemaVersion: 1,
    rosInvocationId: randomUUID(),
    cwd: process.cwd(),
    runMode: "pipeline",
    pipelineName: "selling",
    candidatePresentation: "rich",
  },
};
```

Clientes devem tratar `STEP_*`, `TOOL_CALL_*`, `ACTIVITY_SNAPSHOT` e `CUSTOM`. Um cliente genérico que não reconheça eventos personalizados do iac-code ainda processa normalmente todos os eventos padrão.

## Workspace e credenciais temporárias

`cwd` não é fixado na inicialização do servidor. Cada solicitação deve fornecer um caminho absoluto sob uma raiz permitida por `IAC_CODE_AGUI_ALLOWED_CWDS` ou `IACCODE_A2A_ALLOWED_CWDS`.

O chamador pode fornecer, por solicitação, um modelo, uma chave de LLM e credenciais temporárias do Alibaba Cloud por `forwardedProps.iacCode`. O adaptador não grava esses segredos no estado do thread; ele os encaminha ao núcleo A2A conforme as regras normais de substituição de solicitação.

## Diretório de estado

Estrutura padrão:

```text
<IAC_CODE_CONFIG_DIR>/agui/
  threads/
    <threadId>.json
```

Cada thread é gravado independentemente, e a inicialização não percorre threads históricos. UUIDs normais continuam legíveis. IDs inseguros são codificados, e IDs muito longos usam uma chave de arquivo de tamanho fixo. O documento JSON sempre armazena e valida o `threadId` original.

Esse diretório armazena apenas mapeamentos, interrupções e estado de idempotência do adaptador. Ele não contém conversas nem credenciais de solicitações. Não edite os arquivos JSON manualmente.

## Próximos passos

- [Visão geral do AG-UI](./overview.md)
- [Referência do protocolo](./protocol-reference.md)
