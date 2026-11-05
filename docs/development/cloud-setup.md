# クラウド開発環境セットアップ

更新日: 2026-11-05 / 対象: ロジック検証（バックテスト・デプロイは対象外）

## 前提

- GitHub上の作業ブランチをクラウド環境でcloneする。
- LinuxとPython 3.11を使用する。`.python-version`とdevcontainerも3.11に統一している。
- プロジェクト専用の`.venv`へ依存を導入する。
- 現在のアプリは公開APIキーを必要としない。ただし株価取得時にYahoo Financeへの外向き通信が必要である。
- テストは固定データを使い、外部通信を行わない。

## 初回セットアップ

リポジトリのルートで実行する。

```bash
bash scripts/setup.sh
```

devcontainerまたはGitHub Codespacesでは`postCreateCommand`が同じスクリプトを実行する。OpenAIのクラウド環境でセットアップコマンドを指定できる場合も、リポジトリをcloneした後にこの1コマンドを指定する。

スクリプトはPython 3.11以外を検出すると停止する。既存`.venv`のPython版が異なる場合も、自動削除せず停止するため、作業内容を確認してから環境を作り直す。

## 検証

```bash
bash scripts/check.sh
```

このコマンドは、外部通信なしの単体テストとPythonファイルの構文コンパイルを実行する。バックテストは実行しない。

## Streamlitの手動起動

```bash
.venv/bin/python -m streamlit run app.py \
  --server.address 0.0.0.0 \
  --server.port 8501 \
  --server.headless true
```

ヘルスチェック:

```bash
curl --fail http://127.0.0.1:8501/_stcore/health
```

クラウド側のポート転送機能で8501番を開く。devcontainerはポートを通知するだけで、アプリを自動起動しない。CORSやXSRF保護も無効化しない。

## ネットワーク制限

セットアップ時は少なくともPyPIの配布先（`pypi.org`、`files.pythonhosted.org`）へのHTTPS接続が必要である。実データのスモーク確認にはYahoo Financeの配信先への接続が必要で、現時点の代表例は`query1.finance.yahoo.com`と`query2.finance.yahoo.com`である。Yahoo側の配信先は変更され得るため、失敗時に無制限な通信を許可せず、実際の接続先をログで確認して許可リストを更新する。

ネットワークを許可できない環境でも`bash scripts/check.sh`は実行できる。ただし、その前に依存導入済みの`.venv`が必要である。株価取得の疎通失敗と戦略ロジックの不成立は別の結果として扱う。

## 依存の再現性

`requirements.txt`はPython 3.11互換のメジャー版範囲を制約している。完全な再現が必要な評価段階では、Linux/Python 3.11上で検証した解決済みバージョンを別のlock成果物へ固定する。現段階ではバックテストを行わないため、未検証の環境固有lockを作らない。

OpenAI-hosted environments一般では、依存パッケージの版指定と順序付きsetup commandが提供されている。これはCodexの画面設定そのものを断定する根拠ではなく、環境再現方針の参考である。公式資料: [OpenAI-hosted sandboxes](https://developers.openai.com/api/docs/guides/agents-api/environments/openai-hosted)

## 実施しないこと

- バックテスト、損益計算、パラメータ最適化
- Streamlitへのデプロイ、ポートの外部公開
- 本番秘密情報のコミット
- 実注文や証券口座への接続

GitHubへは既定ブランチではなく作業ブランチをpushし、クラウド側もそのブランチを選択する。
