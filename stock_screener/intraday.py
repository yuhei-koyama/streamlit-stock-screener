"""確定5分足の次足注文、有限資金、当日手仕舞いの仮想バックテスト。"""
from dataclasses import asdict, dataclass
import math

import numpy as np
import pandas as pd

from .backtest import performance
from .backtest_signals import MINERVINI, RSI2, RSI14, STRATEGIES, screening_signals
from .finite_capital import PriceRow, drawdown_periods
from .ratchet import initialize_ratchet, price_exit, update_ratchet


PRICE_COLUMNS = ['Open', 'High', 'Low', 'Close', 'Volume']
SIGNAL_COLUMNS = ['buy_signal', 'sell_signal', 'ready', 'strategy_id', 'rsi']
STOPS = (.003, .005, .008, .01, .012)


@dataclass(frozen=True)
class IntradayConfig:
    start: str
    end: str
    window: str = 'full_session'
    initial_cash: float = 10_000_000
    purchase_budget: float = 100_000
    commission: float = .001
    slippage: float = .0005
    initial_stop: float = .005
    ratchet_reference_stop: float = .005
    minervini_trailing: float = .01

    def __post_init__(self):
        if self.window not in ('full_session', 'morning_0900_0930'):
            raise ValueError('未対応の取引時間帯です')
        start, end = pd.Timestamp(self.start), pd.Timestamp(self.end)
        if start.tzinfo or end.tzinfo or start != start.normalize() or end != end.normalize() or start > end:
            raise ValueError('評価開始・終了は順序の正しい日付で指定してください')
        for name in ('initial_cash', 'purchase_budget'):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name}は有限の正数が必要です')
        for name in ('commission', 'slippage'):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 <= value < 1:
                raise ValueError(f'{name}が不正です')
        for name in ('initial_stop', 'ratchet_reference_stop', 'minervini_trailing'):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 < value < 1:
                raise ValueError(f'{name}が不正です')


@dataclass
class IntradayResult:
    bars: pd.DataFrame
    daily: pd.DataFrame
    trades: pd.DataFrame
    positions: pd.DataFrame
    rejections: pd.DataFrame
    order_updates: pd.DataFrame
    quality_events: pd.DataFrame
    contributions: pd.DataFrame
    drawdown_periods: pd.DataFrame
    metrics: dict
    config: dict


def regular_bar_index(sessions):
    """確定した営業日の通常5分区間を開始時刻で返す。"""
    days = pd.DatetimeIndex(sessions)
    if days.empty or days.tz is not None or not days.equals(days.normalize()) or not days.is_unique or not days.is_monotonic_increasing:
        raise ValueError('取引日は一意・昇順のタイムゾーンなし日付が必要です')
    intervals = []
    for day in days:
        date = day.date().isoformat()
        intervals.extend(pd.date_range(date+' 09:00', date+' 11:25', freq='5min', tz='Asia/Tokyo'))
        intervals.extend(pd.date_range(date+' 12:30', date+' 15:20', freq='5min', tz='Asia/Tokyo'))
    return pd.DatetimeIndex(intervals, name='Timestamp')


def _aligned_prices(frame, grid, allowed_index=None):
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ValueError('価格の時刻にはタイムゾーンが必要です')
    if not frame.index.is_unique or not frame.index.is_monotonic_increasing:
        raise ValueError('価格の時刻は一意・昇順が必要です')
    if not set(PRICE_COLUMNS).issubset(frame.columns):
        raise ValueError('5分足OHLCVが必要です')
    index = frame.index.tz_convert('Asia/Tokyo')
    allowed = grid if allowed_index is None else allowed_index
    if not index.isin(allowed).all():
        raise ValueError('価格に通常区間外の足があります')
    out = frame.copy()
    out.index = index
    out = out.reindex(grid)
    values = out[PRICE_COLUMNS].to_numpy(dtype=float)
    finite = np.isfinite(values).all(axis=1)
    observed = out[PRICE_COLUMNS].notna().any(axis=1).to_numpy()
    if (observed & ~finite).any():
        raise ValueError('OHLCVの部分欠損または非有限値があります')
    valid = finite & (values[:, :4] > 0).all(axis=1) & (values[:, 4] >= 0)
    if (finite & ~valid).any():
        raise ValueError('価格または出来高が不正です')
    if (valid & ((values[:, 1] < values[:, :4].max(axis=1)) | (values[:, 2] > values[:, :4].min(axis=1)))).any():
        raise ValueError('OHLCの大小関係が不正です')
    return out


