# LBR Anti 日本株スクリーナー

LBR Antiのロジックを、バックテスト前に検証するためのStreamlitアプリです。東証上場の日本個別株・国内ETFについて、次の2定義の指標、中間状態、setup、hookを表示します。

- `lbr_anti_stochastic_v1`: 7/4/10ストキャスティクス版
- `lbr_anti_310_adaptation_v1`: SMA 3/10/16適応版

売買注文、entry/exit、損益計算、バックテストはまだ実装していません。ロジック仕様は [docs/specs/lbr-anti-logic.md](docs/specs/lbr-anti-logic.md)、開発ルールは [AGENTS.md](AGENTS.md) を参照してください。

## クラウド/Linuxでのセットアップ

Python 3.11環境で実行します。

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
