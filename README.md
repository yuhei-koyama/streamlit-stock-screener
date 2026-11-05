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
Streamlit起動後、サイドバーの「銘柄検索」ページからミネルヴィニ派生版・RSI平均回帰を選択できます。既存のAntiページは維持しています。追加ページは最大20銘柄の手動検索で、出口判定やバックテストは行いません。判定条件は [追加検索仕様](docs/specs/search-v1.md) を参照してください。成立条件は収益性を保証しません。


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
