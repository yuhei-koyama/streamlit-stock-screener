# LBR Anti 日本株スクリーナー

LBR Antiのロジックを、バックテスト前に検証するためのStreamlitアプリです。東証上場の日本個別株・国内ETFについて、次の2定義の指標、中間状態、setup、hookを表示します。

- `lbr_anti_stochastic_v1`: 7/4/10ストキャスティクス版
- `lbr_anti_310_adaptation_v1`: SMA 3/10/16適応版

売買注文、entry/exit、損益計算、バックテストはまだ実装していません。ロジック仕様は [docs/specs/lbr-anti-logic.md](docs/specs/lbr-anti-logic.md)、開発ルールは [AGENTS.md](AGENTS.md) を参照してください。

## クラウド/Linuxでのセットアップ

Python 3.14（推奨）または3.11環境で実行します。既存の3.11仮想環境は維持できます。

```bash
bash scripts/setup.sh
bash scripts/check.sh
```

## アプリ起動

```bash
.venv/bin/python -m streamlit run app.py \
  --server.address 0.0.0.0 \
  --server.port 8501 \
  --server.headless true
```

詳しいクラウド設定、ネットワーク要件、ヘルスチェックは [docs/development/cloud-setup.md](docs/development/cloud-setup.md) に記載しています。

## 開発状態

現在はロジック検証フェーズです。Yahoo Financeの調整済み日足は画面確認用であり、正式なバックテスト用データとして承認されたものではありません。バックテストは、ロジック確認後に仕様を別途承認してから実装・実行します。

## 追加の銘柄検索
Streamlit起動後、サイドバーの「銘柄検索」ページからミネルヴィニ派生版・RSI平均回帰を選択できます。既存のAntiページは維持しています。追加ページは最大100銘柄の手動検索で、出口判定やバックテストは行いません。判定条件は [追加検索仕様](docs/specs/search-v1.md) を参照してください。成立条件は収益性を保証しません。


## Windows / Python 3.14（PowerShell）

リポジトリのルートで実行します。新規環境の手順です。既存の`.venv`は上書きせず、別フォルダーで作成してください。

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade "pip>=24.3,<27"
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
.\.venv\Scripts\python.exe -m streamlit run app.py
```

アクティベートは不要です。起動後、サイドバーの「銘柄検索」から追加検索を開けます。Linux/Python 3.14での検証とWindows実機の確認状況は検証記録を参照してください。

検索対象は日本個別株50銘柄・国内ETF20銘柄・合計70銘柄のプリセットから選べます。コード欄を編集して独自の対象も指定できます。候補は手動管理の代表例で、東証全銘柄を網羅する一覧ではありません。

## リンダ系の版別ページ

サイドバーで以下を個別に開けます。既存のAntiトップページと「銘柄検索」も維持しています。

| ページ | 計算・判定 |
| --- | --- |
| TradingView添付版 | EMA3/10/16のクロスとストキャス7/3/10 Hookの同時成立 |
| Anti 310適応版 | SMA3/10/16でのSetup・Hook |
| Anti 7410版 | ストキャス7/4/10でのSetup・Hook |
| 初期試作版 | 初期mainのEMA3−EMA10・SMA16 Signal・Histogram反転＋価格SMA16 |

各ページには最新足のBUY/SELL候補フィルター、発生日履歴、チャート、中間値、版ID・パラメータ付きCSVがあります。AntiのBUY/SELL候補はHook Long/Shortで、価格によるentryではありません。試作版は原典準拠ではなく比較用です。詳細は[版別仕様](docs/specs/linda-versions.md)を参照してください。

当日足除外は初期有効で、引け後も当日を除きます。解除すると場中未確定足を含み得ます。自動確定足判定とTradingView実データでの一致確認は未実施です。将来のバックテスト用に共通関数`stock_screener.linda_versions.evaluate_version`を利用できますが、今回の変更でバックテストエンジンや出口条件は追加していません。

## バックテスト（承認済み14条件）

検索と同じ関数を直接使う7設定×出口A/Bの共通エンジンを追加しました。翌営業日寄付・資金/単元制約・損切り・分割/配当会計・現金込みCAGR等に対応します。実行には検証済みの名目価格・当時の単元・企業行動データが必要です。詳細と現時点の阻害条件は[実行手順](docs/development/backtest-run.md)に記録しています。

現時点で完了したのは合成データ検証です。実市場のバックテスト結果はまだありません。サイドバーの「バックテスト比較」は保存済みの結果ZIPを表示する画面で、自動で価格取得や検証を開始しません。
