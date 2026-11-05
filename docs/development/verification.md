# ロジック検証とクラウド引継ぎ

確認日: 2026-11-05

## 実装済み

- SMA 3/10/16適応版と7/4/10ストキャスティクス版の指標、setup、hook。
- 共通Python判定、理由コード、調整本数と交差設定、Streamlit表示。
- Linux/Python 3.11用セットアップ、devcontainer、テストコマンド。
- 旧Cline仕様・ロジックと重複フォルダ・メモの削除。

## 確認結果

Windows/Python 3.14.10の隔離環境で22件の単体・統合テストが合格した。手計算指標値、ウォームアップ、Long/Short鏡像、未来行追加時の過去不変性、2/3本境界、交差設定、欠損リセット、Streamlit初期画面を対象とした。Python構文検査とgit差分検査も合格した。ローカルStreamlitのヘルスチェックはHTTP 200 / `ok`。

テストは固定・合成データを使い、資産推移、損益、バックテスト、最適化は実行していない。

## クラウドで次に確認すること

1. GitHubの `codex/anti-logic-cloud-setup` ブランチを選ぶ。
2. Python 3.11を利用できる環境で `bash scripts/setup.sh` を実行する。
3. `bash scripts/check.sh` を実行する。
4. [cloud-setup.md](cloud-setup.md) のコマンドでStreamlitを起動する。
5. 少数の日本個別株と国内ETFについて取得・日付・チャート・中間値を確認する。

このローカル環境にはLinux、Docker、bash、Python 3.11がないため、Linuxのセットアップ実行は未確認。Yahoo Financeの実データ取得とiPhone実機表示も未確認である。依存はバージョン範囲で管理しており完全固定ではない。クラウドでの動作確認結果を追記する。

## 次の承認ゲート

ユーザーがsetup/hookを確認した後、entry、exit、stop、約定、コスト、データ品質等を別仕様として整理する。バックテストはユーザーの明示依頼を待つ。
