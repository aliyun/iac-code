---
sidebar_position: 1
title: 概要
description: IaC Code の機能と始め方。
---

# 概要

IaC Code は、クラウドインフラの設計、生成、デプロイ、管理を支援する AI アシスタントです。Desktop アプリ、ローカル Web アプリ、対話型ターミナル、自動化インターフェイスから利用でき、別のエージェントの Skill としても組み込めます。アーキテクチャはマルチクラウドワークフローを想定して設計されており、現在のリリースでは Alibaba Cloud ROS と Terraform ワークフローをサポートしています。

主な機能：

- **言葉にすれば、即生成** — 自然言語で必要なものを記述するだけで、検証済みですぐにデプロイ可能な ROS テンプレート、または生成された Terraform テンプレートを作成します。
- **ワンコマンドで本番へ** — Alibaba Cloud ROS では、テンプレートから稼働中のインフラまでを一気通貫で実現し、リージョンをまたいでスタックの作成・更新・削除・監視を行います。Terraform のサポートはテンプレートの生成と変換が対象で、デプロイは含みません。
- **クラウドの知見を内蔵** — ドキュメント検索、リソース在庫確認、デプロイ前のコスト見積もり。すべての判断が実際のクラウドデータに裏付けられています。

用途に合う入口を選んでください。

- すぐに使える GUI には [Desktop アプリ](./desktop-app.md)をダウンロードします。
- [インストール](./getting-started/installation.md)と[クイックスタート](./getting-started/quick-start.md)に従って、REPL、ヘッドレスモード、またはローカル [Web アプリ](./web-app.md)を使います。
- [IaC Code 公式 Skills の概要](./a2a/skill-overview.md)で配布方法を選び、対応エージェントに Alibaba Cloud インフラ機能を追加します。
- 他のアプリやサービスへの統合には [ACP](./acp/overview.md)、[A2A](./a2a/overview.md)、[AG-UI](./agui/overview.md)を使います。

すべての入口でモデル設定が必要です。クラウドリソースの照会、変更、デプロイを行う場合は [Alibaba Cloud 認証情報](./configuration/alibaba-cloud-credentials.md)も設定してください。
