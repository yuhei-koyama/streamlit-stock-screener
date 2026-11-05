# LBR Anti ロジック仕様

作成日: 2026-11-05 / 版: 1.0-approved / 状態: **ロジック検証実装を承認済み**

## 1. 適用範囲

本仕様は、バックテスト前に次の2種類のAnti候補を計算・可視化し、固定データで機械的に検証するためのものである。

- `lbr_anti_stochastic_v1`: 7/4/10ストキャスティクスを用いる原典比較版
- `lbr_anti_310_adaptation_v1`: 3/10 Oscillatorへ適用するプロジェクト版

今回は `setup` と `hook` の検出までを対象とする。注文、エントリー価格、損切り、利益確定、ポジション、損益、バックテストは定義・実装しない。

## 2. 入力契約

- 行は判定可能時点の古い順で、日時インデックスは昇順かつ一意であること。
- 3/10版は数値の `Close`、ストキャス版は数値の `High`、`Low`、`Close` を必要とする。
- `High >= Low` を満たすこと。違反、列欠如、非数値、降順、日時重複は入力エラーとする。
- 欠損・無限値・計算不能窓では値を補完せず、その時点の状態をリセットする。
- 中央化rolling、負方向shift、将来pivotなど、将来行を利用する計算は禁止する。
- 入力DataFrameを変更しない。

## 3. 指標

### 3.1 3/10版

```text
Fast(t) = SMA(Close, 3)(t) - SMA(Close, 10)(t)
Slow(t) = SMA(Fast, 16)(t)
Histogram(t) = Fast(t) - Slow(t)
```

全窓を要求する。0始まりではFastの最初の有効位置は9、SlowとHistogramは24である。Histogramは表示用であり、Anti判定条件には使わない。

### 3.2 ストキャス版

```text
RawK(t) = 100 * (Close(t) - LowestLow(7)(t))
                 / (HighestHigh(7)(t) - LowestLow(7)(t))
Fast(t) = SMA(RawK, 4)(t)
Slow(t) = SMA(Fast, 10)(t)
Histogram(t) = Fast(t) - Slow(t)
```

0始まりではRawKの最初の有効位置は6、Fastは9、SlowとHistogramは18である。7本の最高値と最安値が等しい場合はRawKをNaNとし、任意値で補完しない。

## 4. Anti状態遷移

以下は公開資料の曖昧さを機械化した**本プロジェクト固有の検証規則**であり、原典の逐語的な規則とは断定しない。

### 4.1 Long候補

1. 開始直前にFastとSlowが上向きで、FastがSlow以上のトレンド側にある。
2. Slowは1本差で上昇を維持し、Fastが連続して下降する。
3. 調整の最終時点で `abs(Fast - Slow)` が開始直前より小さい。
4. 必要本数に達した時点を `setup_long` とする。
5. setup後、Slowが上昇を維持したままFastが上昇へ転じた当該バーを一度だけ `hook_long` とする。

### 4.2 Short候補

Longの符号反転である。開始直前にFastとSlowが下向きでFastがSlow以下、Slowが下降を維持しFastが連続上昇して接近し、必要本数後にFastが下降へ転じた時点をhookとする。

### 4.3 共通の境界規則

- 傾きは現在値と1本前の厳密比較で判定する。等値はトレンドでも調整でもなく、候補をリセットする。
- `Fast == Slow` は接触であり交差ではない。
- `allow_cross=false` の場合、調整中とhook時を通じてLongは `Fast >= Slow`、Shortは `Fast <= Slow` を要求する。
- `allow_cross=true` の場合はSlowの反対側への通過を許す。ただし距離縮小条件は残す。
- 欠損、無限値、Slowの方向崩れ、Fastの横ばいは候補をリセットする。
- 最小本数未満の反転はhookではない。
- ストキャス版の3本はsetupの機械化条件であり、価格逆指値によるentryとは扱わない。
- First Crossは別概念とし、Slowとの交差だけではAntiを成立させない。

## 5. パラメータ

| 戦略ID | 調整最小本数 | 交差 |
| --- | --- | --- |
| `lbr_anti_stochastic_v1` | 3本 | 許可 |
| `lbr_anti_310_adaptation_v1` | 初期値3本、2本へ切替可能 | 許可/不許可を切替可能 |

## 6. 出力契約

各時点について少なくとも次を返す。

- `strategy_id`, `definition_version`, `parameter_set`
- `decision_timestamp`, `data_cutoff_timestamp`, `warmup_ready`
- `phase`: `warmup`, `idle`, `pullback`, `setup`, `hook`, `invalid`
- `direction`: `long`, `short`, または空
- `reason`
- `fast`, `slow`, `histogram`, `fast_slope`, `slow_slope`, `distance_to_slow`
- `pullback_count`, `crossed_slow`
- `setup_long`, `setup_short`, `hook_long`, `hook_short`

entry、stop、exit、order、期待収益は返さない。

## 7. 必須テスト

- 手計算fixtureでSMA3/10/16と7/4/10の各値・ウォームアップ位置を照合する。
- 指標式と状態遷移が本書の定義だけに従うことを固定する。
- Long/Short鏡像、2本/3本境界、Slow方向崩れ、Fast横ばい、接近不足を検証する。
- 接触と交差を分け、交差許可ON/OFFの差を検証する。
- 欠損、無限値、zero-range、重複日時、降順、非数値を検証する。
- hookを次バーに重複発火しないことを確認する。
- First CrossやV字だけでAntiにならないことを確認する。
- 将来行追加・将来値変更で過去の値と判定が変わらないことを確認する。
- 価格鏡像でLongとShortの日時・本数・理由が対称になることを確認する。
- UI経由と純粋関数の結果が同一であることを確認する。

## 8. 承認記録

2026-11-05、ユーザーは次を承認した。

- 旧Clineロジックとその仕様を削除し、互換対象にしないこと。
- 原典比較用ストキャス版と3/10適応版を併設すること。
- 3/10版の調整期間は3本を初期値とし、2本へ切替可能にすること。
- Slowへの接近と交差許可/不許可を検証できること。
- 今回はロジックのセットアップ・フック可視化まで進め、バックテストは別途明示依頼を待つこと。
