---
sidebar_position: 2
title: Primeros pasos
description: Instalación, arranque y uso del adaptador AG-UI de iac-code.
---

# Primeros pasos con AG-UI

## Requisitos

1. Python 3.10 o posterior.
2. Un proveedor LLM configurado para iac-code. Consulte [Autenticación](../configuration/authentication.md).
3. Para acceder a Alibaba Cloud, credenciales configuradas o credenciales temporales por solicitud.
4. Una ruta absoluta de workspace que iac-code pueda leer y escribir.

Instale las dependencias AG-UI:

```bash
pip install "iac-code[agui]"
```

Desde el repositorio fuente:

```bash
uv sync --extra agui
```

## Opción 1: núcleo A2A local administrado

Omita `--a2a-url`:

```bash
iac-code agui --host 127.0.0.1 --port 41243
```

El adaptador elige un puerto loopback libre, inicia un proceso hijo `iac-code a2a` y lo detiene al salir. El hijo hereda la configuración y el entorno actuales. Es la opción más cómoda para desarrollo local.

## Opción 2: núcleo A2A independiente

Inicie A2A:

```bash
iac-code a2a --host 127.0.0.1 --port 41242 --thinking-exposure all
```

Después inicie AG-UI:

```bash
iac-code agui \
  --host 0.0.0.0 \
  --port 41243 \
  --a2a-url http://127.0.0.1:41242
```

A2A puede seguir atendiendo a sus propios clientes mientras AG-UI lo utiliza por loopback. `--thinking-exposure all` permite generar eventos `REASONING_*`; actívelo solo para clientes de confianza. Mantenga el valor predeterminado `tool-trace` si no desea exponer el razonamiento.

Con Bearer token en A2A:

```bash
export IACCODE_A2A_HTTP_TOKEN="a2a-local-secret"
iac-code a2a --host 127.0.0.1 --port 41242
```

Configure el mismo token upstream en AG-UI:

```bash
export IAC_CODE_AGUI_A2A_TOKEN="a2a-local-secret"
iac-code agui --port 41243 --a2a-url http://127.0.0.1:41242
```

## Configuración YAML

```yaml title="agui-server.yml"
host: 0.0.0.0
port: 41243
a2a-url: http://127.0.0.1:41242
state-dir: /var/lib/iac-code/agui
idle-shutdown: 0
debug: false
log-stdout: true
```

```bash
iac-code agui --config agui-server.yml
```

Los argumentos CLI explícitos prevalecen sobre YAML. Inyecte los tokens mediante variables de entorno en vez de guardarlos en el archivo.

| CLI / YAML | Predeterminado | Significado |
|------------|---------------|-------------|
| `--host` / `host` | `127.0.0.1` | Dirección HTTP |
| `--port` / `port` | `8000` | Puerto AG-UI; los ejemplos usan `41243` |
| `--a2a-url` / `a2a-url` | vacío | URL A2A local; vacío inicia un hijo |
| `--state-dir` / `state-dir` | `<config-dir>/agui` | Estado por thread |
| `--idle-shutdown` / `idle-shutdown` | `0` | Cierre por inactividad; `0` lo desactiva |
| `--debug` / `debug` | `false` | Logs de depuración |
| `--log-stdout` / `log-stdout` | `false` | Copiar logs a stdout |

| Variable | Uso |
|----------|-----|
| `IAC_CODE_AGUI_HOST` / `IAC_CODE_AGUI_PORT` | Dirección y puerto |
| `IAC_CODE_AGUI_A2A_URL` | URL upstream A2A local |
| `IAC_CODE_AGUI_A2A_TOKEN` | Bearer token de A2A |
| `IAC_CODE_AGUI_AUTH_TOKEN` | Bearer token del endpoint AG-UI |
| `IAC_CODE_AGUI_STATE_DIR` | Directorio de estado |
| `IAC_CODE_AGUI_ALLOWED_CWDS` | Raíces permitidas separadas con el separador de rutas del SO |
| `IAC_CODE_CONFIG_DIR` | Raíz de configuración de iac-code |

