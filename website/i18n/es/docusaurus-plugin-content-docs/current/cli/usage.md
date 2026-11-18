---
title: Vision general del CLI
description: Iniciar IaC Code desde la terminal y elegir el flujo de trabajo adecuado.
---

# Vision general del CLI

Ejecuta `iac-code` en una terminal para iniciar la aplicación web local y abrir el navegador. En el primer uso, configura el proveedor LLM y las credenciales de Alibaba Cloud en Ajustes.

```bash
iac-code
iac-code web --no-open
```

[Modo interactivo](./interactive-mode.md):

```bash
iac-code tui
iac-code tui --help
```

`iac-code` abre la aplicación web de forma predeterminada. Coloca las opciones de terminal después de `iac-code tui`, por ejemplo `iac-code tui --resume <id>`. Las opciones headless `--prompt` y stream-json siguen en el comando principal. Sin una terminal interactiva, inicia la web explícitamente con `iac-code web --no-open`.

```bash
iac-code --prompt "Create an OSS Bucket"
iac-code --input-format stream-json --output-format stream-json
```
