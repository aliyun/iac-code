---
sidebar_position: 1
title: IaC Code 公式 Skills の概要
description: 公式 IaC Code Skills を比較し、用途に合う配布方法を選びます。
---

# IaC Code 公式 Skills の概要

IaC Code には 3 種類の公式 Skill 配布があります。いずれもエージェントとの会話から Alibaba Cloud
インフラを管理できますが、配布元と IaC Code Agent の実行場所が異なります。

## Skill を選ぶ

| Skill | 実行場所 | 適した用途 |
|---|---|---|
| `iac-code` | マシンにダウンロードされる検証済み IaC Code Runtime | iac-code プロジェクトの単体パッケージを使い、インストールと更新を自分で管理する。 |
| `alibabacloud-iac-code` | Alibaba Cloud Agent Skills ポータル向けにパッケージされた同じローカル Runtime | ポータルまたは `npx skills` で Alibaba Cloud Skills を管理する。 |
| `alibabacloud-ros-agent` | ROS StartChat API 経由で利用する Alibaba Cloud のホステッド ROS Agent | ローカル Runtime をダウンロードせず、リモート ROS Agent を利用する。 |

`iac-code` と `alibabacloud-iac-code` の機能は同じです。同じエージェントスコープではどちらか一方を
選んでください。両方を入れても機能は増えず、ルーティングが重複します。

`alibabacloud-ros-agent` は別のリモートサービス統合です。ローカル IaC Code とホステッド ROS Agent を
明示的に使い分ける場合は、ローカル版の一つと共存できます。

## 単体 Skill の入手

[安定版 iac-code-skill.zip をダウンロード](https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/skill/stable/iac-code-skill.zip)

この版は Skill ディレクトリを自分で管理する場合に適しています。初回に Runtime を取得し、
`~/.iac-code/` のモデルと Alibaba Cloud 設定を再利用します。詳細は
[IaC Code Skill のインストールと使用](./skill-integration.md)を参照してください。

## Alibaba Cloud ポータル版の入手

[Alibaba Cloud Agent Skills ポータル](https://skills.aliyun.com/)で正確な名前を検索するか、公式リポジトリから
インストールします。

```bash
npx skills add aliyun/alibabacloud-aiops-skills --skill alibabacloud-iac-code
npx skills add aliyun/alibabacloud-aiops-skills --skill alibabacloud-ros-agent
```

直接ダウンロードもできます。

- [`alibabacloud-iac-code` ZIP](https://skills.aliyun.com/api/public/skills/alibabacloud-iac-code/download) · [ソース](https://github.com/aliyun/alibabacloud-aiops-skills/tree/master/skills/developertools/ros/alibabacloud-iac-code)
- [`alibabacloud-ros-agent` ZIP](https://skills.aliyun.com/api/public/skills/alibabacloud-ros-agent/download) · [ソース](https://github.com/aliyun/alibabacloud-aiops-skills/tree/master/skills/developertools/ros/alibabacloud-ros-agent)

`npx skills` には Node.js 18 以降が必要で、対応エージェントとインストール範囲を対話的に選べます。
ZIP の場合は最上位 Skill ディレクトリをホストのユーザー用またはプロジェクト用 Skill ディレクトリへ展開します。

## 機能と設定の違い

ローカル Runtime の 2 配布は、通常会話と Pipeline、構成設計、ROS/Terraform テンプレート、料金見積もり、
スタック操作、デプロイ、質問、候補選択、権限・デプロイ確認に対応します。モデル設定が必要で、クラウド
リソースの照会・変更には Alibaba Cloud 認証情報も必要です。

`alibabacloud-ros-agent` は `ros:StartChat` で Alibaba Cloud ROS Agent に接続します。ローカル Runtime と
ローカルモデル設定は不要で、ホストの Alibaba Cloud ID を使います。必要最小限の RAM 権限を付与し、
明示的なリモートキャンセルには `ros:StopChat` も必要です。

どの版でも、変更やデプロイを承認する前にリソース、リージョン、影響、料金、権限を確認し、認証情報を
`SKILL.md`、プロンプト、プロジェクトファイルへ書かないでください。

## 関連ドキュメント

- [IaC Code Skill のインストールと使用](./skill-integration.md)
- [ホスト統合リファレンス](./skill-host-integration.md)
- [Alibaba Cloud 認証情報](../configuration/alibaba-cloud-credentials.md)
