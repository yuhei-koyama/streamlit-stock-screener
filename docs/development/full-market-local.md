# 全市場のローカル補助診断手順

## 現在の完了状態

2026年11月7日。現行一覧v4は個別株3,646・ETF381、計4,027銘柄。入力準備と有限資金560条件の実行・集計・保存を完了した。結果は[日本語報告書](../../artifacts/finite-capital-universe-v4-20261107-01/primary-analysis-01/REPORT.md)を参照する。以下の旧対象件数・準備未完了・停止の記載は各段階の記録として保持する。現在の条件は[主仕様](../specs/finite-capital-primary-v1.md)を参照する。

## 現時点の状態

更新日2026年11月7日。このCLIは銘柄ごとの独立取引の補助診断を処理する。有限資金の主検証と共通現金会計は[主仕様](../specs/finite-capital-primary-v1.md)に定義し、実装課題として管理する。

JPX公式上場一覧.xlsxをローカルで取得し、2026年10月末の内国株候補3,693件を確認済み。ETF全件の日付・範囲の照合と全市場母集団の固定は未完了。既存50銘柄の準備と全市場の完了状態を区分する。

## 対象と比較条件

- JPX最新の東証上場銘柄一覧の、プライム/スタンダード/グロース内国株式。
- JPXの国内上場ETF一覧で照合できるETF。ETF・ETN混在区分を丸ごとETFとはしない。
- 外国個別株・ETN・REIT・インフラファンド・TOKYO PRO Marketは対象外。ETFには海外資産型や商品型も含む。
- 現在の一覧を両期間に固定する。2016〜2020年、2021〜2025年を分け、各期間の準備開始日は2014年、2019年に固定する。期間内の履歴なし/ready不足を明示する。
- 検索の共有7入口はそのまま。入口評価は5/10/20/40/60営業日の35条件。出口評価は既存A/B/C/D×3/5/8/10/12%の140条件。最適化しない。
- 同額元本の独立仮想取引、調整価格、配当別加算なし、片道手数料0.1%・スリッページ0.05%。年率の資産運用成績ではない。
- オルカン実ファンドの設定前を含むため、この全期間比較でオルカンを代替系列で埋めない。

## 1. 一覧の取得と固定

ブラウザーで公式原本を取得し、ローカルへ保存する。

