# 仕様書索引

作成日: 2026-11-05

版: 1.1
状態: **Antiロジック検証は承認済み・バックテスト仕様は未承認**

## 状態の読み方
- **確定事項**: ユーザーが明示した要求、または既存コードから直接確認した現状事実。現状事実は将来仕様として承認済みという意味ではない。
- **提案事項**: 技術調査に基づく推奨案。承認前は採用済みとして扱わない。
- **未決定事項**: 原典、数値、データ、運用条件などの決定が必要な事項。依存する実装を開始しない。

## 文書一覧
| 文書 | 内容 | 今回の依頼との対応 |
| --- | --- | --- |
| [requirements.md](requirements.md) | 目標、対象、機能・品質・制約 | 全体 |
| [architecture.md](architecture.md) | 推奨構成、共通戦略API、Streamlit統合案 | 3 |
| [data-sources.md](data-sources.md) | 日本株・ETFの無料データ候補と限界 | 4 |
| [strategies.md](strategies.md) | 5戦略を仕様化する項目、原典と仮定 | 5 |
| [backtest.md](backtest.md) | エンジン、約定、会計、指標、バイアス防止 | 6 |
| [benchmark.md](benchmark.md) | オルカンとTOPIXの比較方法 | 7 |
| [test-plan.md](test-plan.md) | 承認前の高水準テスト方針・品質ゲート | 8 |
| [logic-validation-plan.md](logic-validation-plan.md) | バックテスト前のロジック検証範囲、詳細テスト設計、実装計画 | 追加承認 |
| [lbr-anti-logic.md](lbr-anti-logic.md) | 承認済みの2つのAnti指標・状態遷移・出力契約 | 追加承認 |
| [phases.md](phases.md) | 開発フェーズ案。詳細実装計画ではない | 9 |
| [open-questions.md](open-questions.md) | ユーザー判断・承認事項 | 10 |

## 承認ゲート
Antiの指標・setup/hook・クラウドセットアップは承認済みであり、[logic-validation-plan.md](logic-validation-plan.md) の範囲で実装と自動テストを行う。entry、exit、stop、損益、バックテストは未承認のため実装・実行しない。ほかの戦略も個別仕様の承認を待つ。

外部サービスの料金、利用規約、データ範囲は2026-11-05時点の調査結果である。実装開始時と運用開始時に公式情報を再確認する。