## Comprobación de salud

```bash
curl http://127.0.0.1:41243/health
```

```json
{
  "status": "ok",
  "protocol": "ag-ui",
  "protocolPackageVersion": "0.1.20",
  "executionKernel": "a2a-1.0",
  "serverVersion": "versión actual de iac-code"
}
```

## Cliente JavaScript oficial

```bash
pnpm add @ag-ui/client@0.0.58
```

```javascript
import { HttpAgent, randomUUID } from "@ag-ui/client";

const agent = new HttpAgent({
  url: "http://127.0.0.1:41243/",
  threadId: randomUUID(),
  // Con IAC_CODE_AGUI_AUTH_TOKEN:
  // headers: { Authorization: `Bearer ${process.env.AG_UI_TOKEN}` },
});

const forwardedProps = {
  iacCode: {
    schemaVersion: 1,
    rosInvocationId: randomUUID(),
    cwd: process.cwd(),
    runMode: "normal",
    preferredLanguage: "es",
  },
};

agent.addMessage({
  id: randomUUID(),
  role: "user",
  content: "Crea una plantilla de VPC con dos vSwitches.",
});

const subscriber = {
  onTextMessageContentEvent({ event }) {
    process.stdout.write(event.delta);
  },
  onToolCallStartEvent({ event }) {
    console.log(`\n[tool] ${event.toolCallName}`);
  },
  onStepStartedEvent({ event }) {
    console.log(`\n[step] ${event.stepName}`);
  },
  onRunErrorEvent({ event }) {
    console.error(`\n${event.code}: ${event.message}`);
  },
};

await agent.runAgent({ forwardedProps }, subscriber);
```

Pase `Authorization` mediante `HttpAgent.headers` si el endpoint usa token. En un navegador, use normalmente un backend del mismo origen o un proxy inverso; el adaptador no configura CORS.

## Resolver Interrupt

El cliente mantiene los Interrupt en `agent.pendingInterrupts`. Construya cada respuesta según su `responseSchema`:

```javascript
const responses = agent.pendingInterrupts.map((interrupt) => ({
  interruptId: interrupt.id,
  status: "resolved",
  payload: { decision: "allow_once" },
}));

await agent.runAgent({ forwardedProps, resume: responses }, subscriber);
```

Este payload solo sirve para permisos cuyo schema exige `decision`. Preguntas y selecciones tienen otros schemas.

Resume conserva `threadId` y `rosInvocationId`, usa un `runId` nuevo, responde una vez a todos los Interrupt pendientes y proporciona un payload válido para `resolved`. Use `cancelled` si el usuario decide no continuar.

## Iniciar un Pipeline

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

Procese `STEP_*`, `TOOL_CALL_*`, `ACTIVITY_SNAPSHOT` y `CUSTOM`. Los clientes genéricos que ignoren las extensiones siguen recibiendo todos los eventos estándar.

## Workspace, credenciales y estado

Cada solicitud proporciona un `cwd` absoluto bajo una raíz permitida por `IAC_CODE_AGUI_ALLOWED_CWDS` o `IACCODE_A2A_ALLOWED_CWDS`. El modelo, la clave LLM y las credenciales temporales de Alibaba Cloud pueden enviarse en `forwardedProps.iacCode`; el adaptador no las guarda en su estado.

El estado predeterminado se distribuye por thread:

```text
<IAC_CODE_CONFIG_DIR>/agui/threads/<threadId>.json
```

No se examinan todos los threads al iniciar. Los UUID conservan nombres legibles; los ID inseguros se codifican y los muy largos usan una clave de longitud fija. El JSON siempre conserva y valida el `threadId` original. Solo se guardan relaciones, Interrupt e idempotencia, nunca el contenido de la conversación ni credenciales.

## Continúe leyendo

- [Descripción general](./overview.md)
- [Referencia del protocolo](./protocol-reference.md)