- [JPX 東証上場銘柄一覧](https://www.jpx.co.jp/markets/statistics-equities/misc/01.html)のExcel（通常data_j.xls）。
- [JPX ETF一覧](https://www.jpx.co.jp/equities/products/etfs/issues/01.html)。ETFだけのコードを含む一覧を使う。

ETF一覧は`コード`または`銘柄コード`列を持つ.xls/.xlsx、またはUTF-8 CSVとして用意する。CSVへ抽出する場合は公式ETF一覧との全件照合と日付確認を行い、原本も別途保持する。JPX一覧側は`日付,コード,銘柄名,市場・商品区分`を読み込む。一覧の日付がなければ`--source-date YYYY-MM-DD`で確認済み日付を指定する。ETF一覧とJPX一覧で対象コードが整合しなければ停止する。上場一覧の列と候補件数は確認済み。ETF全件との対象・日付の整合を確認してから固定する。

Windows PowerShellの例。リポジトリ直下で、以前作成したPython環境を使う。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-research.txt
.\.venv\Scripts\python.exe scripts/full_market_backtest.py freeze --jpx-file C:/data/data_j.xls --etf-file C:/data/etf_codes.csv --output artifacts/full-market-001
```

Linux/macOSは先頭を`.venv/bin/python`へ置き換える。.xlsの読込にはxlrd、.xlsxにはopenpyxlを使う。2026年11月6日に現行のJPX上場一覧.xlsxを取得・読込確認した。ETFの対象と日付の整合は原本ごとに確認する。

`universe.csv`、元ファイルのコピーとハッシュ、日付・銘柄数・採否理由を保存する。原本とuniverse.csvは途中で変更しない。実行開始時に件数と対象を確認する。株式の全過去母集団ではなく、現在の一覧による生存者バイアス付き探索である。

## 2. 少数バッチで取得・時間と容量を確認

```powershell
.\.venv\Scripts\python.exe scripts/full_market_backtest.py collect --root artifacts/full-market-001 --limit 10 --pause 1
```

RS参照の1306.Tを先に取得する。各銘柄は2014〜2025年を1回保存し、両期間で使う。保存済みのハッシュが一致するファイルは再取得しない。続けるときも同じコマンドを実行し、未取得分だけ処理する。慣れてから`--limit 100`等へ増やす。429・接続エラーでは最初の失敗で止まり、待機して原因を確認する。404の履歴なしも記録し、全市場成功に読み替えない。無制限の自動リトライは行わない。

数千銘柄では取得・計算・保存容量が大きくなる。最初の10銘柄で所要時間とcache容量を確認してから進める。大量の同時アクセスは行わない。

## 3. 入口の比較を先に実行

```powershell
.\.venv\Scripts\python.exe scripts/full_market_backtest.py run --root artifacts/full-market-001 --phase entry --limit 10
.\.venv\Scripts\python.exe scripts/full_market_backtest.py summary --root artifacts/full-market-001 --phase entry
```

1回最大10銘柄を処理し、同じコマンドで続きから進む。各銘柄の原票はentry.zipへまとめ、多数の小ファイルを作らない。基準は同じ銘柄・購入年の全ready日を同じ保有期間で評価した平均。シグナル件数で加重した入口のタイミング差を比較する。ETFと個別株は別集計する。

## 4. 既存の出口条件も追試

```powershell
.\.venv\Scripts\python.exe scripts/full_market_backtest.py run --root artifacts/full-market-001 --phase exits --limit 10
.\.venv\Scripts\python.exe scripts/full_market_backtest.py summary --root artifacts/full-market-001 --phase exits
```

140条件×2期間のため入口評価より重い。小バッチで時間と容量を確認する。原票はexits.zipへ保存し、入口評価とは別の完了状態を持つ。原票・入力・コードのハッシュを保存し、違うコード版を混ぜた再開を拒否する。コードを更新した場合は新しい作業先へ一覧を固定し直す。

## 品質問題と未完了の扱い

- 高安の逆転・非正価格・欠損などは自動補正せず、その銘柄をblocked_data_qualityとして保存する。
- 既存197行の追加承認と固定照合は追加期間の専用準備へ適用する。全市場の新しい行や欠損・非正価格は個別確認へ戻す。ETF1321の負価格は未解決。
- summaryのcoverage.csvに全対象のpending/blocked/completedを残す。未処理・失敗がある集計はpartialであり、全市場を完了した結果ではない。
- 履歴が期間内に存在しない場合も0件と理由を記録する。正の成績の銘柄だけ残して対象を変えない。
- 全銘柄を処理しても、生存者バイアス・重複取引・調整価格・仮想数量・流動性近似は残る。

新しい原票は自動でGitへ追加しない。まずローカルで容量と内容を確認し、共有する成果物を選ぶ。既存の14条件向けStreamlit比較UIにこの全市場集計をアップロードする方式は今回追加していない。summary CSVと原票から調べる。

## 休場日区分（2026年11月7日）

`run`は価格キャッシュを保持したまま、銘柄とRS参照の休場日を指標計算前に除く。原票は各銘柄の`excluded_nontrading_rows.csv`とルートの`reference_nontrading_rows.csv`へ保存する。対応カレンダーは2014〜2025年で範囲外は停止する。営業日の個別銘柄の出来高ゼロは保持する。詳細は[取引日仕様](../specs/trading-calendar-v1.md)。旧コードで処理済みの作業先は混在再開せず、新規の作業先を使う。

## フェーズ別の依存記録（2026年11月7日、F01承認済み）

再開・集計時は銘柄価格だけでなく1306.Tの価格、コード、カレンダー規則も照合する。`status.json`の`phase_records`へ各フェーズの依存入力と成果物ハッシュを保存し、別フェーズの実行で過去の記録を付け替えない。依存関係が異なる場合とフェーズ記録がない旧形式は停止し、新しい作業先での再計算が必要になる。既存結果を手作業で新しい依存記録へ付け替えない。

## 現行仕様と履歴

カレンダーはv2。新規実行・再開・集計の規則は[カレンダー仕様](../specs/trading-calendar-v1.md)と[主仕様](../specs/finite-capital-primary-v1.md)へ対応付ける。[整理前の手順](../history/f05-20261107/full-market-local-before.md)は履歴として保持する。
