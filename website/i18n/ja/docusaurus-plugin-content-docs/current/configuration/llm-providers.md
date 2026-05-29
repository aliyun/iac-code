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
| `IAC_CODE_BASE_URL` | `OpenAPI Compatible` と `Anthropic Compatible` 用の API エンドポイント。他のプロバイダーでは無視されます |
| `IAC_CODE_API_KEY` | プロバイダー API キー |
