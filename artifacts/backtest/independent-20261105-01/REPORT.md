# 独立仮想取引の実装・検証記録

2026年11月5日。承認仕様はdocs/specs/independent-backtest-v1.mdの1.0-approved、R-IV01〜R-IV06。

## 実施結果

各買いシグナルを同額元本に正規化して独立評価する機能を追加した。同一銘柄の保有期間が重なるシグナルも評価し、既存の翌営業日寄付、出口A/B、損切り、手数料、スリッページを適用する。

- 関連回帰テスト30件成功。tests-targeted.logへ保存。
- 全自動テスト68件成功。tests-full.logへ保存。
- 独立評価の結果ZIPを画面で表示するテストを含めた8件成功。tests-viewer.logへ保存。
- 通常CLIで70銘柄・2019〜2025年の固定合成データを使い14条件完了。synthetic-run/とsynthetic-run.zipへ保存。合成データによる機能検証として参照する。
- 取得済みYahoo Financeデータの再実行は、8306.Tの価格範囲不整合を検出して停止。market-run-original/run.jsonとmarket-execution.logへ保存。実データ成績は未算出。

## 実行方法

```powershell
.\.venv\Scripts\python.exe scripts/backtest.py --mode exploratory --evaluation independent --data <検証済み入力ディレクトリ> --output <新しい結果ディレクトリ> --zip
```

REPORT.md、summary.csv、各条件の全取引・未決済・見送り・銘柄別・購入年別・資産区分別集計、実行条件、コードと入力のハッシュを保存する。結果ZIPは既存のバックテスト比較画面から参照できる。

## 実データの再開条件

取得済み原本を保持する価格補正案はdocs/specs/backtest-data-quality-v1.mdの1.0-proposed、R-DQ01〜R-DQ05。承認を確認後、別の派生入力と結果保存先で再実行する。

固定利確・追随利確の追加比較は今回の既存出口A/Bと分けて仕様確認を継続する。
