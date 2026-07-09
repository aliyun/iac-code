---
title: Transporte HTTP+SSE
description: Ejecutar el servidor ACP a traves de HTTP con Server-Sent Events para escenarios remotos y multi-cliente.
sidebar_position: 5
---

# Transporte HTTP+SSE

El servidor ACP de iac-code soporta dos modos de transporte. El transporte **Stdio** predeterminado se comunica a traves de entrada/salida estandar y es ideal para integraciones locales con IDEs. El transporte **HTTP+SSE** expone un endpoint de red y transmite las respuestas mediante Server-Sent Events, lo que lo hace adecuado para despliegues remotos, entornos con balanceo de carga y acceso multi-cliente.

## Por que HTTP+SSE

Stdio tiene limitaciones inherentes:

- Requiere que el proceso del servidor sea un hijo directo del cliente — sin acceso remoto.
- La gestion bloqueante de procesos dificulta servir a multiples clientes de forma concurrente.
- Incompatible con proxies de red, balanceadores de carga o despliegues en contenedores.

HTTP+SSE aborda estas restricciones:

- **Compatible con red** — accesible desde cualquier maquina que pueda alcanzar el endpoint.
- **Multi-cliente** — cada cliente obtiene una conexion aislada con su propio flujo de eventos.
- **Listo para infraestructura** — funciona detras de proxies inversos, en contenedores y con herramientas de monitoreo HTTP estandar.
- **Facil integracion** — cualquier cliente HTTP (curl, fetch, SDK) puede interactuar con el servidor.

## Iniciar el servidor HTTP

```bash
# Default port 8765
iac-code acp --transport http

# Custom port
iac-code acp --transport http --port 9090
```

El servidor usa [Starlette](https://www.starlette.io/) como framework ASGI y se ejecuta sobre Uvicorn.

## Rutas

Todas las rutas se sirven en la ruta `/acp`. El metodo HTTP determina la operacion.

### `POST /acp`

Envia una solicitud JSON-RPC al servidor.

- **`initialize`** — Crea una nueva conexion y devuelve la respuesta JSON-RPC completa directamente. La respuesta incluye un encabezado `Acp-Connection-Id`.
- **Todos los demas metodos** — Requiere un encabezado `Acp-Connection-Id` valido. Devuelve `202 Accepted` inmediatamente; el resultado real se entrega asincronamente a traves del flujo SSE.

### `GET /acp`

Abre un flujo de Server-Sent Events para recibir respuestas y notificaciones.

- Requiere el encabezado `Acp-Connection-Id`.
- Los eventos tienen tipo `message` con la respuesta/notificacion JSON-RPC como campo `data`.
- El flujo incluye campos `id` y `retry` para reconexion automatica.

### `DELETE /acp`

Cierra la conexion y libera todos los recursos asociados.

- Requiere el encabezado `Acp-Connection-Id`.
- Devuelve `200 OK`.

## ID de conexion

El ID de conexion vincula las solicitudes de un cliente con su flujo de eventos SSE.

1. El cliente envia un `POST /acp` con el metodo `initialize`.
2. El servidor responde con el resultado de inicializacion y un encabezado de respuesta `Acp-Connection-Id` que contiene un UUID.
3. Todas las solicitudes posteriores (`POST`, `GET`, `DELETE`) deben incluir el encabezado de solicitud `Acp-Connection-Id` con este valor.
4. Cada ID de conexion se asigna a una sesion de agente ACP independiente con su propia cola de eventos.

Si una solicitud hace referencia a un ID de conexion faltante o invalido, el servidor devuelve `400 Bad Request`.

## Autenticacion

El servidor soporta autenticacion opcional con token Bearer a traves de la variable de entorno `IACCODE_ACP_HTTP_TOKEN`.

```bash
# Set the token before starting the server
export IACCODE_ACP_HTTP_TOKEN=your-secret-token
iac-code acp --transport http
```

Cuando se establece, cada solicitud debe incluir:

```
Authorization: Bearer your-secret-token
```

| Escenario | Comportamiento |
|----------|----------|
| Token no establecido | No se requiere autenticacion (adecuado para desarrollo local) |
| Token establecido, encabezado coincide | La solicitud procede normalmente |
| Token establecido, encabezado faltante/incorrecto | Se devuelve `401 Unauthorized` |

## Flujo de trabajo completo

A continuacion se muestra una interaccion completa usando `curl`:

```bash
# Step 1: Initialize — creates a connection and returns the Connection ID
CONN_ID=$(curl -s -D - -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}' \
  | grep -i 'acp-connection-id' | awk '{print $2}' | tr -d '\r')

echo "Connection ID: $CONN_ID"

# Step 2: Open the SSE stream (run in background)
curl -N http://localhost:8765/acp \
  -H "Acp-Connection-Id: $CONN_ID" &
SSE_PID=$!

# Step 3: Create a session
curl -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: $CONN_ID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/workspace"}}'

# Step 4: Send a prompt
curl -X POST http://localhost:8765/acp \
  -H "Content-Type: application/json" \
  -H "Acp-Connection-Id: $CONN_ID" \
  -d '{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"...","prompt":[{"type":"text","text":"Hello"}]}}'

# Step 5: Close the connection
curl -X DELETE http://localhost:8765/acp \
  -H "Acp-Connection-Id: $CONN_ID"

# Clean up background SSE process
kill $SSE_PID 2>/dev/null
```

:::tip
La respuesta de `initialize` se devuelve sincronamente (dentro de un tiempo limite de 30 segundos). Todas las respuestas posteriores llegan exclusivamente a traves del flujo SSE abierto en el Paso 2.
:::
