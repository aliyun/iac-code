---
title: Alibaba Cloud-Anmeldedaten
description: Alibaba Cloud-Anmeldedaten einschließlich ECS-RAM-Rollen-Authentifizierung konfigurieren.
---

# Alibaba Cloud-Anmeldedaten

Diese Schritte gelten für Desktop und Web; beide verwenden dieselbe Gesprächsoberfläche.

## Cloud-Zugriff in Desktop oder Web einrichten

1. Öffnen Sie **Einstellungen → Cloud-Anmeldedaten** und wählen Sie **Alibaba Cloud**.

2. Wählen Sie die Region für Abfragen und Bereitstellungen, beispielsweise `cn-hangzhou`.

3. Wählen Sie das Anmeldeverfahren und füllen Sie die zugehörigen Felder aus.

4. Klicken Sie für AccessKey, STS oder RAM-/ECS-RAM-Rollen auf **Cloud-Anmeldedaten speichern**. Folgen Sie für OAuth der Browseranmeldung weiter unten.

5. Beschreiben Sie anschließend die Cloud-Aufgabe im Gespräch. Das Speichern richtet nur den Zugriff ein; es erstellt keine Ressourcen und erweitert keine RAM-Rechte.

| Verfahren | Eingaben |
|---|---|
| AccessKey | `AccessKeyId` und `AccessKeySecret`. |
| STS Token | `AccessKeyId`, `AccessKeySecret` und `StsToken` derselben temporären Zugangsdaten. |
| RAM-Rolle | Quellschlüssel `AccessKeyId` und `AccessKeySecret`, Ziel `RamRoleArn` und optional `RamSessionName`. |
| ECS-RAM-Rolle | Optionaler Rollenname; leer wird die Rolle auf der ausführenden ECS-Instanz ermittelt. |
| OAuth-Browseranmeldung | Kontostandort wählen und im Browser autorisieren; Token nicht manuell eingeben. |

Alibaba Cloud-Anmeldedaten werden fuer Operationen benoetigt, die Cloud-Ressourcen ueberpruefen oder verwalten.

Die Tools `resolve_cloud_resource_selector` und `select_cloud_resource` verwenden dieselbe Entscheidung ueber effektive Anmeldedaten wie `aliyun_api`. In A2A haben Anfrage-/Sitzungsanmeldedaten Vorrang; fehlen sie, greift die normale Kette aus lokaler Konfiguration, Umgebungsvariablen und Alibaba-Cloud-CLI-Anmeldedaten. Ohne gueltige effektive Anmeldedaten werden weder `aliyun_api` noch die beiden Ressourcenselektor-Tools registriert.

## ECS-RAM-Rolle

Verwenden Sie **ECS RAM Role**, wenn IaC Code auf einer Alibaba-Cloud-ECS-Instanz mit zugewiesener RAM-Rolle ausgeführt wird. IaC Code bezieht temporäre STS-Anmeldedaten vom Metadatendienst der ECS-Instanz (IMDS), aktualisiert sie automatisch und speichert weder AccessKey-ID, AccessKey-Secret noch STS-Token in der Konfiguration.

Öffnen Sie in der Web- oder Desktop-App **Einstellungen > Cloud-Anmeldedaten**, wählen Sie **Alibaba Cloud** und anschließend **ECS RAM Role** als Authentifizierungsmethode.

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

1. Öffnen Sie **Einstellungen → Cloud-Anmeldedaten**, wählen Sie **Alibaba Cloud** und **OAuth-Browseranmeldung**.

2. Wählen Sie die Region und die zum Konto passende **Anmeldeseite**: China oder International.

3. Klicken Sie auf **Mit Browser anmelden** und schließen Sie Anmeldung und Autorisierung auf Alibaba Cloud ab.

4. Kehren Sie zu IaC Code zurück und warten Sie auf die Erfolgsmeldung. Zugangsdaten und Standort werden durch den Anmeldevorgang gespeichert.

OAuth-Zugangsdaten werden automatisch erneuert, wenn das Zugriffstoken oder die STS-Zugangsdaten bald ablaufen. Läuft das Aktualisierungstoken ab oder wird es widerrufen, autorisieren Sie den Zugriff erneut unter **Einstellungen → Cloud-Anmeldedaten**.

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
