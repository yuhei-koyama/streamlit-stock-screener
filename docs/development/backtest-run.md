# 固定14条件のバックテスト手順

正本: [承認済み条件](../specs/backtest-approved-v1.md)。検索の判定式を複製せず、`backtest_signals.screening_signals`が既存の`evaluate_version` / `search_minervini` / `search_rsi`を直接呼ぶ。パラメータはページの初期値に固定する。検索と同じデータ・履歴・パラメータなら同じ各足の買い候補となる。UIの当日足除外は2025年末までの歴史データには影響しない。

## 実行と結果

リポジトリ直下、準備済みPython環境から実行する。

```bash
python scripts/backtest.py --data /path/to/verified-dataset --output artifacts/backtest/run-001
```

`--output`には新規ディレクトリを指定し、前の結果を上書きしない。7設定×出口A/Bを順に実行する。最適化は行わない。全14条件が完了した場合だけ`run.json`が`completed`となり`summary.csv`が保存される。失敗時の部分的な結果は正式な比較結果にしない。

各条件にdaily、trades、rejections、positions、annual_returns、monthly_returns、asset_groups、metrics、configを保存。run.jsonには実/合成区分、設定、入力/コードハッシュ、コミットと作業ツリーの状態、警告を保存する。合成データの結果を市場の収益性として表示しない。

ZIPにまとめる際はZIPの直下にrun.jsonとsummary.csvが来るようにする。サブアプリの「バックテスト比較」ページにZIPをアップロードすると、CAGR・資産曲線・MDD・年次リターン・取引・見送り・未決済建玉を確認できる。このページはAPI取得やバックテストを自動実行しない。

## データ契約

ディレクトリには以下を用意する。現在の70銘柄すべてが必要で、一部銘柄欠落を黙って除外しない。

- `calendar.csv`: `Date`列にJPX営業日を昇順・重複なしで記録。ウォームアップを含むすべての入力日と2021-01-04〜2025-12-30を覆う。
- `signals_7203.T.csv`等: `Date,Open,High,Low,Close,Volume`。検索と同じ調整済み系列。欠損を後方から埋めない。2019年以降の利用可能な履歴を含める。
- `execution_7203.T.csv`等: `Date,Open,High,Low,Close,Volume,LotSize,Split,Dividend,DividendPayDate`。
  - OHLCはその日に実際に売買された円建て名目価格。Yahooの分割調整済み履歴と混同しない。
  - LotSizeはその日有効な売買単位（正整数）。株式/ETFから推測しない。
  - Splitは当日の新株数/旧株数。分割なしは1。分割端数の現金清算は未対応で停止する。
  - Dividendは権利落ち日の新株数基準1株当たり金額。なしは0。正値なら確認済みDividendPayDateが必要。
  - 配当は権利落ち前から保有していた株数に対応して未収計上し、支払日以降最初の営業日に現金化する。これは税引前。
  - 任意のCanBuy/CanSell（True/False）で停止・約定不可を表す。なければ正の出来高と有効OHLCで約定可能と仮定し、限界を警告する。
  - signalsとexecutionの各銘柄の日付は完全一致が必要。
- `manifest.json`: 以下の形。assetsには70銘柄すべてを記載。

```json
{
  "kind": "market",
  "provider": "実際の提供元・データ版",
  "retrieved_at": "実際の取得日時",
  "execution_price_basis": "nominal_unadjusted",
  "actions_verified": true,
  "lot_sizes_verified": true,
  "assets": [{"ticker": "7203.T", "asset_class": "stock"}]
}
```

検証していないデータをtrueと宣言しない。合成データはkindをsyntheticとする。現在の母集団・銘柄分類はuniverse.pyで固定し、生存者/選択バイアスを明示する。

## ソース取得の補助

```bash
python scripts/collect_backtest_source.py --output artifacts/backtest/source-001
```

70銘柄の2019〜2025年を各1回取得して保存する。最初の取得失敗で停止する。既存出力は上書きしない。auto_adjust=Falseのソースと、yfinance自身のauto_adjust処理による検索用系列を保存する。これは名目約定価格・単元・企業行動を検証済みのデータセットを自動生成するコマンドではない。配当支払日や過去単元等は別の確認済み情報が必要。

## 現在の実行状況（2026-11-05）
- 固定合成データの全体53テスト・構文検査が合格。その後、寄付ギャップ売却後の資金利用と入力検証の2テストを追加し、変更関連17件が再度合格（合格確認は計55件）。初回のコスト期待値の算術誤りを8.08から8.04へ訂正後に全件成功。
- 7戦略×2出口の一括処理も小さい固定合成データで完走した。これは実データ成績ではない。
- Yahoo chartへの限定疎通は429、検索と同じyfinanceの限定取得はfc.yahoo.comへのCONNECT 403で失敗。価格の一括取得は行っていない。
- fc.yahoo.comを許可する設定ドラフトを保存済みでユーザーも反映を許可したが、確認時の実適用済み許可リストはquery1/query2のみ。ツールに実適用/Publish機能はない。
- 名目約定価格・過去単元・企業行動の必要データも未入手。実市場の14条件のバックテストは未実行で、CAGRやランキングは作成していない。
- 次はネットワーク設定の適用を確認し、保存可能なソースを一度取得して上記の必要データを照合する。不足は推測せず報告する。mainは更新しない。

## 追記: 承認済みのローカル探索モード

調整済み価格・仮想1株単位・配当別加算なしの`--mode exploratory`を追加した。上記の名目価格・実単元に関する厳密要件は既定のstrictモードについての説明であり、探索モードでは別契約を明示して使う。具体的なローカル操作は[ローカル探索手順](local-exploratory-backtest.md)、仕様は[探索モード](../specs/exploratory-backtest-v1.md)を参照。
