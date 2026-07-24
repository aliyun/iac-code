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
| `IAC_CODE_PROVIDER` | Nom du fournisseur de modèles (insensible à la casse). Valeurs valides : `DashScope`, `DashScope Token Plan`, `OpenAI`, `Anthropic`, `DeepSeek`, `Gemini`, `Azure OpenAI`, `ModelScope`, `Kimi CN`, `Kimi Intl`, `MiniMax CN`, `MiniMax Intl`, `ZhiPu CN`, `ZhiPu Intl`, `Volcengine CN`, `SiliconFlow CN`, `SiliconFlow Intl`, `Aliyun CodingPlan`, `Aliyun CodingPlan Intl`, `ZhiPu CN CodingPlan`, `ZhiPu Intl CodingPlan`, `Volcengine CodingPlan`, `OpenAPI Compatible`, `Anthropic Compatible`, `OpenRouter`, `Ollama`, `LM Studio` |
| `IAC_CODE_MODEL` | Nom du modèle |
| `IAC_CODE_BASE_URL` | Point de terminaison API pour `OpenAPI Compatible` uniquement ; ignoré (avec un avertissement) pour les autres fournisseurs |
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
| `IAC_CODE_CHANNEL` | Canal source de télémétrie stable et à faible cardinalité (par défaut : `unknown`), par exemple `ros_official` ou `partner_acme` |

## Autres

| Variable | Description |
|---|---|
| `IAC_CODE_CONFIG_DIR` | Remplace le répertoire de configuration à l'exécution (par défaut `~/.iac-code/`) ; prend en charge l'expansion de `~` et `$VAR`. Tous les artefacts persistés (identifiants, paramètres, historique, projects, image-cache, skills, telemetry, etc.) suivent ce répertoire |
| `IAC_CODE_LOG_DIR` | Remplace le répertoire local des journaux de démarrage/débogage (par défaut `<config-dir>/logs/`) ; prend en charge l'expansion de `~` et `$VAR`. Les enregistrements d'audit des permissions suivent le layout de session et ne sont pas déplacés par cette variable |
| `IAC_CODE_PERMISSION_AUDIT_INCLUDE_TOOL_INPUT` | Remplace `permissions.audit.include_tool_input` ; définissez-le sur `1` / `true` / `yes` / `on` pour inclure une entrée d'outil sous forme uniquement dans les enregistrements d'audit des permissions, avec type/longueur/empreinte au lieu des chaînes de payload métier brutes et avec empreinte pour les noms de champs hors liste blanche |
| `IAC_CODE_ENV` | Label d'environnement de déploiement (par défaut : `production`) |
| `IAC_CODE_TENANT_ID` | Identifiant de locataire pour la télémétrie ; préfixé automatiquement avec `iac_tenant_` si ce n'est pas déjà le cas |
| `IAC_CODE_GIT_BASH_PATH` | Chemin vers `bash.exe` de Git Bash sous Windows lorsqu'il n'est pas dans le PATH |
| `IAC_CODE_A2A_PUSH_KEYRING` | Trousseau de clés secret push A2A chiffré géré par l'environnement (format JSON) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Point de terminaison OpenTelemetry standard ; lorsqu'il est défini, active l'export OTLP |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | Capturer le contenu des messages/outils GenAI sur les spans : `SPAN_ONLY`, `EVENT_ONLY`, `SPAN_AND_EVENT` |


## Sauvegarde de session

| Variable | Description |
|---|---|
| `IAC_CODE_CONFIG_BACKUP_DIR` | Répertoire optionnel de sauvegarde des sessions; prend en charge l’expansion `~` et `$VAR`, ainsi que l’expansion `%VAR%` sous Windows. Dans PowerShell, fournissez un chemin concret ou laissez le shell développer `$env:VAR` avant de démarrer `iac-code`. Dans les déploiements sandbox, il s’agit souvent d’un chemin OSS monté, mais il doit être indépendant de `IAC_CODE_CONFIG_DIR` et de toute source de session, sans chevauchement, avec une latence assez faible pour les checkpoints critiques. Les chemins UNC, lecteurs mappés et chemins OSS montés doivent conserver le verrouillage de fichier `.backup-lock`, le remplacement atomique et les métadonnées de fichier pour la réplication incrémentale; évitez les ancêtres symlink, junction ou reparse point pour la source de session active, la racine de sauvegarde et les sessions miroir. Lorsqu’il est activé, les points de contrôle répliquent chaque session v2 vers `<backup>/projects/<project>/<session_id>/` avec la même arborescence que la session active; `.backup-state.json` et `.backup-lock` restent locaux et ne sont pas copiés. Les sauvegardes de fin de tour normal utilisent `normal_turn_end` et ne bloquent pas la réponse; seuls les échecs de checkpoints `critical=true` bloquent la publication. Les index A2A task/context partagés peuvent être montés séparément. |
