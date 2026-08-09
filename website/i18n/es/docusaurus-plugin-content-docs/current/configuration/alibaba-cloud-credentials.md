---
title: Credenciales de Alibaba Cloud
description: Configurar credenciales de Alibaba Cloud, incluida la autenticación mediante rol RAM de ECS.
---

# Credenciales de Alibaba Cloud

Las credenciales de Alibaba Cloud son necesarias para las operaciones que inspeccionan o gestionan recursos en la nube.

## Rol RAM de ECS

Usa **ECS RAM Role** cuando IaC Code se ejecute en una instancia ECS de Alibaba Cloud que tenga un rol RAM asociado. IaC Code obtiene credenciales STS temporales del servicio de metadatos de la instancia ECS (IMDS), las renueva automáticamente y no guarda un AccessKey ID, un AccessKey Secret ni un token STS en la configuración.

Puedes configurar este modo desde todas las interfaces de usuario:

- En el REPL, ejecuta `/auth`, elige **Configurar servicio cloud de IaC**, luego **Alibaba Cloud** y **ECS RAM Role**.
- En la aplicación Web o Desktop, abre **Configuración > Credenciales cloud**, elige **Alibaba Cloud** y selecciona **ECS RAM Role** como método de autenticación.

Selecciona la región que se usará para las llamadas a las API cloud. El nombre del rol RAM de ECS es opcional: déjalo en blanco para detectar mediante IMDS el rol asociado a la instancia. El nombre guardado en IaC Code tiene prioridad sobre `ALIBABA_CLOUD_ECS_METADATA`; si ninguno está definido, IaC Code solicita a IMDS que detecte el nombre del rol.

La configuración equivalente en `.cloud-credentials.yml` es:

```yaml
aliyun:
  mode: EcsRamRole
  region_id: cn-beijing
  ram_role_name: MyEcsRole # Opcional; omítelo o déjalo vacío para la detección automática
```

IaC Code también reconoce el perfil activo de `~/.aliyun/config.json` cuando su `mode` es `EcsRamRole`; `ram_role_name` también es opcional en ese archivo.

La configuración puede guardarse en cualquier equipo, pero las llamadas a las API cloud solo funcionan cuando IMDS de ECS está accesible y la instancia tiene un rol RAM coincidente. Las políticas RAM asociadas al rol determinan qué API están permitidas.

## Inicio de sesión OAuth en el navegador

La ruta de configuración interactiva recomendada es `/auth`:

```text
/auth
```

Elige **Configurar servicio cloud de IaC**, luego **Alibaba Cloud** y después **OAuth Login (Browser)**. IaC Code abre un flujo de autorización en el navegador, espera la devolución de llamada local, intercambia el código de autorización con PKCE y guarda credenciales temporales respaldadas por OAuth en `.cloud-credentials.yml`, dentro del directorio de configuración de IaC Code.

Durante la configuración puedes elegir el sitio OAuth de China o el internacional. IaC Code guarda el sitio elegido junto con el refresh token para que las actualizaciones posteriores usen el mismo endpoint.

Las credenciales OAuth se actualizan automáticamente cuando el access token o las credenciales STS están por caducar. Si el refresh token caduca o se revoca, ejecuta `/auth` de nuevo y elige OAuth Login (Browser).

## Variables de entorno

Variables de entorno soportadas:

| Variable | Descripcion |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | Token STS; cambia el modo de credenciales a STS cuando se establece |
| `ALIBABA_CLOUD_REGION_ID` | Region predeterminada |
| `ALIBABA_CLOUD_ECS_METADATA` | Nombre opcional del rol RAM de ECS; se usa cuando el modo ya es `EcsRamRole` y no hay un nombre guardado, pero no selecciona el modo por sí solo |
| `ALIBABA_CLOUD_ECS_METADATA_DISABLED` | Establécelo en `true` para deshabilitar las credenciales de metadatos de la instancia ECS |
| `ALIBABA_CLOUD_IMDSV1_DISABLED` | Establécelo en `true` para exigir IMDSv2 e impedir el fallback a IMDSv1 |

Usa credenciales de prueba o temporales cuando experimentes. No pegues secretos de produccion en el historial del shell, capturas de pantalla, registros o reportes de problemas.
