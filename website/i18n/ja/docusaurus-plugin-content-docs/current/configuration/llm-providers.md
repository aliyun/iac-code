---
title: LLM プロバイダー
description: サポートされるモデルプロバイダーと環境変数。
---

# LLM プロバイダー

IaC Code は複数のモデルプロバイダーバックエンドをサポートしています。プロバイダーの選択は CLI オプション、環境変数、または設定ファイルから行えます。優先順位は以下の通りです：

```text
CLI 引数 > 環境変数 > 設定ファイル
```

## クラウドプロバイダー

| プロバイダー値 | 用途 |
|---|---|
| `DashScope` | Alibaba Cloud DashScope（百炼）互換エンドポイント |
| `DashScope Token Plan` | Alibaba Cloud DashScope Token Plan エンドポイント |
| `OpenAI` | OpenAI モデル |
| `Anthropic` | Anthropic モデル |
| `DeepSeek` | DeepSeek モデル |
| `Gemini` | Google Gemini モデル |
| `Azure OpenAI` | Azure OpenAI サービス |
| `ModelScope` | ModelScope 推論エンドポイント |

## 中国国内プロバイダー

| プロバイダー値 | 用途 |
|---|---|
| `Kimi CN` | Kimi（Moonshot AI）中国国内エンドポイント |
| `MiniMax CN` | MiniMax 中国国内エンドポイント |
| `ZhiPu CN` | ZhiPu AI（GLM）中国国内エンドポイント |
| `Volcengine CN` | Volcengine（ByteDance）中国国内エンドポイント |
| `SiliconFlow CN` | SiliconFlow 中国国内エンドポイント |

## 国際プロバイダー

| プロバイダー値 | 用途 |
|---|---|
| `Kimi Intl` | Kimi（Moonshot AI）国際エンドポイント |
| `MiniMax Intl` | MiniMax 国際エンドポイント |
| `ZhiPu Intl` | ZhiPu AI（GLM）国際エンドポイント |
| `SiliconFlow Intl` | SiliconFlow 国際エンドポイント |

## CodingPlan プロバイダー

| プロバイダー値 | 用途 |
|---|---|
| `Aliyun CodingPlan` | Alibaba Cloud CodingPlan エンドポイント |
| `Aliyun CodingPlan Intl` | Alibaba Cloud CodingPlan 国際エンドポイント |
| `ZhiPu CN CodingPlan` | ZhiPu AI CodingPlan 中国国内エンドポイント |
| `ZhiPu Intl CodingPlan` | ZhiPu AI CodingPlan 国際エンドポイント |
| `Volcengine CodingPlan` | Volcengine CodingPlan エンドポイント |

## 互換 / カスタムエンドポイント

| プロバイダー値 | 用途 |
|---|---|
| `OpenAPI Compatible` | 任意の OpenAI 互換 API エンドポイント |
| `Anthropic Compatible` | 任意の Anthropic 互換 API エンドポイント |
| `OpenRouter` | OpenRouter アグリゲーションゲートウェイ |

## ローカルプロバイダー

| プロバイダー値 | 用途 |
|---|---|
| `Ollama` | Ollama ローカルモデルサーバー |
| `LM Studio` | LM Studio ローカルモデルサーバー |

## LLM 環境変数

| 変数 | 説明 |
|---|---|
| `IAC_CODE_PROVIDER` | モデルプロバイダー名（大文字小文字不問）。有効な値は上記の表を参照 |
| `IAC_CODE_MODEL` | モデル名 |
| `IAC_CODE_BASE_URL` | 現在アクティブなプロバイダーの API エンドポイントを上書きします。保存済みの `apiBase` と組み込みの既定 URL より優先されます |
| `IAC_CODE_API_KEY` | プロバイダー API キー |

## Responses API

OpenAI 形式のプロトコルを使うプロバイダーは、デフォルトで Chat Completions を使用します。ツール呼び出しの仕様に応じて、モデルのデフォルトプロトコルが Responses に設定されている場合があります。各モデルのデフォルトプロトコルと Chat のツール呼び出し制限は、組み込みのモデルカタログで一元管理し、ここでは別のモデル一覧を掲載しません。Anthropic プロバイダーは従来の Messages プロトコルを引き続き使用します。

OpenAI プロトコルを使用するプロバイダーでは、モデルごとに Responses を明示的に選択できます。Azure OpenAI、`openai_compatible`、DashScope Token Plan も対象です。`apiBase` には Responses に対応するサービスのベース URL を設定します。Azure OpenAI では `/openai/v1/` を含むベース URL を使い、`model` にデプロイ名を指定します。

[Azure OpenAI](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle) と [DashScope Token Plan](https://help.aliyun.com/en/model-studio/codex) は、対応するエンドポイントとモデルでの Responses の利用方法を公開しています。カスタムの互換エンドポイントやコーディングプランの対応状況は、サービスによって異なります。[Alibaba Cloud Coding Plan](https://help.aliyun.com/en/model-studio/coding-plan-faq) は Responses に対応していないと明記しています。未対応のサービスやモデルで Responses を選択すると、リクエストはエラーになります。IaC Code は自動で Chat に切り替えません。

対応する Qwen モデルで Responses を使うには、`settings.yml` に次のモデル単位の設定を追加し、`<model-id>` を実際のモデル ID に置き換えます。

```yaml
activeProvider: dashscope
providers:
  dashscope:
    model: <model-id>
    models:
      <model-id>:
        apiMode: responses
```

元に戻すには、`apiMode` を `chat_completions` に変更します。省略すると、モデルに組み込まれたデフォルトのプロトコルが使われます。標準 DashScope モデルのデフォルトは引き続き Chat Completions です。この設定は、指定したプロバイダーの指定したモデルにのみ適用されます。

Chat を明示的に選ぶ場合、モデルは指定した推論レベルでのツール呼び出しに対応している必要があります。モデルの Chat API がその組み合わせに対応していなければ、Responses を使用してください。

リクエスト設定と出力上限は[実行時の設定](./runtime-configuration.md)を参照してください。
