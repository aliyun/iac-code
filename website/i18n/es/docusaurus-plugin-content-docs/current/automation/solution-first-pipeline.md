---
title: Pipeline de solución primero
description: Elija una arquitectura antes de generar y desplegar su plantilla ROS.
---

# Pipeline de solución primero

`selling_solution_first` es un pipeline de compra para Alibaba Cloud que permite comparar arquitecturas antes de que IaC Code genere una plantilla ROS. Solo implementa y calcula el precio de la solución seleccionada, evitando trabajo en candidatos que no se desplegarán.

El pipeline `selling` sigue disponible y continúa siendo el predeterminado. El nuevo pipeline es una alternativa que se selecciona de forma explícita y no cambia las sesiones existentes de `selling`.

## Cuándo utilizarlo

Use `selling_solution_first` si desea:

- comparar varias arquitecturas, productos, costes, ventajas y riesgos antes de implementarlas;
- aclarar región, escala, red, disponibilidad o presupuesto antes de definir una plantilla;
- generar, previsualizar y calcular el precio únicamente de la arquitectura elegida;
- revisar los parámetros ROS finales y la cotización exacta antes de crear recursos en la nube.

| Pipeline | Orden de trabajo |
|---|---|
| `selling` | Genera y evalúa plantillas candidatas, permite elegir una y después la despliega. |
| `selling_solution_first` | Planifica y permite elegir una arquitectura, implementa solo esa opción y después la despliega. |

## Iniciar el pipeline

En el terminal interactivo:

```bash
IAC_CODE_MODE=pipeline \
IAC_CODE_PIPELINE_NAME=selling_solution_first \
iac-code
```

En la aplicación web local, seleccione el modo Pipeline al crear la conversación e inicie el servidor con el nombre del pipeline:

```bash
IAC_CODE_PIPELINE_NAME=selling_solution_first iac-code web
```

Con A2A, el cliente puede elegir el modo y el pipeline en cada mensaje sin modificar el valor predeterminado del servidor:

```json
{
  "metadata": {
    "iac_code": {
      "run_mode": "pipeline",
      "pipeline_name": "selling_solution_first",
      "preferredLanguage": "es",
      "candidatePresentation": "rich-v1"
    }
  }
}
```

`pipeline_name` acepta `selling` y `selling_solution_first`. Un valor no vacío no compatible se rechaza en lugar de ejecutar silenciosamente otro pipeline. Para continuar un pipeline guardado, reutilice el mismo `contextId` de A2A; la identidad almacenada en la instantánea duradera es la fuente autorizada.

## Las tres etapas

### 1. Planificar y elegir una solución

IaC Code comprueba primero si la solicitud es una tarea de infraestructura de Alibaba Cloud compatible. Puede formular preguntas concretas cuando falte información que cambie de forma importante los productos, la topología o el precio.

Después presenta entre una y tres soluciones comparables. Cada solución puede incluir:

- un diagrama de arquitectura y la topología;
- productos de Alibaba Cloud e inventario de recursos;
- especificaciones recomendadas y restricciones obligatorias;
- escenarios aplicables y problemas resueltos;
- un coste mensual aproximado para comparar;
- ventajas, desventajas, riesgos y motivos de la recomendación.

Puede elegir una solución, ajustar los requisitos y generar un nuevo conjunto, o cancelar. En esta etapa no se crea ninguna plantilla ROS ni ningún recurso en la nube.

### 2. Implementar la solución seleccionada

IaC Code trabaja únicamente en la solución seleccionada. Genera y escribe la plantilla ROS, la valida, resuelve los parámetros obligatorios, ejecuta `PreviewStack` y solicita una estimación precisa de ROS.

Antes del despliegue, la interfaz muestra la arquitectura final, los parámetros de la plantilla y la cotización. Puede:

- confirmar el despliegue;
- cambiar los parámetros permitidos y volver a calcular;
- regresar a la primera etapa para elegir o planificar otra solución;
- cancelar sin crear recursos en la nube.

La estimación aproximada de la etapa 1 y la cotización precisa de ROS de la etapa 2 son valores distintos. La confirmación del despliegue utiliza la cotización precisa y los parámetros actuales de la plantilla.

### 3. Desplegar

Tras la confirmación, IaC Code crea la pila ROS, transmite el progreso autorizado de la pila, espera el estado terminal y registra el ID y las salidas. Los errores de despliegue quedan disponibles para diagnóstico y recuperación.

## Confirmación del despliegue y permiso de herramienta

La confirmación del despliegue y el permiso de herramienta son dos límites de seguridad separados:

1. **Confirmación del despliegue**: acepta la solución, los parámetros y el coste cotizado.
2. **Permiso de herramienta**: autoriza una llamada concreta que modifica la nube, como `ros:CreateStack` o `vpc:CreateVpc`, para esta ejecución.

Aprobar el primer paso no aprueba automáticamente el segundo. Cuando una herramienta necesita permiso, IaC Code se detiene en ese punto y presenta una solicitud segura. Las operaciones de lectura, modificación y eliminación se distinguen visualmente. Los detalles de API pueden incluir producto, API, región, secuencia de llamadas y parámetros redactados; las credenciales, tokens, firmas y otros valores sensibles nunca aparecen en los campos de presentación.

El usuario puede elegir **Permitir una vez** o **Denegar**. La decisión se correlaciona con la solicitud exacta y se registra en el log de auditoría. Si no se puede conservar el registro de auditoría requerido, una decisión de permitir falla de forma segura.

## Pausa, recuperación y traspaso

La selección, las preguntas, la confirmación del despliegue y los permisos son esperas recuperables. IaC Code conserva una instantánea del pipeline antes de depender de la continuación del cliente. Tras reiniciar el proceso o recargar la conversación, la interfaz reconstruye las etapas completadas y restaura cada entrada pendiente en su posición original.

Para integraciones A2A:

- los eventos `permission_requested` y `permission_resolved` conservan la etapa y las coordenadas del candidato;
- `pendingPermissions` expone las solicitudes pendientes en una instantánea restaurada;
- una respuesta de permiso por el canal lateral reanuda la tarea y el contexto originales;
- repetir la misma decisión es idempotente, mientras que una decisión contradictoria se rechaza.

Cuando el pipeline termina, falla, sale antes de tiempo o se cancela, entrega el mismo contexto al chat normal. Las solicitudes posteriores pueden utilizar la solución elegida, la plantilla generada, el resultado del despliegue y el estado de limpieza sin iniciar otra conversación.

## Interfaces e idiomas

El pipeline funciona en el terminal interactivo, la aplicación web local, el contenedor web de Desktop, el modo de proceso SDK y el servidor A2A. Las interfaces ofrecen distintas capacidades de presentación —por ejemplo, A2A puede solicitar candidatos estructurados `rich-v1`—, pero comparten el estado y los límites de seguridad.

El texto visible admite inglés, chino simplificado, español, francés, alemán, japonés y portugués. Los clientes A2A eligen el idioma de una solicitud con `metadata.iac_code.preferredLanguage`; los nombres de campos, enumeraciones, identificadores y estructuras JSON no se traducen.

## Documentación relacionada

- [Modo Pipeline](./pipeline-mode.md)
- [Aplicación web](../web-app.md)
- [Referencia del protocolo A2A](../a2a/protocol-reference.md)
- [Credenciales de Alibaba Cloud](../configuration/alibaba-cloud-credentials.md)