def intraday_signals(prices, strategy_id, sessions, split_dates=None, decision_time=None, latest_only=False):
    """欠損・価格単位変更で状態を区切り、既存の共有式を呼び出す。"""
    if strategy_id not in STRATEGIES:
        raise ValueError('未対応の入口ロジックです')
    calendar_grid = regular_bar_index(sessions)
    grid = calendar_grid
    if decision_time is not None:
        cutoff = pd.Timestamp(decision_time)
        if cutoff.tzinfo is None:
            raise ValueError('判定時刻にはタイムゾーンが必要です')
        grid = grid[grid + pd.Timedelta(5, unit='min') <= cutoff]
        if grid.empty:
            raise ValueError('判定時刻までに確定した足がありません')
    market = {ticker: _aligned_prices(frame, grid, calendar_grid) for ticker, frame in prices.items()}
    if strategy_id == MINERVINI and '1306.T' not in market:
        raise ValueError('RS参照1306.Tが必要です')
    output = {}
    for ticker, frame in market.items():
        target_index = grid[-1:] if latest_only else grid
        flags = pd.DataFrame(dict(buy_signal=False, sell_signal=False, ready=False,
                                  strategy_id=strategy_id, rsi=np.nan), index=target_index)
        valid = frame[PRICE_COLUMNS].notna().all(axis=1)
        if strategy_id == MINERVINI:
            valid &= market['1306.T'].Close.notna()
        resets = ~valid
        dates = grid.tz_localize(None).normalize()
        boundary_dates = pd.to_datetime((split_dates or {}).get(ticker, []))
        if strategy_id == MINERVINI:
            boundary_dates = boundary_dates.union(pd.to_datetime((split_dates or {}).get('1306.T', [])))
        resets |= dates.isin(boundary_dates) & (grid.strftime('%H:%M') == '09:00')
        groups = resets.cumsum()
        selected = valid.copy()
        if latest_only:
            selected &= groups.eq(groups.iloc[-1]) & bool(valid.iloc[-1])
        for _, part in frame.loc[selected].groupby(groups[selected]):
            reference = market['1306.T'].Close if strategy_id == MINERVINI else None
            result = screening_signals(part, strategy_id, reference)
            timestamps = flags.index.intersection(part.index)
            for column in ('buy_signal', 'sell_signal', 'ready'):
                flags.loc[timestamps, column] = result.loc[timestamps, column].astype(bool)
            if 'rsi' in result:
                flags.loc[timestamps, 'rsi'] = result.loc[timestamps, 'rsi']
        flags['segment_id'] = groups.reindex(flags.index)
        output[ticker] = flags
    return output


