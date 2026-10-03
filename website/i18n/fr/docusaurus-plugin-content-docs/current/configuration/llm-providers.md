---
title: Fournisseurs LLM
description: Fournisseurs de modèles pris en charge et variables d'environnement.
---

# Fournisseurs LLM

IaC Code prend en charge plusieurs backends de fournisseurs de modèles. La sélection du fournisseur peut provenir des options CLI, des variables d'environnement ou des fichiers de configuration. L'ordre de priorité est :

```text
CLI arguments > environment variables > configuration files
```

## Fournisseurs Cloud

| Valeur du fournisseur | Fonction |
|---|---|
| `DashScope` | Point de terminaison compatible Alibaba Cloud DashScope (Bailian) |
| `DashScope Token Plan` | Point de terminaison Alibaba Cloud DashScope Token Plan |
| `OpenAI` | Modèles OpenAI |
| `Anthropic` | Modèles Anthropic |
| `DeepSeek` | Modèles DeepSeek |
| `Gemini` | Modèles Google Gemini |
| `Azure OpenAI` | Service Azure OpenAI |
| `ModelScope` | Point de terminaison d'inférence ModelScope |

## Fournisseurs en Chine

| Valeur du fournisseur | Fonction |
|---|---|
| `Kimi CN` | Point de terminaison Kimi (Moonshot AI) en Chine |
| `MiniMax CN` | Point de terminaison MiniMax en Chine |
| `ZhiPu CN` | Point de terminaison ZhiPu AI (GLM) en Chine |
| `Volcengine CN` | Point de terminaison Volcengine (ByteDance) en Chine |
| `SiliconFlow CN` | Point de terminaison SiliconFlow en Chine |

## Fournisseurs Internationaux

| Valeur du fournisseur | Fonction |
|---|---|
| `Kimi Intl` | Point de terminaison international Kimi (Moonshot AI) |
| `MiniMax Intl` | Point de terminaison international MiniMax |
| `ZhiPu Intl` | Point de terminaison international ZhiPu AI (GLM) |
| `SiliconFlow Intl` | Point de terminaison international SiliconFlow |

## Fournisseurs CodingPlan

| Valeur du fournisseur | Fonction |
|---|---|
| `Aliyun CodingPlan` | Point de terminaison Alibaba Cloud CodingPlan |
| `Aliyun CodingPlan Intl` | Point de terminaison international Alibaba Cloud CodingPlan |
| `ZhiPu CN CodingPlan` | Point de terminaison ZhiPu AI CodingPlan en Chine |
| `ZhiPu Intl CodingPlan` | Point de terminaison international ZhiPu AI CodingPlan |
| `Volcengine CodingPlan` | Point de terminaison Volcengine CodingPlan |

## Compatible / Points de terminaison personnalisés

| Valeur du fournisseur | Fonction |
|---|---|
| `OpenAPI Compatible` | Tout point de terminaison API compatible OpenAI |
| `Anthropic Compatible` | Tout point de terminaison API compatible Anthropic |
| `OpenRouter` | Passerelle d'agrégation OpenRouter |

## Fournisseurs Locaux

| Valeur du fournisseur | Fonction |
|---|---|
| `Ollama` | Serveur de modèles local Ollama |
| `LM Studio` | Serveur de modèles local LM Studio |

## Variables d'environnement LLM

| Variable | Description |
|---|---|
| `IAC_CODE_PROVIDER` | Nom du fournisseur de modèles (insensible à la casse). Consultez les tableaux ci-dessus pour les valeurs valides |
| `IAC_CODE_MODEL` | Nom du modèle |
| `IAC_CODE_BASE_URL` | Remplace le point de terminaison API du fournisseur actif ; prioritaire sur l’`apiBase` enregistré et l’URL intégrée par défaut |
| `IAC_CODE_API_KEY` | Clé API du fournisseur |

## Responses API

Les fournisseurs utilisant le protocole OpenAI emploient Chat Completions par défaut. Un modèle peut avoir Responses comme protocole intégré par défaut si ses capacités d’appel d’outils l’exigent. Les protocoles par défaut et les restrictions des appels d’outils avec Chat sont maintenus dans le catalogue intégré des modèles ; aucune liste séparée n’est reproduite ici. Les fournisseurs Anthropic conservent leur protocole Messages actuel.

Les fournisseurs utilisant le protocole OpenAI peuvent sélectionner explicitement Responses pour chaque modèle, notamment Azure OpenAI, `openai_compatible` et DashScope Token Plan. Configurez `apiBase` avec l’URL de base d’un service prenant en charge Responses. Pour Azure OpenAI, utilisez l’URL de base avec `/openai/v1/` et indiquez le nom du déploiement dans `model`.

[Azure OpenAI](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle) et [DashScope Token Plan](https://help.aliyun.com/en/model-studio/codex) documentent la prise en charge de Responses pour certains points de terminaison et modèles. Pour les points de terminaison compatibles personnalisés et les offres de programmation, cette prise en charge dépend du service utilisé. [Alibaba Cloud Coding Plan](https://help.aliyun.com/en/model-studio/coding-plan-faq) indique explicitement ne pas prendre en charge Responses. Si Responses est choisi pour un service ou un modèle incompatible, la requête renvoie une erreur ; IaC Code ne revient pas automatiquement à Chat.

Pour utiliser Responses avec un modèle Qwen pris en charge, ajoutez ce réglage au niveau du modèle dans `settings.yml` et remplacez `<model-id>` par l’identifiant réel du modèle :

```yaml
activeProvider: dashscope
providers:
  dashscope:
    model: <model-id>
    models:
      <model-id>:
        apiMode: responses
```

Définissez `apiMode` sur `chat_completions` pour revenir au protocole précédent. En l’omettant, vous utilisez le protocole intégré par défaut du modèle ; pour les modèles DashScope standard, il reste Chat Completions. Le réglage s’applique uniquement au modèle indiqué pour le fournisseur indiqué.

Si vous sélectionnez explicitement Chat, le modèle doit prendre en charge les appels d’outils avec le niveau de raisonnement choisi. Utilisez Responses si l’API Chat du modèle ne prend pas en charge cette combinaison.

Consultez la [configuration d’exécution](./runtime-configuration.md) pour les paramètres de requête et les limites de sortie.
