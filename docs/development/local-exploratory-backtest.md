# ローカルでの探索バックテスト

対象: サブブランチ`codex/search-python314`、日足、現在の70銘柄、2021-01-04〜2025-12-30、7設定×出口A/B。検索の判定関数はそのまま利用する。

## このモードの意味

検索と同じ調整済みOHLCを疑似約定にも使い、仮想1株単位で数量を決める。分割・配当は価格調整へ含まれるものとし、配当の現金加算と分割による保有株数変更は行わない。実際の単元・配当入金日・名目価格で運用した成績ではない。売買停止や値幅制限、現在の銘柄だけを使う生存者バイアス等も完全には再現しない。本実装・改善用の探索データとして使用する。

初期資金1000万円、最大5銘柄、各20%以内、手数料片道0.1%・スリッページ0.05%、翌営業日始値約定と承認済み出口は維持する。保有中の買い増しはしない。期末は建玉を強制決済せず時価評価する。

## 1. ブランチを取得

既存のローカル変更がある場合は先に保存し、無理に上書きしない。

```bash
git status
git fetch origin
git switch codex/search-python314
git pull --ff-only origin codex/search-python314
```

初回取得なら:

```bash
git clone --branch codex/search-python314 https://github.com/yuhei-koyama/streamlit-stock-screener.git
cd streamlit-stock-screener
```

以降はすべてリポジトリのルートで実行する。Python 3.14を事前にインストールする（3.11も対応）。実データ取得には通常のインターネット接続が必要。クラウドの許可リストをローカルへ設定する必要はないが、Yahoo側の制限はローカルでも起こり得る。

## 2. Windows PowerShell

`.venv`がなければ作成する。既存の`.venv`がある場合は上書きせず、`python.exe --version`で3.11/3.14を確認して使用する。

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade "pip>=24.3,<27"
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

アクティベートは不要。各段階が成功したことを確認してから次へ進む。

```powershell
# 取得: 2019年からのウォームアップを含む各銘柄の履歴を保存
.\.venv\Scripts\python.exe scripts/collect_backtest_source.py --output artifacts/backtest/source-001

# 準備: 保存済みデータのみ使用。追加のネットワークアクセスなし
.\.venv\Scripts\python.exe scripts/prepare_exploratory_data.py --source artifacts/backtest/source-001 --output artifacts/backtest/data-001

# 実行: 固定14条件。結果ZIPも作成
.\.venv\Scripts\python.exe scripts/backtest.py --mode exploratory --data artifacts/backtest/data-001 --output artifacts/backtest/run-001 --zip

# 結果表示用Streamlitを起動
.\.venv\Scripts\python.exe -m streamlit run app.py
```

## 3. Linux / macOS

```bash
bash scripts/setup.sh
bash scripts/check.sh
.venv/bin/python scripts/collect_backtest_source.py --output artifacts/backtest/source-001
.venv/bin/python scripts/prepare_exploratory_data.py --source artifacts/backtest/source-001 --output artifacts/backtest/data-001
.venv/bin/python scripts/backtest.py --mode exploratory --data artifacts/backtest/data-001 --output artifacts/backtest/run-001 --zip
.venv/bin/python -m streamlit run app.py
```

## 4. 取得失敗・再実行

取得は逐次実行し、銘柄間に短い間隔を入れる。最初の取得失敗で停止し、進捗をcollection.jsonへ記録する。429等のレート制限時は続けて何度も実行せず、時間を置いて原因を確認する。

取得済みキャッシュを再利用して再開する場合は、元の取得コマンドに`--resume`を付ける。

```powershell
.\.venv\Scripts\python.exe scripts/collect_backtest_source.py --output artifacts/backtest/source-001 --resume
```

Linux/macOSでは先頭を`.venv/bin/python`へ置き換える。保存済みファイルのハッシュを確認し、一致する取得済み銘柄は再取得しない。改変・欠損・保存途中の未記録ファイルは黙って使わず停止する。データを作り直す場合はsource-002などの新しい名前を使う。

準備処理は70銘柄の取得完了を必要とする。データ不足銘柄を黙って除外して比較しない。準備済みdata-001は何度でも読み込み可能。別の検証結果を保存する場合はrun-002等を指定する。データ/結果/ZIPは上書きしない。取得データをGitHubへ追加しない（artifacts/は無視対象）。

## 5. 結果を見る

ローカルで起動した画面またはサブアプリの「バックテスト比較」で`artifacts/backtest/run-001.zip`を選ぶ。ZIP直下のrun.json、summary.csvと各条件の詳細を読む。新しいZIPを自分で作る必要はない。

CAGR・資産推移・最大ドローダウン・年次リターン・売買履歴・見送り注文・期末建玉を確認できる。各run.jsonに`execution_mode: exploratory`、データ区分、設定、入力/コードハッシュ、制約を記録する。合成テストのZIPはsyntheticと表示し、市場の収益性と区別する。単一期間の探索結果から将来の利益やオルカン超過を結論付けない。

## 確認状況

この追加実装はLinux/Python 3.14上の固定合成データ・モック通信で検証する。実市場の取得/バックテストとWindows/macOS実機での手順実行は今回クラウドでは行わない。既存の厳密モードは`--mode strict`（既定）として残り、名目価格・実単元・企業行動の確認条件を維持する。
