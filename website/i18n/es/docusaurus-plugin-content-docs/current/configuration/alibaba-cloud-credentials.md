---
title: Credenciales de Alibaba Cloud
description: Configurar credenciales de AccessKey o STS de Alibaba Cloud.
---

# Credenciales de Alibaba Cloud

Las credenciales de Alibaba Cloud son necesarias para las operaciones que inspeccionan o gestionan recursos en la nube.

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

Usa credenciales de prueba o temporales cuando experimentes. No pegues secretos de produccion en el historial del shell, capturas de pantalla, registros o reportes de problemas.
