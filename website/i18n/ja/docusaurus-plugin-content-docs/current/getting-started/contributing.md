---
title: コントリビュート
description: ローカル環境のセットアップと IaC Code への貢献方法。
---

# コントリビュート

## 前提条件

- Python 3.10+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## セットアップ

```bash
git clone https://github.com/aliyun/iac-code.git
cd iac-code
make install
```

`make install` はすべての依存関係をインストールし、pre-commit フック（コミット時の lint・format チェック）を設定します。

## 開発ワークフロー

デバッグモードで実行：

```bash
make dev
```

テストを実行：

```bash
make test           # デフォルトの Python バージョン
make test PY=3.12   # 特定のバージョン
make test PY=all    # サポートされている全バージョン（3.10–3.14）
```

コード品質：

```bash
make lint      # ruff check + ty check
make format    # ruff format
```

カバレッジ：

```bash
make coverage
```

## プロジェクト構成

```
src/iac_code/       # ソースコード
tests/              # テスト
website/            # ドキュメントサイト（Docusaurus）
```

## 変更の提出

1. リポジトリをフォークし、フィーチャーブランチを作成します。
2. テスト付きで変更を行います。
3. `make format` を実行し、`make lint` と `make test` が通ることを確認します。
4. `main` ブランチに対して Pull Request を作成します。
