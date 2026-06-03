---
title: Alibaba Cloud-Anmeldedaten
description: Konfigurieren Sie Alibaba Cloud AccessKey- oder STS-Anmeldedaten.
---

# Alibaba Cloud-Anmeldedaten

Alibaba Cloud-Anmeldedaten werden fuer Operationen benoetigt, die Cloud-Ressourcen ueberpruefen oder verwalten.

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

Verwenden Sie Test- oder temporaere Anmeldedaten beim Experimentieren. Fuegen Sie keine Produktionsgeheimnisse in Shell-Verlaeufe, Screenshots, Protokolle oder Fehlerberichte ein.
