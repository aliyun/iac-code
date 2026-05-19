---
title: Variables d'environnement
description: Toutes les variables d'environnement prises en charge et les règles de priorité.
---

# Variables d'environnement

IaC Code lit la configuration depuis les arguments CLI, les variables d'environnement et les fichiers de configuration. L'ordre de priorité est :

```text
CLI arguments > environment variables > configuration files
```

Les variables d'environnement sont utiles pour les pipelines CI/CD, les conteneurs et les remplacements ponctuels sans modifier les fichiers de configuration.

## Configuration LLM

| Variable | Description |
|---|---|
| `IAC_CODE_PROVIDER` | Nom du fournisseur de modèles (insensible à la casse) : `Anthropic`, `OpenAI`, `DashScope`, `DashScopeTokenPlan`, `DeepSeek`, `OpenAPICompatible` |
| `IAC_CODE_MODEL` | Nom du modèle |
| `IAC_CODE_BASE_URL` | Point de terminaison API pour `OpenAPICompatible` uniquement ; ignoré pour les autres fournisseurs |
| `IAC_CODE_API_KEY` | Clé API du fournisseur ; remplace la clé du fournisseur actif dans `.credentials.yml` |

Consultez [Fournisseurs LLM](./llm-providers.md) pour les détails des fournisseurs.

## Identifiants Alibaba Cloud

| Variable | Description |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | Jeton STS ; bascule le mode d'identification vers STS lorsqu'il est défini |
| `ALIBABA_CLOUD_REGION_ID` | Région par défaut |

Consultez [Identifiants Alibaba Cloud](./alibaba-cloud-credentials.md) pour plus de détails.

## Télémétrie

| Variable | Description |
|---|---|
| `IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | Définir à `1` / `true` / `yes` / `on` pour désactiver le trafic de télémétrie non essentiel |
| `DISABLE_TELEMETRY` | Définir à `1` / `true` / `yes` / `on` pour désactiver toute la télémétrie |
| `IAC_CODE_TELEMETRY_ENDPOINT` | Point de terminaison OTLP de base ; les points de terminaison de signaux individuels utilisent cette valeur par défaut |
| `IAC_CODE_TELEMETRY_TRACES_ENDPOINT` | Point de terminaison de remplacement pour les traces |
| `IAC_CODE_TELEMETRY_METRICS_ENDPOINT` | Point de terminaison de remplacement pour les métriques |
| `IAC_CODE_TELEMETRY_LOGS_ENDPOINT` | Point de terminaison de remplacement pour les journaux |
| `IAC_CODE_TELEMETRY_HEADERS` | En-têtes OTLP personnalisés (format JSON ou clé=valeur) |

## Autres

| Variable | Description |
|---|---|
| `IAC_CODE_CONFIG_DIR` | Remplace le répertoire de configuration à l'exécution (par défaut `~/.iac-code/`) ; prend en charge l'expansion de `~` et `$VAR`. Tous les artefacts persistés (identifiants, paramètres, historique, projects, image-cache, skills, telemetry, etc.) suivent ce répertoire |
| `IAC_CODE_ENV` | Label d'environnement de déploiement (par défaut : `production`) |
| `IAC_CODE_TENANT_ID` | Identifiant de locataire pour la télémétrie ; préfixé automatiquement avec `iac_tenant_` si ce n'est pas déjà le cas |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Point de terminaison OpenTelemetry standard ; lorsqu'il est défini, active l'export OTLP |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | Capturer le contenu des messages/outils GenAI sur les spans : `SPAN_ONLY`, `EVENT_ONLY`, `SPAN_AND_EVENT` |
