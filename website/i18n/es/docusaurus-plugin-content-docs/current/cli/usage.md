---
title: Vision general del CLI
description: Iniciar IaC Code desde la terminal y elegir el flujo de trabajo adecuado.
---

# Vision general del CLI

Ejecute `iac-code` en un terminal para iniciar la aplicación web local y abrir el navegador. En el primer uso, configure el proveedor LLM y las credenciales de Alibaba Cloud en Ajustes.

```bash
iac-code
iac-code web --no-open
```

Al crear una conversación web o de escritorio, elija chat normal o Pipeline. Para automatizar, use un prompt único o un subproceso stream-json:

```bash
iac-code --prompt "Generar una plantilla VPC"
iac-code --input-format stream-json --output-format stream-json
```

Consulte las [opciones de línea de comandos](./command-line-options.md) y la [automatización headless](../automation/non-interactive-mode.md). Sin un terminal interactivo, inicie la web explícitamente con `iac-code web --no-open`.

Para flujos de terminal anteriores, consulte la [entrada de compatibilidad](./interactive-mode.md).
