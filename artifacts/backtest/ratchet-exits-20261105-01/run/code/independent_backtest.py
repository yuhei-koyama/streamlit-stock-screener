"""各買いシグナルを同額元本の独立した仮想取引として評価する。"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .backtest import Config, _exit_at_close, _prepare_backtest
from .ratchet import initialize_ratchet, update_ratchet, price_exit


TRADE_COLUMNS = [
    'trade_id', 'ticker', 'asset_class', 'signal_date', 'entry_date', 'exit_date',
    'entry_price', 'exit_price', 'quantity', 'entry_cost', 'net_pnl', 'net_return',
    'costs', 'holding_sessions', 'exit_reason', 'overlaps_existing',
]
POSITION_COLUMNS = [
    'trade_id', 'ticker', 'asset_class', 'signal_date', 'entry_date', 'entry_price',
    'quantity', 'entry_cost', 'mark', 'mark_date', 'unrealized_return',
    'holding_sessions', 'pending_exit', 'overlaps_existing',
]
REJECTION_COLUMNS = ['ticker', 'signal_date', 'date', 'reason']
ORDER_COLUMNS = ['trade_id', 'date', 'effective_from', 'event', 'ratchet_step', 'initial_risk', 'stop', 'target']
RATCHET_TRADE_COLUMNS = ['initial_risk', 'ratchet_step', 'exit_stop', 'exit_target', 'ambiguous_oco']
RATCHET_POSITION_COLUMNS = ['initial_risk', 'ratchet_step', 'stop', 'target']


@dataclass
class IndependentResult:
    trades: pd.DataFrame
    positions: pd.DataFrame
    rejections: pd.DataFrame
    metrics: dict
    order_updates: pd.DataFrame | None = None


def trade_statistics(trades):
    """同額元本に対する完了取引の損益率を集計する。"""
    returns = trades.net_return
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    return {
        'trade_count': len(trades),
        'mean_net_return': float(returns.mean()) if len(trades) else None,
        'median_net_return': float(returns.median()) if len(trades) else None,
        'win_rate': float((returns > 0).mean()) if len(trades) else None,
        'average_win_return': float(wins.mean()) if len(wins) else None,
        'average_loss_return': float(losses.mean()) if len(losses) else None,
        'profit_factor': float(wins.sum() / abs(losses.sum())) if len(losses) else None,
        'average_holding_sessions': float(trades.holding_sessions.mean()) if len(trades) else None,
        'overlapping_trades': int(trades.overlaps_existing.sum()),
    }


def grouped_statistics(trades, field):
    """銘柄・エントリー年・資産区分ごとの集計を返す。"""
    columns = [field, *trade_statistics(trades).keys()]
    records = [
        {field: key, **trade_statistics(group)}
        for key, group in trades.groupby(field, sort=True)
    ]
    return pd.DataFrame(records, columns=columns)


def run_independent_backtest(execution, signals, strategy_id, exit_policy,
                             calendar, asset_classes, config=None):
    """正規化元本1で計算し、同一銘柄の重複シグナルも独立評価する。"""
    cfg = config or Config()
    market, decisions, days = _prepare_backtest(
        execution, signals, strategy_id, exit_policy, calendar, asset_classes, cfg)
    for frame in market.values():
        if not frame.Split.eq(1).all() or not frame.Dividend.eq(0).all():
            raise ValueError('Independent evaluation requires adjusted prices with embedded actions')

    trades, positions, rejections, updates = [], [], [], []
    is_ratchet = exit_policy in ('C', 'D')

    def record_order(position, date, effective_from, event):
        updates.append(dict(trade_id=position['trade_id'], date=date,
                            effective_from=effective_from, event=event,
                            **{name: position[name] for name in ('ratchet_step', 'initial_risk', 'stop', 'target')}))

    def close_trade(position, date, session, base_price, reason, apply_slippage=True, ambiguous=False):
        price = base_price * (1 - cfg.slippage) if apply_slippage else base_price
        proceeds = position['quantity'] * price
        exit_fee = proceeds * cfg.commission
        pnl = proceeds - exit_fee - 1.0
        trades.append({
            **{name: position[name] for name in (
                'trade_id', 'ticker', 'asset_class', 'signal_date', 'entry_date',
                'entry_price', 'quantity', 'overlaps_existing')},
            'exit_date': date, 'exit_price': price, 'entry_cost': 1.0,
            'net_pnl': pnl, 'net_return': pnl,
            'costs': position['entry_costs'] + exit_fee
                     + position['quantity'] * (base_price - price),
            'holding_sessions': session - position['entry_session'],
            'exit_reason': reason,
            **(dict(initial_risk=position['initial_risk'], ratchet_step=position['ratchet_step'],
                    exit_stop=position['stop'], exit_target=position['target'], ambiguous_oco=ambiguous)
               if is_ratchet else {}),
        })

    for ticker, frame in market.items():
        rows = dict(zip(frame.index, frame.itertuples(index=False)))
        flags = dict(zip(decisions[ticker].index, decisions[ticker].itertuples(index=False)))
        valid = np.isfinite(frame[['Open', 'High', 'Low', 'Close', 'Volume']]).all(axis=1)
        valid = valid & frame.Volume.gt(0)
        can_buy = (valid & frame.get('CanBuy', True)).to_dict()
        can_sell = (valid & frame.get('CanSell', True)).to_dict()
        active = []
        for session, date in enumerate(days):
            row = rows.get(date)
            survivors = []
            # 前日までの売却予約と寄付のギャップストップを先に処理する。
            for position in active:
                if can_sell.get(date, False):
                    fill = price_exit(position, row)
                    if fill is not None:
                        close_trade(position, date, session, fill.base_price, fill.reason,
                                    fill.apply_slippage, fill.ambiguous)
                        continue
                    if position['pending_exit']:
                        close_trade(position, date, session, float(row.Open), position['pending_exit'])
                        continue
                survivors.append(position)
            active = survivors

            if session:
                signal_date = days[session - 1]
                signal = flags.get(signal_date)
                if signal is not None and signal.ready and signal.buy_signal:
                    if can_buy.get(date, False):
                        price = float(row.Open) * (1 + cfg.slippage)
                        quantity = 1.0 / (price * (1 + cfg.commission))
                        active.append({
                            'trade_id': f'{ticker}:{signal_date.date()}',
                            'ticker': ticker, 'asset_class': asset_classes[ticker],
                            'signal_date': signal_date, 'entry_date': date,
                            'entry_session': session, 'entry_price': price,
                            'quantity': quantity, 'peak': price,
                            'stop': price * (1 - cfg.initial_stop),
                            'entry_costs': quantity * (price * cfg.commission + price - float(row.Open)),
                            'mark': float(row.Open), 'mark_date': date,
                            'pending_exit': None, 'overlaps_existing': bool(active),
                        })
                        if is_ratchet:
                            initialize_ratchet(active[-1], cfg.initial_stop)
                            record_order(active[-1], date, date, 'entry')
                    else:
                        rejections.append(dict(ticker=ticker, signal_date=signal_date,
                                               date=date, reason='not_tradable'))

            survivors = []
            for position in active:
                if can_sell.get(date, False):
                    fill = price_exit(position, row, include_intraday=True)
                    if fill is not None:
                        close_trade(position, date, session, fill.base_price, fill.reason,
                                    fill.apply_slippage, fill.ambiguous)
                        continue
                if row is not None and pd.notna(row.Close):
                    position['mark'] = float(row.Close)
                    position['mark_date'] = date
                reason = _exit_at_close(position, row, flags.get(date), session,
                                        strategy_id, exit_policy,
                                        time_exit_enabled=cfg.time_exit_enabled)
                if reason:
                    position['pending_exit'] = reason
                if is_ratchet and can_sell.get(date, False) and session + 1 < len(days):
                    if update_ratchet(position, float(row.Close), exit_policy):
                        record_order(position, date, days[session + 1], 'update')
                survivors.append(position)
            active = survivors

        signal = flags.get(days[-1])
        if signal is not None and signal.ready and signal.buy_signal:
            rejections.append(dict(ticker=ticker, signal_date=days[-1],
                                   date=days[-1], reason='outside_test_period'))
        for position in active:
            positions.append({
                **{name: position[name] for name in (
                    'trade_id', 'ticker', 'asset_class', 'signal_date', 'entry_date',
                    'entry_price', 'quantity', 'mark', 'mark_date',
                    'pending_exit', 'overlaps_existing')},
                'entry_cost': 1.0,
                'unrealized_return': position['quantity'] * position['mark'] - 1.0,
                'holding_sessions': len(days) - position['entry_session'],
                **({name: position[name] for name in RATCHET_POSITION_COLUMNS} if is_ratchet else {}),
            })

    trade_frame = pd.DataFrame(trades, columns=TRADE_COLUMNS + (RATCHET_TRADE_COLUMNS if is_ratchet else []))
    if len(trade_frame):
        trade_frame = trade_frame.sort_values(['entry_date', 'ticker', 'trade_id']).reset_index(drop=True)
    position_frame = pd.DataFrame(positions, columns=POSITION_COLUMNS + (RATCHET_POSITION_COLUMNS if is_ratchet else []))
    rejection_frame = pd.DataFrame(rejections, columns=REJECTION_COLUMNS)
    metrics = trade_statistics(trade_frame)
    metrics.update(open_positions=len(position_frame), rejected_orders=len(rejection_frame),
                   normalized_entry_cost=1.0,
                   open_mean_unrealized_return=float(position_frame.unrealized_return.mean())
                   if len(position_frame) else None)
    update_frame = pd.DataFrame(updates, columns=ORDER_COLUMNS)
    if is_ratchet:
        metrics['ambiguous_oco_trades'] = int(trade_frame.ambiguous_oco.sum())
        metrics['order_update_count'] = int(update_frame.event.eq('update').sum())
    return IndependentResult(trade_frame, position_frame, rejection_frame, metrics, update_frame)


def write_report(output, summary, status):
    """独立取引の損益率と再現条件を日本語で保存する。"""
    lines = [
        '# 独立仮想取引の検証結果', '',
        f"データ区分は{status['kind']}。全{len(summary)}条件を完了。",
        '各取引は同額元本に正規化し、同一銘柄の保有期間が重なる買いシグナルも独立評価しました。',
        '平均・中央値・勝率は決済済み取引を対象とします。期末保有の評価は別に保存しています。', '',
        '| 入口設定 | 出口 | 決済件数 | 平均損益率 | 中央値 | 勝率 | 期末保有 |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: |',
    ]
    def percent(value):
        return f'{value:.2%}' if pd.notna(value) else '対象なし'

    for row in summary.itertuples():
        lines.append(
            f'| {row.strategy_id} | {row.exit_policy} | {row.trade_count} | '
            f'{percent(row.mean_net_return)} | {percent(row.median_net_return)} | '
            f'{percent(row.win_rate)} | {row.open_positions} |')
    if status.get('data_quality_policy'):
        lines.extend([
            '', '## 価格補正', '',
            f"承認済みの価格範囲補正を{status['corrected_rows']}行へ適用しました。",
            f"微小差は{status['rounding_scale_rows']}行、実質的な差は{status['substantive_rows']}行です。",
            'corrections.csvに変更前後の値、correction.jsonに原入力のハッシュと処理記録を保存しています。',
            '高値・安値を始値・終値を含む範囲へ広げた仮定を含みます。',
        ])
    if status.get('exit_set') == 'cd':
        exit_description = [
            '初期損切り幅を1Rとし、初期利確は+3Rです。終値が1R上昇するごとに損切りを1R切り上げ、翌営業日から使います。',
            '出口Cは利確を固定し、出口Dは利確も同時に1R切り上げます。',
            '日足内でストップ・利確の両方へ到達した場合はストップを優先し、該当取引を記録しています。',
            '利確の指値約定は目標価格とし、手数料を適用します。売りスリッページは損切りに適用します。',
            'order_updates.csvに初期価格と翌営業日用の更新履歴を保存しました。',
            '最大保有期限は20営業日です。' if status['settings'].get('time_exit_enabled', True) else '保有日数による決済を無効化しました。',
        ]
    else:
        exit_description = [
            '出口Aは20営業日経過を引けで判断し翌寄付で売却します。' if status['settings'].get('time_exit_enabled', True) else '出口Aは初期8%損切りを使います。',
            '出口Bはリンダ各版の反対候補、RSIの50回復、ミネルヴィニの前日までの最高値から10%下のトレーリングを使います。',
            '出口Bの最大保有期限はリンダ20・RSI5・ミネルヴィニ40営業日です。' if status['settings'].get('time_exit_enabled', True) else '保有日数による決済を無効化しました。',
        ]
    lines.extend([
        '', '## 条件と保存物', '',
        f"検証期間は{status['settings']['start']}〜{status['settings']['end']}。",
        '買いシグナルの翌営業日寄付で購入。初期損切り8%、片道手数料0.1%、片道スリッページ0.05%。',
        *exit_description,
        f"コード版は `{status['code_commit']}`、作業中の変更ありは `{status['code_dirty']}`。",
        '実行時の共通Pythonコードはcode/へ保存しました。入力とコードのハッシュはrun.jsonに記録しています。',
        '各条件のtrades.csv、positions.csv、rejections.csvへ取引・期末保有・見送りを保存しています。',
        'by_ticker.csv、by_entry_year.csv、by_asset_class.csvへ銘柄別・購入年別・資産区分別の集計を保存しています。',
        '損益率と設定はmetrics.json、config.json、全条件の集計はsummary.csvに保存しています。', '',
        '## データと解釈の制約', '',
        '現在の70銘柄による生存者・選択バイアスがあります。保有期間が重なる取引には相関があります。',
        '調整済み価格と仮想の端数数量を使い、分割・配当は価格調整へ含めています。',
        '日足・観測済み営業日の和集合・出来高による売買可否には、流動性と値幅制限の再現上の制約があります。',
        '末日買いシグナルや売買不可による見送りはrejections.csvに記録しています。',
        '将来の成果を評価するには期間外データによる追加検証が必要です。',
    ])
    (output / 'REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
