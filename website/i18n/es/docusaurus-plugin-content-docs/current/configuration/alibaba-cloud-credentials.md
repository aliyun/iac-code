---
title: Credenciales de Alibaba Cloud
description: Configurar credenciales de Alibaba Cloud, incluida la autenticación mediante rol RAM de ECS.
---

# Credenciales de Alibaba Cloud

Los pasos se aplican tanto a Desktop como a Web, que comparten la misma interfaz de conversación.

## Configurar el acceso a la nube en Desktop o Web

1. Abre **Ajustes → Credenciales de nube** y elige **Alibaba Cloud**.

2. Selecciona la región de consulta y despliegue, por ejemplo `cn-hangzhou`.

3. Elige el método de autenticación y rellena los campos correspondientes de la tabla.

4. Para AccessKey, STS o roles RAM/ECS RAM, pulsa **Guardar credenciales de nube**. Para OAuth, sigue los pasos de inicio de sesión con navegador que aparecen más abajo.

5. Vuelve a la conversación y describe la tarea. Guardar las credenciales configura el acceso; no crea recursos ni añade permisos RAM.

| Método | Datos necesarios |
|---|---|
| AccessKey | `AccessKeyId` y `AccessKeySecret`. |
| STS Token | `AccessKeyId`, `AccessKeySecret` y `StsToken` de las mismas credenciales temporales. |
| Rol RAM | Claves de origen `AccessKeyId` y `AccessKeySecret`, destino `RamRoleArn` y `RamSessionName` opcional. |
| Rol ECS RAM | Nombre de rol opcional; en blanco se descubre en la instancia ECS que ejecuta IaC Code. |
| OAuth con navegador | Elige el sitio de la cuenta y autoriza en el navegador, sin introducir tokens manualmente. |

Las credenciales de Alibaba Cloud son necesarias para las operaciones que inspeccionan o gestionan recursos en la nube.

Las herramientas `resolve_cloud_resource_selector` y `select_cloud_resource` aplican la misma decisión de credenciales efectivas que `aliyun_api`. En A2A tienen prioridad las credenciales de la solicitud o sesión; si no existen, se usa la cadena normal de configuración local, variables de entorno y credenciales de Alibaba Cloud CLI. Sin credenciales efectivas válidas, no se registran `aliyun_api` ni las dos herramientas de selección.

## Rol RAM de ECS

Usa **ECS RAM Role** cuando IaC Code se ejecute en una instancia ECS de Alibaba Cloud que tenga un rol RAM asociado. IaC Code obtiene credenciales STS temporales del servicio de metadatos de la instancia ECS (IMDS), las renueva automáticamente y no guarda un AccessKey ID, un AccessKey Secret ni un token STS en la configuración.

En la aplicación Web o Desktop, abre **Configuración > Credenciales cloud**, elige **Alibaba Cloud** y selecciona **ECS RAM Role** como método de autenticación.

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

1. Abre **Ajustes → Credenciales de nube**, selecciona **Alibaba Cloud** y **Inicio de sesión OAuth con navegador**.

2. Selecciona la región y el **Sitio de inicio de sesión** de la cuenta: China o Internacional.

3. Pulsa **Iniciar sesión con el navegador** y completa la autenticación y autorización en Alibaba Cloud.

4. Vuelve a IaC Code y espera la confirmación. El flujo guarda las credenciales y el sitio seleccionado.

Las credenciales OAuth se renuevan automáticamente cuando el token de acceso o las credenciales STS se acercan a su vencimiento. Si el token de renovación caduca o se revoca, vuelva a autorizar desde **Ajustes → Credenciales de nube**.

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
