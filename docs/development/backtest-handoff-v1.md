# 別端末でのバックテスト検証の継続

更新: 2026年11月6日。共有ブランチは`codex/search-python314`。

## 最新の主検証方針を先に読む

[有限資金・仮想端数株の主検証方針](../specs/finite-capital-primary-v1.md)を優先して読む。今回、会話で策定した方針を文書保存した。新方式の実装・実データ検証は未実施。以下の保存済み結果は従来の独立仮想取引であり、有限資金・重複購入なしの結果ではない。

初期1,000万円・購入枠10万円の整理、仮想端数株、保有中の追加購入なし、共有入口、既存出口A〜D、対象・期間、未決定の運用細部と実装差分を上記文書に記載した。ローカルの会話へ記憶が自動転送されるとは想定せず、取得後にAGENTS.mdと上記文書を読む。

## 取得

初めて取得する端末で実行する。

```bash
git clone --branch codex/search-python314 --single-branch https://github.com/yuhei-koyama/streamlit-stock-screener.git
cd streamlit-stock-screener
```

既存のチェックアウトでは、作業内容を確認してから対象ブランチへ移動し、取得する。

```bash
git fetch origin
git switch codex/search-python314
git pull --ff-only origin codex/search-python314
```

## 保存済みの到達点

- 主報告書は[入口・出口診断](../../artifacts/backtest/entry-exit-diagnosis-20261105-01/REPORT.md)。出口140条件・固定保有35条件を保存した。
- 日本個別株50銘柄を主集計、東証ETF20銘柄を別集計。7入口、固定保有5/10/20/40/60営業日、初期損切り3/5/8/10/12%、A/B/C/D期限なし。C/Dの診断用値幅は8%固定。
- 自動テスト99件成功。既存8%条件28件一致、固定保有窓1,050例、回復原票700ファイルと出口全条件の保存集計を監査済み。
- 既に確認済み期間の探索。現行の固定銘柄一覧、重複取引、調整価格・仮想端株等の制約は報告書へ記録済み。
- 米国株案は取り下げ済み。FXも対象外。今後の説明文書は日本語で作成する。

## 引き継ぐデータ

`artifacts/backtest/`に次を含める。取得済みのデータから再開できる。

| ディレクトリ | 内容 |
| --- | --- |
| yahoo-20261105-01 | Yahoo取得原本、準備データ、補正前の不整合・実行記録 |
| independent-20261105-01 | 初期の独立評価、合成検証、実市場データ検証の失敗記録 |
| independent-yahoo-20261105-02 | 承認済み63行補正データ、期限ありA/B、補正監査 |
| all-country-comparison-20261105-01 | 公式オルカンCSVと取得元メタデータ、期限あり比較 |
| holding-period-comparison-20261105-01 | 期限なしA/B、期限有無の比較、監査 |
| ratchet-exits-20261105-01 | C/Dの既存8%条件、注文履歴、比較、監査 |
| entry-exit-diagnosis-20261105-01 | 最新175条件、原票、全集計、報告書、コードスナップショット、監査 |

重複するZIPはGitの対象外とし、展開済みの原票を共有する。取得データと保存成果物は`.gitattributes`の`-text`指定で改行変換を防止し、既存SHA-256を保持する。Git LFSの設定・取得操作は不要。原票を含むため初回取得には時間と保存容量が必要になる。

## 環境準備と自動テスト

Python 3.14を推奨し、既存の3.11も対応対象とする。既存環境の導入済み版は各成果物の`environment.txt`等も参照する。

Windows PowerShellで新しい仮想環境を用意する場合。

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -q
```

Linuxでは既存のセットアップを利用する。

```bash
bash scripts/setup.sh
bash scripts/check.sh
```

## 最新結果の再監査

リポジトリのルートから実行する。外部の価格取得は不要。

```powershell
.\.venv\Scripts\python.exe scripts/verify_entry_exit_diagnosis.py artifacts/backtest/entry-exit-diagnosis-20261105-01
```

LinuxではPythonのパスを`.venv/bin/python`へ置き換える。この監査は報告書・監査JSON・コードスナップショットを再生成する。共有済みファイルへの変更は`git diff`で確認する。

保存済み全ファイルのハッシュを、再監査の前に検証する場合。

```powershell
.\.venv\Scripts\python.exe -c "import json,hashlib; from pathlib import Path; r=Path('artifacts/backtest/entry-exit-diagnosis-20261105-01'); m=json.loads((r/'file_hashes.json').read_text(encoding='utf-8')); bad=[n for n,h in m.items() if hashlib.sha256((r/n).read_bytes()).hexdigest()!=h]; print('mismatches',len(bad)); assert not bad"
```

## 再実行

既存成果物の上書きを避け、新しい出力先へ保存する。

```powershell
.\.venv\Scripts\python.exe scripts/diagnose_entry_exit.py --data artifacts/backtest/independent-yahoo-20261105-02/data --fund-csv artifacts/backtest/all-country-comparison-20261105-01/253425-original.csv --output artifacts/backtest/entry-exit-diagnosis-reproduction
.\.venv\Scripts\python.exe scripts/verify_entry_exit_diagnosis.py artifacts/backtest/entry-exit-diagnosis-reproduction
```

出力の追加共有は`.gitignore`の許可対象へ明示追加する。最新診断は175条件のため、既存14条件用の比較UIは対象に含めない。報告書とCSVから参照する。

## 次の検討

入口の計算式は固定のまま検証済み。ATRストップ、入口遅延、未使用期間・母集団の追加は候補として記録されており、具体条件の承認後に着手する。仕様の正本は[入口・出口診断](../specs/entry-exit-diagnosis-v1.md)と[要求記録](../specs/requirements.md)。

## 共有前の検証

2026年11月5日に自動テスト99件が成功。共有する原票4,011ファイルをGitインデックスから読み戻し、ローカル原票とのSHA-256一致を確認した。最大の単一ファイルは24,207,374バイト。入力・原票の改行と空白は原本通りに保存する。共有対象の展開済みデータは合計約1.67GB。

## 2016〜2020年の追加検証準備（2026年11月6日）

`artifacts/backtest/period-2016-2020-20261106-01/REPORT.md`に取得原本と停止理由を保存。70銘柄の2014〜2020年を取得済みだが、個別株197行の微小差とETF1321の負調整価格245行を新たに検出したため、R-DQ05に従って補正/実市場検証の実行は承認待ち。結果が完了したとは扱わない。個別株50銘柄を先行する補正と実行の手順は当該報告書に記載している。
