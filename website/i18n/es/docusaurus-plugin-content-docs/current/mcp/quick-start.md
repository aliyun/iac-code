---
sidebar_position: 2
title: Inicio rapido MCP
description: Añadir y conectar servidores MCP desde el panel Plugins de Desktop o Web.
---

# Inicio rapido MCP

En escritorio y Web, añada y gestione servidores en la pestaña **MCP** de **Complementos**. Los comandos CLI y archivos siguientes se destinan a automatización y configuración avanzada.

## Conectar un servidor HTTP remoto

1. Abre **Complementos → MCP**, elige el proyecto y pulsa **Añadir servidor**.

2. Introduce el **Nombre**, elige el **Ámbito** y selecciona **http (remoto)** como tipo de transporte.

3. Rellena la **URL** y las cabeceras necesarias; usa referencias a variables de entorno para los secretos.

4. Pulsa **Crear**. Si la configuración de proyecto necesita aprobación, revísala y pulsa **Aprobar**.

5. Si necesita autenticación, pulsa **Autenticar**, completa la autorización en el navegador y vuelve al panel.

6. Pulsa **Comprobar conexión** y **Ver capacidades**. Comprueba los recursos, herramientas o prompts esperados antes de usarlos.

## Conectar un servidor stdio local

1. Abre **Complementos → MCP**, elige el proyecto y pulsa **Añadir servidor**.

2. Introduce el **Nombre**, el **Ámbito** y selecciona **stdio (comando local)**.

3. Rellena el **Comando**, un argumento por línea en **Argumentos**, y las variables de entorno necesarias. El ejecutable debe existir en la máquina que ejecuta IaC Code.

4. Pulsa **Crear** y aprueba la configuración de proyecto si es necesario.

5. Usa **Comprobar conexión** y **Ver capacidades** para confirmar la conexión y el descubrimiento.

:::note

El ámbito de usuario abarca los proyectos; el local es privado al directorio elegido; el de proyecto escribe un `.mcp.json` compartible y requiere aprobación local. Para modificar un servidor existente, usa su acción de edición y pulsa **Guardar**.

:::

## Automatización CLI

```bash
iac-code mcp add --transport http yuque https://mcp.example.com/yuque/mcp
iac-code mcp auth yuque
iac-code mcp get yuque --check
```

```bash
iac-code mcp add local-files --scope user -- npx -y @modelcontextprotocol/server-filesystem ./templates
iac-code mcp get local-files --config-only
```

## Siguientes pasos

- Consulta [Configuración MCP](./configuration.md) para ámbitos, archivos de proyecto, cabeceras, opciones OAuth y formatos JSON avanzados.
- Consulta [OAuth y seguridad](./oauth-and-security.md) para almacenamiento de tokens, reinicio y aprobaciones.
- Consulta [Solución de problemas](./troubleshooting.md) si un servidor necesita aprobación o autenticación, o falla al conectar.
