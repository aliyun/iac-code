---
title: Alibaba Cloud-Anmeldedaten
description: Alibaba Cloud-Anmeldedaten einschließlich ECS-RAM-Rollen-Authentifizierung konfigurieren.
---

# Alibaba Cloud-Anmeldedaten

Alibaba Cloud-Anmeldedaten werden fuer Operationen benoetigt, die Cloud-Ressourcen ueberpruefen oder verwalten.

## ECS-RAM-Rolle

Verwenden Sie **ECS RAM Role**, wenn IaC Code auf einer Alibaba-Cloud-ECS-Instanz mit zugewiesener RAM-Rolle ausgeführt wird. IaC Code bezieht temporäre STS-Anmeldedaten vom Metadatendienst der ECS-Instanz (IMDS), aktualisiert sie automatisch und speichert weder AccessKey-ID, AccessKey-Secret noch STS-Token in der Konfiguration.

Sie können diesen Modus über jede Benutzeroberfläche konfigurieren:

- Führen Sie im REPL `/auth` aus und wählen Sie **IaC-Cloud-Service konfigurieren**, danach **Alibaba Cloud** und **ECS RAM Role**.
- Öffnen Sie in der Web- oder Desktop-App **Einstellungen > Cloud-Anmeldedaten**, wählen Sie **Alibaba Cloud** und anschließend **ECS RAM Role** als Authentifizierungsmethode.

Wählen Sie die Region für Cloud-API-Aufrufe. Der Name der ECS-RAM-Rolle ist optional: Lassen Sie ihn leer, damit die der Instanz zugewiesene Rolle über IMDS automatisch erkannt wird. Ein in IaC Code gespeicherter Rollenname hat Vorrang vor `ALIBABA_CLOUD_ECS_METADATA`; wenn beide fehlen, lässt IaC Code den Rollennamen von IMDS ermitteln.

Die entsprechende `.cloud-credentials.yml`-Konfiguration lautet:

```yaml
aliyun:
  mode: EcsRamRole
  region_id: cn-beijing
  ram_role_name: MyEcsRole # Optional; für automatische Erkennung weglassen oder leer lassen
```

IaC Code erkennt auch das aktive Profil in `~/.aliyun/config.json`, wenn dessen `mode` den Wert `EcsRamRole` hat; `ram_role_name` ist auch dort optional.

Die Konfiguration kann auf jedem Rechner gespeichert werden. Cloud-API-Aufrufe sind jedoch nur erfolgreich, wenn ECS IMDS erreichbar ist und der Instanz eine passende RAM-Rolle zugewiesen wurde. Die an die Rolle gebundenen RAM-Richtlinien bestimmen, welche APIs zulässig sind.

## OAuth-Browser-Anmeldung

Der empfohlene interaktive Einrichtungsweg ist `/auth`:

```text
/auth
```

Wählen Sie **IaC-Cloud-Service konfigurieren**, dann **Alibaba Cloud** und anschließend **OAuth Login (Browser)**. IaC Code öffnet einen Browser-Autorisierungsablauf, wartet auf den lokalen Callback, tauscht den Autorisierungscode mit PKCE aus und speichert OAuth-gestützte temporäre Anmeldedaten in `.cloud-credentials.yml` im IaC-Code-Konfigurationsverzeichnis.

Während der Einrichtung können Sie die China- oder International-OAuth-Site wählen. IaC Code speichert die ausgewählte Site zusammen mit dem Refresh Token, damit spätere Aktualisierungen denselben Endpunkt verwenden.

OAuth-Anmeldedaten werden automatisch aktualisiert, wenn Access Token oder STS-Anmeldedaten bald ablaufen. Wenn der Refresh Token abläuft oder widerrufen wird, führen Sie erneut `/auth` aus und wählen Sie OAuth Login (Browser).

## Umgebungsvariablen

Unterstuetzte Umgebungsvariablen:

| Variable | Beschreibung |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey-ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey-Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | STS-Token; wechselt den Anmeldedatenmodus zu STS, wenn gesetzt |
| `ALIBABA_CLOUD_REGION_ID` | Standardregion |
| `ALIBABA_CLOUD_ECS_METADATA` | Optionaler Name der ECS-RAM-Rolle; wird verwendet, wenn der Modus bereits `EcsRamRole` ist und kein Rollenname gespeichert wurde, wählt den Modus aber nicht selbst aus |
| `ALIBABA_CLOUD_ECS_METADATA_DISABLED` | Auf `true` setzen, um Anmeldedaten aus ECS-Instanzmetadaten zu deaktivieren |
| `ALIBABA_CLOUD_IMDSV1_DISABLED` | Auf `true` setzen, um IMDSv2 zu verlangen und den Rückgriff auf IMDSv1 zu verhindern |

Verwenden Sie Test- oder temporaere Anmeldedaten beim Experimentieren. Fuegen Sie keine Produktionsgeheimnisse in Shell-Verlaeufe, Screenshots, Protokolle oder Fehlerberichte ein.