def run_intraday_backtest(execution, signals, strategy_id, exit_policy, sessions,
                          daily_prices, config, auxiliary=None):
    """シグナル足確定後の注文と、窓終了前の価格出口を保存する。"""
    if not isinstance(config, IntradayConfig) or exit_policy not in ('A', 'B', 'C', 'D'):
        raise ValueError('分足設定と出口A〜Dが必要です')
    if strategy_id not in STRATEGIES or not execution or set(execution) != set(signals):
        raise ValueError('入口と価格・シグナルの銘柄集合が不正です')
    cfg = config
    all_days = pd.DatetimeIndex(sessions)
    full_grid = regular_bar_index(all_days)
    market = {t: _aligned_prices(f, full_grid) for t, f in execution.items()}
    days = all_days[(all_days >= cfg.start) & (all_days <= cfg.end)]
    if days.empty or days[0] != pd.Timestamp(cfg.start) or days[-1] != pd.Timestamp(cfg.end):
        raise ValueError('評価開始・終了の営業日が入力にありません')
    for ticker, frame in daily_prices.items():
        if (not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is not None
                or not frame.index.is_unique or not frame.index.equals(frame.index.normalize())
                or not {'Close','Volume'}.issubset(frame.columns)):
            raise ValueError(f'日足終値の形式が不正です {ticker}')
    for ticker, frame in (auxiliary or {}).items():
        if (not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None
                or not frame.index.is_unique or not set(PRICE_COLUMNS).issubset(frame.columns)
                or not (frame.index.tz_convert('Asia/Tokyo').strftime('%H:%M')=='11:30').all()):
            raise ValueError(f'前引け補助価格の形式が不正です {ticker}')
    decisions = {}
    for ticker, flags in signals.items():
        if not flags.index.equals(full_grid) or not set(SIGNAL_COLUMNS[:4]).issubset(flags.columns):
            raise ValueError('シグナル時刻または列が不正です')
        if flags[SIGNAL_COLUMNS[:3]].isna().any().any() or not flags.strategy_id.eq(strategy_id).all():
            raise ValueError('シグナルの欠損または入口IDの不一致があります')
        if not all(pd.api.types.is_bool_dtype(flags[c]) for c in SIGNAL_COLUMNS[:3]):
            raise ValueError('売買シグナルは真偽値が必要です')
        if strategy_id in (RSI2, RSI14) and 'rsi' not in flags:
            raise ValueError('RSIの出口判定にはrsi列が必要です')
        decisions[ticker] = flags
    tickers = sorted(market)
    cash, costs = float(cfg.initial_cash), 0.0
    positions, pending_buys = {}, []
    trades, rejected, updates, quality, snapshots, daily = [], [], [], [], [], []
    completed = True
    peak_positions = 0
    window_end = '15:30' if cfg.window == 'full_session' else '09:30'
    last_bar = '15:20' if cfg.window == 'full_session' else '09:25'

    def tradable(ticker, timestamp, side):
        row = market[ticker].loc[timestamp]
        return bool(np.isfinite(row.Open) and row.Open > 0 and np.isfinite(row.Volume)
                    and row.Volume > 0 and row.get('CanBuy' if side == 'buy' else 'CanSell', True))

    def record_order(pos, timestamp, effective, event):
        updates.append(dict(ticker=pos['ticker'], trade_id=pos['trade_id'],
                            recorded_at=timestamp, effective_from=effective, event=event,
                            stop=pos['stop'], target=pos.get('target'),
                            ratchet_step=pos.get('ratchet_step')))

    def sell(ticker, timestamp, base, reason, bar_start, precision,
             apply_slippage=True, ambiguous=False):
        nonlocal cash, costs
        pos = positions.pop(ticker)
        price = base * (1 - cfg.slippage) if apply_slippage else base
        gross = pos['quantity'] * price
        exit_fee = gross * cfg.commission
        exit_slip = pos['quantity'] * (base - price)
        proceeds = gross - exit_fee
        pnl = proceeds - cfg.purchase_budget
        cash += proceeds
        costs += exit_fee + exit_slip
        trades.append(dict(ticker=ticker, trade_id=pos['trade_id'],
            signal_bar_start=pos['signal_bar_start'], signal_time=pos['signal_time'],
            entry_time=pos['entry_time'], exit_time=timestamp, exit_bar_start=bar_start,
            exit_time_precision=precision, exit_reason=reason,
            entry_price=pos['entry_price'], exit_price=price, quantity=pos['quantity'],
            entry_cost=cfg.purchase_budget, net_pnl=pnl, net_return=pnl / cfg.purchase_budget,
            costs=pos['entry_costs'] + exit_fee + exit_slip,
            gross_pnl=pnl + pos['entry_costs'] + exit_fee + exit_slip,
            holding_minutes=(timestamp-pos['entry_time']).total_seconds()/60,
            holding_sessions=1, ambiguous_oco=ambiguous, path_missing=pos['path_missing'],
            exit_stop=pos['stop'], exit_target=pos.get('target')))

    def snapshot(timestamp):
        value = sum(p['quantity'] * p['mark'] for p in positions.values())
        snapshots.append(dict(timestamp=timestamp, cash=cash, market_value=value,
                              nav=cash+value, positions=len(positions), costs=costs))

    def apply_price_exit(ticker, row, timestamp, bar_start, precision, intrabar=False):
        fill = price_exit(positions[ticker], row, include_intraday=intrabar)
        if fill:
            sell(ticker, timestamp, fill.base_price, fill.reason, bar_start, precision,
                 fill.apply_slippage, fill.ambiguous)

    for day in days:
        date = day.date().isoformat()
        cutoff = pd.Timestamp(date+' '+window_end, tz='Asia/Tokyo')
        starts = full_grid[(full_grid.tz_localize(None).normalize() == day)
                           & (full_grid.strftime('%H:%M') <= last_bar)]
        pending_buys = []
        snapshot(pd.Timestamp(date+' 09:00', tz='Asia/Tokyo'))
        for timestamp in starts:
            end = timestamp + pd.Timedelta(5, unit='min')
            for ticker, pos in list(positions.items()):
                if pos['update_effective'] is not None and pos['update_effective'] <= timestamp:
                    pos.update(pos.pop('next_order'))
                    pos['update_effective'] = None
                if tradable(ticker, timestamp, 'sell'):
                    row = market[ticker].loc[timestamp]
                    opening = PriceRow(row.Open, row.Open, row.Open, row.Open, row.Volume, True, True)
                    apply_price_exit(ticker, opening, timestamp, timestamp, 'bar_open')
                    if ticker in positions and pos['pending_exit']:
                        sell(ticker, timestamp, float(row.Open), pos['pending_exit'], timestamp, 'bar_open')
            reserved = 0.0
            for rank, order in enumerate(sorted(pending_buys, key=lambda x:x['ticker']), 1):
                ticker = order['ticker']
                row = market[ticker].loc[timestamp]
                reason = None
                if ticker in positions:
                    reason = 'duplicate_position'
                elif cash + 1e-8 < cfg.purchase_budget:
                    reason = 'insufficient_cash'
                elif not tradable(ticker, timestamp, 'buy'):
                    reason = 'next_bar_not_tradable'
                    # 足の出来高による約定確認まで、同じ始値の別注文へ余力を転用しない。
                    cash -= cfg.purchase_budget
                    reserved += cfg.purchase_budget
                if reason:
                    rejected.append(dict(**order, order_time=timestamp, reason=reason, candidate_rank=rank))
                    continue
                price = float(row.Open) * (1 + cfg.slippage)
                quantity = cfg.purchase_budget / (price * (1 + cfg.commission))
                entry_costs = quantity * (price * cfg.commission + price - float(row.Open))
                cash -= cfg.purchase_budget
                costs += entry_costs
                pos = dict(**order, trade_id=f'{ticker}:{timestamp.isoformat()}', entry_time=timestamp,
                    entry_price=price, quantity=quantity, entry_costs=entry_costs, peak=price,
                    stop=price*(1-cfg.initial_stop), pending_exit=None,
                    mark=float(row.Open), mark_time=timestamp, path_missing=False, update_effective=None)
                if exit_policy in ('C', 'D'):
                    initialize_ratchet(pos, cfg.initial_stop, cfg.ratchet_reference_stop)
                positions[ticker] = pos
                record_order(pos, timestamp, timestamp, 'entry')
            pending_buys = []
            peak_positions = max(peak_positions, len(positions))
            for ticker, pos in list(positions.items()):
                row = market[ticker].loc[timestamp]
                if tradable(ticker, timestamp, 'sell') and np.isfinite(row[['High','Low','Close']].to_numpy(dtype=float)).all():
                    price_row = PriceRow(row.Open, row.High, row.Low, row.Close, row.Volume, True, True)
                    apply_price_exit(ticker, price_row, end, timestamp, 'bar_end_approximation', intrabar=True)
                else:
                    pos['path_missing'] = True
                    quality.append(dict(ticker=ticker, timestamp=timestamp, event='held_bar_not_tradable'))
                if ticker not in positions:
                    continue
                if np.isfinite(row.Close) and row.Close > 0:
                    pos.update(mark=float(row.Close), mark_time=end)
                flag = decisions[ticker].loc[timestamp]
                next_open = pd.Timestamp(date+' 12:30', tz='Asia/Tokyo') if end.strftime('%H:%M') == '11:30' else end
                if next_open >= cutoff or end.strftime('%H:%M') == '15:25':
                    continue
                next_order = dict(stop=pos['stop'], target=pos.get('target'), ratchet_step=pos.get('ratchet_step'))
                if exit_policy in ('C', 'D'):
                    preview = {**pos}
                    if np.isfinite(row.Close) and update_ratchet(preview, float(row.Close), exit_policy):
                        next_order = {key:preview.get(key) for key in ('stop','target','ratchet_step')}
                        record_order(preview, end, next_open, 'ratchet_update')
                elif exit_policy == 'B':
                    if strategy_id == MINERVINI and np.isfinite(row.High):
                        pos['peak'] = max(pos['peak'], float(row.High))
                        next_order['stop'] = max(pos['stop'], pos['peak']*(1-cfg.minervini_trailing))
                        if next_order['stop'] > pos['stop']:
                            record_order({**pos, **next_order}, end, next_open, 'trailing_update')
                    elif bool(flag.ready) and ((strategy_id in (RSI2, RSI14) and flag.get('rsi', np.nan) >= 50)
                                              or (strategy_id not in (MINERVINI, RSI2, RSI14) and flag.sell_signal)):
                        if not pos['pending_exit']:
                            pos['pending_exit'] = 'rsi_recovery' if strategy_id in (RSI2,RSI14) else 'opposite_signal'
                            record_order(pos, end, next_open, 'exit_signal')
                pos['next_order'] = next_order
                pos['update_effective'] = next_open
            cash += reserved
            for ticker in tickers:
                flag = decisions[ticker].loc[timestamp]
                if not (flag.ready and flag.buy_signal):
                    continue
                order = dict(ticker=ticker, signal_bar_start=timestamp, signal_time=end)
                time = end.strftime('%H:%M')
                eligible = '09:05' <= time <= '09:25' if cfg.window=='morning_0900_0930' else (
                    '09:05' <= time <= '11:25' or '12:35' <= time <= '15:20')
                if ticker in positions or not eligible:
                    reason = 'duplicate_position' if ticker in positions else 'outside_entry_window'
                    rejected.append(dict(**order, order_time=end, reason=reason, candidate_rank=None))
                else:
                    pending_buys.append(order)
            if end < cutoff:
                snapshot(end)
            if cfg.window=='full_session' and end.strftime('%H:%M')=='11:30':
                for ticker, pos in list(positions.items()):
                    event = (auxiliary or {}).get(ticker, pd.DataFrame())
                    if end in event.index:
                        row = event.loc[end]
                        if np.isfinite(row[PRICE_COLUMNS].to_numpy(dtype=float)).all() and row.Volume>0:
                            price_row = PriceRow(row.Open,row.High,row.Low,row.Close,row.Volume,True,True)
                            apply_price_exit(ticker,price_row,end+pd.Timedelta(5,unit='min'),end,'auction_event_approximation',True)
                            if ticker in positions:
                                pos.update(mark=float(row.Close),mark_time=end+pd.Timedelta(5,unit='min'))
                                if exit_policy=='B' and strategy_id==MINERVINI:
                                    pos['peak']=max(pos['peak'],float(row.High))
                                    previous_stop=pos['next_order']['stop']
                                    pos['next_order']['stop']=max(pos['next_order']['stop'],pos['peak']*(1-cfg.minervini_trailing))
                                    if pos['next_order']['stop'] > previous_stop:
                                        record_order({**pos, **pos['next_order']}, end+pd.Timedelta(5,unit='min'),
                                                     pos['update_effective'], 'auction_trailing_update')
                snapshot(end+pd.Timedelta(5,unit='min'))
        for ticker, pos in list(positions.items()):
            if cfg.window == 'morning_0900_0930':
                terminal = market[ticker].loc[pd.Timestamp(date+' 09:25',tz='Asia/Tokyo')]
            else:
                frame = daily_prices.get(ticker, pd.DataFrame())
                terminal = frame.loc[day] if day in frame.index else pd.Series(dtype=float)
            close = terminal.get('Close', np.nan)
            volume = terminal.get('Volume', np.nan)
            if np.isfinite(close) and close>0 and np.isfinite(volume) and volume>0 and bool(terminal.get('CanSell',True)):
                event = PriceRow(close,close,close,close,volume,True,True)
                apply_price_exit(ticker,event,cutoff,cutoff,'window_terminal_price')
                if ticker in positions:
                    sell(ticker,cutoff,float(close),'window_end',cutoff,'window_terminal_price')
            else:
                completed = False
                quality.append(dict(ticker=ticker,timestamp=cutoff,event='unclosed_due_to_missing_close'))
        snapshot(cutoff)
        final = snapshots[-1]
        daily.append(dict(date=day,**{key:final[key] for key in ('cash','market_value','nav','positions','costs')},receivables=0.0))
        if cash < -1e-6:
            raise ArithmeticError('現金が負になっています')
        if not completed:
            break

    trade_columns = ['ticker','trade_id','signal_bar_start','signal_time','entry_time','exit_time','exit_bar_start',
        'exit_time_precision','exit_reason','entry_price','exit_price','quantity','entry_cost','net_pnl','net_return',
        'costs','gross_pnl','holding_minutes','holding_sessions','ambiguous_oco','path_missing','exit_stop','exit_target']
    closed = pd.DataFrame(trades,columns=trade_columns)
    held = pd.DataFrame([{**p,'entry_cost':cfg.purchase_budget,'market_value':p['quantity']*p['mark'],
                          'unrealized_pnl':p['quantity']*p['mark']-cfg.purchase_budget} for p in positions.values()],
        columns=['ticker','trade_id','signal_bar_start','signal_time','entry_time','entry_price','quantity',
                 'entry_cost','entry_costs','mark','mark_time','market_value','unrealized_pnl','path_missing',
                 'pending_exit','stop','target','ratchet_step'])
    bars = pd.DataFrame(snapshots).set_index('timestamp')
    daily_frame = pd.DataFrame(daily).set_index('date')
    unrealized = float(held.unrealized_pnl.sum()) if len(held) else 0.0
    if not np.isclose(daily_frame.nav.iloc[-1],cfg.initial_cash+closed.net_pnl.sum()+unrealized,atol=.01,rtol=1e-10):
        raise ArithmeticError('資産と実現・含み損益が一致しません')
    if not np.allclose(bars.nav,bars.cash+bars.market_value,atol=.01,rtol=1e-10):
        raise ArithmeticError('足ごとの資産が一致しません')
    contributions = pd.DataFrame([dict(ticker=t,realized_pnl=float(closed.loc[closed.ticker.eq(t),'net_pnl'].sum()),
        unrealized_pnl=float(held.loc[held.ticker.eq(t),'unrealized_pnl'].sum()) if len(held) else 0.0) for t in tickers])
    contributions['total_pnl'] = contributions.realized_pnl+contributions.unrealized_pnl
    metrics = performance(daily_frame,closed,cfg.initial_cash)
    mdd = float((bars.nav/bars.nav.cummax()-1).min())
    metrics.update(max_drawdown=mdd,calmar=metrics['cagr']/abs(mdd) if metrics['cagr'] is not None and mdd<0 else None,
        max_positions=peak_positions,
        daily_max_drawdown=float((daily_frame.nav/daily_frame.nav.cummax()-1).min()),
        average_holding_minutes=float(closed.holding_minutes.mean()) if len(closed) else None,
        average_net_pnl=float(closed.net_pnl.mean()) if len(closed) else None,
        average_net_return=float(closed.net_return.mean()) if len(closed) else None,
        run_status='completed' if completed else 'incomplete_missing_terminal_price',
        valuation_status='current_prices' if completed else 'provisional_stale_prices',
        quality_status='reference_missing_intraday_path' if quality else 'observed_price_paths',
        rank_eligible=bool(completed and not quality),quality_event_count=len(quality),
        evaluation_sessions=len(daily_frame),ambiguous_oco_trades=int(closed.ambiguous_oco.sum()),
        zero_volume_fill_assumption='約定足の正の出来高で約定可能性を確認する近似',
        holding_time_precision='足内の価格出口は足末時刻で近似')
    nav = daily_frame.nav.copy()
    nav.index = pd.DatetimeIndex([pd.Timestamp(str(d.date())+' '+window_end,tz='Asia/Tokyo') for d in nav.index])
    initial = pd.Series([cfg.initial_cash],index=[pd.Timestamp(str(days[0].date())+' 09:00',tz='Asia/Tokyo')])
    recovery = drawdown_periods(pd.concat([initial,nav]))
    for index, row in recovery.iterrows():
        peak_date = row.peak_date.tz_localize(None).normalize()
        end_date = row.end_date.tz_localize(None).normalize()
        recovery.loc[index,'trading_sessions'] = int(((days>peak_date)&(days<=end_date)).sum())
    daily_nav = pd.concat([initial,nav])
    metrics['daily_max_drawdown'] = float((daily_nav/daily_nav.cummax()-1).min())
    return IntradayResult(bars,daily_frame,closed,held,
        pd.DataFrame(rejected,columns=['ticker','signal_bar_start','signal_time','order_time','reason','candidate_rank']),
        pd.DataFrame(updates,columns=['ticker','trade_id','recorded_at','effective_from','event','stop','target','ratchet_step']),
        pd.DataFrame(quality,columns=['ticker','timestamp','event']),contributions,recovery,metrics,asdict(cfg))
