<p align="center">
  <img src="../website/static/img/logo-with-front.png" alt="iac-code" width="200">
</p>
<p align="center">
  <em>自然言語インタラクションを通じて、Alibaba Cloud のリソースオーケストレーションテンプレート（ROS / Terraform）を生成・管理する AI 駆動の Infrastructure as Code（IaC）アシスタントです。</em>
</p>
<p align="center">
  <a href="https://github.com/aliyun/iac-code/actions/workflows/test.yml"><img src="https://github.com/aliyun/iac-code/actions/workflows/test.yml/badge.svg" alt="Test"></a>
  <a href="https://pypi.org/project/iac-code"><img src="https://img.shields.io/pypi/v/iac-code?color=%2334D058&label=pypi%20package" alt="PyPI Package"></a>
  <a href="https://pypi.org/project/iac-code"><img src="https://img.shields.io/pypi/pyversions/iac-code?color=%2334D058&label=python" alt="Python"></a>
</p>
<p align="center">
  <strong>Language</strong>: <a href="../README.md">English</a> | <a href="README.zh.md">中文</a> | <a href="README.es.md">Español</a> | <a href="README.fr.md">Français</a> | <a href="README.de.md">Deutsch</a> | 日本語 | <a href="README.pt.md">Português</a>
</p>

> **ドキュメント**：[https://aliyun.github.io/iac-code/](https://aliyun.github.io/iac-code/ja/)
<p align="center">
  <img src="../website/static/img/demo_en.gif" alt="iac-code demo" width="100%">
</p>

## インストール

```bash
pip install iac-code
```

## 使い方

初回使用時は、インタラクティブモードで `/auth` を入力して LLM プロバイダーと IaC クラウドサービスを設定してください。

### インタラクティブモード

直接実行してインタラクティブ REPL に入ります：

```bash
iac-code
```

### ノンインタラクティブモード

`--prompt` でワンショットプロンプトを渡します：

```bash
iac-code --prompt "VPC と 2 つの ECS インスタンスを作成"
```

stdin からの読み取りもサポートされています：

```bash
echo "OSS バケットを作成" | iac-code --prompt -
```

## お問い合わせ

| [DingTalk](https://qr.dingtalk.com/action/joingroup?code=v1,k1,ubm/77U7qRh/STFZUNBP26X4PNg2z6+uhiPcLGtDNfU=&_dt_no_comment=1&origin=11) | [Discord](https://discord.gg/qECFuFBwF) |
| :----------------------------------------------------------: | :----------------------------------------------------------: |
| [<img src="../website/static/img/qrcode-dingtalk.jpg" width="120" height="120" alt="DingTalk">](https://qr.dingtalk.com/action/joingroup?code=v1,k1,ubm/77U7qRh/STFZUNBP26X4PNg2z6+uhiPcLGtDNfU=&_dt_no_comment=1&origin=11) | [<img src="../website/static/img/qrcode-discord.jpg" width="120" height="120" alt="Discord">](https://discord.gg/qECFuFBwF) |
