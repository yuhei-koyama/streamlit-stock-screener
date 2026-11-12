"""固定購入枠・仮想端数株・共通現金による有限資金の主検証。"""
from collections import namedtuple
from dataclasses import asdict, dataclass, field
import math

import numpy as np
import pandas as pd

from .backtest import Config, _exit_at_close, _prepare_backtest, performance
from .ratchet import initialize_ratchet, price_exit, update_ratchet
from .trading_calendar import validate_execution_calendar, trading_day_mask


PriceRow = namedtuple('PriceRow', 'Open High Low Close Volume CanBuy CanSell')
Decision = namedtuple('Decision', 'ready sell_signal rsi')

@dataclass(frozen=True)
class FiniteCapitalConfig:
    start: str = '2021-01-04'
    end: str = '2025-12-30'
    initial_cash: float = 10_000_000
    purchase_budget: float = 100_000
    commission: float = .001
    slippage: float = .0005
    initial_stop: float = .08
    ratchet_reference_stop: float = .08
    time_exit_enabled: bool = field(default=False, init=False)

    def __post_init__(self):
        Config(start=self.start, end=self.end, initial_cash=self.initial_cash,
               commission=self.commission, slippage=self.slippage,
               initial_stop=self.initial_stop, time_exit_enabled=False)
        if not math.isfinite(self.purchase_budget) or self.purchase_budget <= 0:
            raise ValueError('購入枠は有限の正数が必要です')
        if not 0 < self.ratchet_reference_stop < 1:
            raise ValueError('切り上げ基準幅が不正です')


@dataclass
class FiniteCapitalResult:
    daily: pd.DataFrame
    trades: pd.DataFrame
    positions: pd.DataFrame
    rejections: pd.DataFrame
    order_updates: pd.DataFrame
    drawdown_periods: pd.DataFrame
    contributions: pd.DataFrame
    annual_returns: pd.Series
    monthly_returns: pd.Series
    metrics: dict
    config: dict


def drawdown_periods(nav):
    """同額最高値の最後の日から回復までを営業日・暦日で数える。"""
    columns = ['peak_date', 'trough_date', 'recovery_date', 'end_date',
               'max_drawdown', 'recovered', 'trading_sessions', 'calendar_days']
    records = []
    peak = float(nav.iloc[0])
    peak_date = nav.index[0]
    peak_session = 0
    active = None
    for session, (date, value) in enumerate(nav.items()):
        if value >= peak:
            if active is not None:
                records.append({**active, 'recovery_date': date, 'end_date': date,
                                'recovered': True, 'trading_sessions': session - peak_session,
                                'calendar_days': (date - peak_date).days})
                active = None
            peak, peak_date, peak_session = float(value), date, session
        else:
            decline = float(value / peak - 1)
            if active is None:
                active = dict(peak_date=peak_date, trough_date=date, max_drawdown=decline)
            elif decline < active['max_drawdown']:
                active.update(trough_date=date, max_drawdown=decline)
    if active is not None:
        end = nav.index[-1]
        records.append({**active, 'recovery_date': pd.NaT, 'end_date': end,
                        'recovered': False, 'trading_sessions': len(nav) - 1 - peak_session,
                        'calendar_days': (end - peak_date).days})
    return pd.DataFrame(records, columns=columns)


def _period_returns(nav, rule, initial_cash):
    ends = nav.resample(rule).last()
    bases = ends.shift(1)
    bases.iloc[0] = initial_cash
    return ends / bases - 1


def run_finite_capital_backtest(execution, signals, strategy_id, exit_policy,
                                calendar, asset_classes, config=None):
    cfg = config or FiniteCapitalConfig()
    if not isinstance(cfg, FiniteCapitalConfig):
        raise ValueError('有限資金用の設定が必要です')
    if len(set(asset_classes.values())) != 1:
        raise ValueError('個別株とETFは独立したポートフォリオで実行してください')
    validate_execution_calendar(execution, signals, calendar)
    market, flags, days = _prepare_backtest(
        execution, signals, strategy_id, exit_policy, calendar, asset_classes, cfg)
    expected = pd.date_range(days[0], days[-1])
    expected = expected[trading_day_mask(expected)]
    if not days.equals(expected):
        raise ValueError('評価期間の取引営業日が欠けています')
    for frame in market.values():
        if not frame.Split.eq(1).all() or not frame.Dividend.eq(0).all():
            raise ValueError('主検証には分割・配当を含む調整価格が必要です')
    price_values, buy_allowed, sell_allowed, decision_values = {}, {}, {}, {}
    for ticker, frame in market.items():
        values = frame[['Open', 'High', 'Low', 'Close', 'Volume']].to_numpy(dtype=float)
        valid = np.isfinite(values).all(axis=1) & (values[:, 4] > 0)
        price_values[ticker] = values
        buy_allowed[ticker] = np.asarray(valid & frame.get('CanBuy', True))
        sell_allowed[ticker] = np.asarray(valid & frame.get('CanSell', True))
        decision = flags[ticker]
        decision_values[ticker] = (decision.ready.to_numpy(), decision.sell_signal.to_numpy(),
            decision.rsi.to_numpy() if 'rsi' in decision else np.full(len(decision), np.nan))
    candidates = {}
    for ticker, frame in flags.items():
        dates = frame.index[frame.ready & frame.buy_signal]
        for date in dates[(dates >= days[0]) & (dates <= days[-1])]:
            candidates.setdefault(date, []).append(ticker)
    cash = float(cfg.initial_cash)
    cumulative_costs = 0.0
    positions, buys = {}, []
    trades, rejected, daily, updates = [], [], [], []
    is_ratchet = exit_policy in ('C', 'D')

    def row_at(ticker, date):
        try:
            index = market[ticker].index.get_loc(date)
        except KeyError:
            return None
        return PriceRow(*price_values[ticker][index],
                        bool(buy_allowed[ticker][index]),
                        bool(sell_allowed[ticker][index]))

    def signal_at(ticker, date):
        try:
            index = flags[ticker].index.get_loc(date)
        except KeyError:
            return None
        ready, sell_signal, rsi = decision_values[ticker]
        return Decision(bool(ready[index]), bool(sell_signal[index]), float(rsi[index]))

    def reject(ticker, date, signal_date, reason, rank=None):
        rejected.append(dict(ticker=ticker, date=date, signal_date=signal_date,
                             reason=reason, candidate_rank=rank))

    def record_order(pos, date, effective_from, event):
        updates.append(dict(ticker=pos['ticker'], trade_id=pos['trade_id'], date=date,
                            effective_from=effective_from, event=event, stop=pos['stop'],
                            target=pos.get('target'), ratchet_step=pos.get('ratchet_step'),
                            initial_risk=pos.get('initial_risk')))

    def sell(ticker, date, session, base, reason, apply_slippage=True, ambiguous=False):
        nonlocal cash, cumulative_costs
        pos = positions.pop(ticker)
        price = base * (1 - cfg.slippage) if apply_slippage else base
        gross = pos['quantity'] * price
        fee = gross * cfg.commission
        slip = pos['quantity'] * (base - price)
        proceeds = gross - fee
        pnl = proceeds - cfg.purchase_budget
        cash += proceeds
        cumulative_costs += fee + slip
        trades.append(dict(ticker=ticker, trade_id=pos['trade_id'],
                           asset_class=asset_classes[ticker], signal_date=pos['signal_date'],
                           entry_date=pos['entry_date'], exit_date=date, exit_reason=reason,
                           entry_price=pos['entry_price'], exit_price=price, quantity=pos['quantity'],
                           entry_cost=cfg.purchase_budget, net_pnl=pnl, net_return=pnl / cfg.purchase_budget,
                           costs=pos['entry_costs'] + fee + slip,
                           holding_sessions=session - pos['entry_session'],
                           ambiguous_oco=ambiguous, exit_stop=pos['stop'], exit_target=pos.get('target'),
                           ratchet_step=pos.get('ratchet_step'), initial_risk=pos.get('initial_risk')))

    for session, date in enumerate(days):
        # 前引けまでの売却予約と寄付の価格出口を、全ての買い注文より先に処理する。
        for ticker, pos in list(positions.items()):
            row = row_at(ticker, date)
            if row is not None and row.CanSell:
                fill = price_exit(pos, row)
                if fill is not None:
                    sell(ticker, date, session, fill.base_price, fill.reason,
                         fill.apply_slippage, fill.ambiguous)
                elif pos['pending_exit']:
                    sell(ticker, date, session, float(row.Open), pos['pending_exit'])
        for rank, (ticker, signal_date) in enumerate(sorted(buys), 1):
            row = row_at(ticker, date)
            if ticker in positions:
                reject(ticker, date, signal_date, 'duplicate_position', rank)
            elif row is None or not row.CanBuy:
                reject(ticker, date, signal_date, 'not_tradable', rank)
            elif cash + 1e-8 < cfg.purchase_budget:
                reject(ticker, date, signal_date, 'insufficient_cash', rank)
            else:
                price = float(row.Open) * (1 + cfg.slippage)
                quantity = cfg.purchase_budget / (price * (1 + cfg.commission))
                entry_costs = quantity * (price * cfg.commission + price - float(row.Open))
                cash -= cfg.purchase_budget
                cumulative_costs += entry_costs
                pos = dict(ticker=ticker, trade_id=f'{ticker}:{date.date()}',
                           signal_date=signal_date, entry_date=date, entry_session=session,
                           entry_price=price, quantity=quantity, entry_costs=entry_costs,
                           peak=price, stop=price * (1 - cfg.initial_stop),
                           pending_exit=None, mark=float(row.Open), mark_date=date,
                           mark_session=session)
                if is_ratchet:
                    initialize_ratchet(pos, cfg.initial_stop, cfg.ratchet_reference_stop)
                positions[ticker] = pos
                record_order(pos, date, date, 'entry')
        buys = []
        # 日中の売却代金は、既に処理した寄付購入へ遡って使用しない。
        for ticker, pos in list(positions.items()):
            row = row_at(ticker, date)
            if row is not None and row.CanSell:
                fill = price_exit(pos, row, include_intraday=True)
                if fill is not None:
                    sell(ticker, date, session, fill.base_price, fill.reason,
                         fill.apply_slippage, fill.ambiguous)
                    continue
            if row is not None and pd.notna(row.Close):
                pos.update(mark=float(row.Close), mark_date=date, mark_session=session)
            signal = signal_at(ticker, date)
            previous_stop = pos['stop']
            reason = _exit_at_close(pos, row, signal, session, strategy_id, exit_policy,
                                    time_exit_enabled=False)
            if reason and not pos['pending_exit']:
                pos['pending_exit'] = reason
            changed = pos['stop'] != previous_stop
            if is_ratchet and row is not None and row.CanSell:
                changed = update_ratchet(pos, float(row.Close), exit_policy)
            if changed and session + 1 < len(days):
                record_order(pos, date, days[session + 1], 'update')
        for ticker in sorted(candidates.get(date, [])):
            if ticker in positions:
                reject(ticker, date, date, 'duplicate_position')
            elif session + 1 == len(days):
                reject(ticker, date, date, 'outside_test_period')
            else:
                buys.append((ticker, date))
        value = sum(pos['quantity'] * pos['mark'] for pos in positions.values())
        if cash < -1e-6:
            raise ArithmeticError('現金残高が負です')
        daily.append(dict(date=date, cash=cash, market_value=value, nav=cash + value,
                          positions=len(positions), costs=cumulative_costs, receivables=0.0,
                          stale_positions=sum(pos['mark_date'] < date for pos in positions.values())))
    daily = pd.DataFrame(daily).set_index('date')
    trade_columns = ['ticker', 'trade_id', 'asset_class', 'signal_date', 'entry_date', 'exit_date',
                     'exit_reason', 'entry_price', 'exit_price', 'quantity', 'entry_cost', 'net_pnl',
                     'net_return', 'costs', 'holding_sessions', 'ambiguous_oco', 'exit_stop',
                     'exit_target', 'ratchet_step', 'initial_risk']
    trades = pd.DataFrame(trades, columns=trade_columns)
    held = []
    for pos in positions.values():
        value = pos['quantity'] * pos['mark']
        held.append({**pos, 'asset_class': asset_classes[pos['ticker']], 'entry_cost': cfg.purchase_budget,
                     'market_value': value, 'unrealized_pnl': value - cfg.purchase_budget,
                     'stale_mark': pos['mark_date'] < days[-1],
                     'stale_trading_sessions': len(days) - 1 - pos['mark_session'],
                     'holding_sessions': len(days) - 1 - pos['entry_session']})
    held = pd.DataFrame(held, columns=['ticker', 'trade_id', 'asset_class', 'signal_date', 'entry_date',
                        'entry_price', 'quantity', 'entry_cost', 'entry_costs', 'mark', 'mark_date',
                        'market_value', 'unrealized_pnl', 'stale_mark', 'stale_trading_sessions',
                        'holding_sessions', 'pending_exit', 'stop', 'target', 'initial_risk', 'ratchet_step'])
    contributions = []
    for ticker in sorted(market):
        closed = trades.loc[trades.ticker.eq(ticker), 'net_pnl']
        opened = held.loc[held.ticker.eq(ticker)]
        pnl = float(closed.sum() + opened.unrealized_pnl.sum())
        contributions.append(dict(ticker=ticker, realized_pnl=float(closed.sum()),
                                  unrealized_pnl=float(opened.unrealized_pnl.sum()),
                                  total_pnl=pnl, contribution_return=pnl / cfg.initial_cash,
                                  final_market_value=float(opened.market_value.sum())))
    contributions = pd.DataFrame(contributions)
    if not np.isclose(contributions.total_pnl.sum(), daily.nav.iloc[-1] - cfg.initial_cash,
                      rtol=1e-10, atol=1e-6):
        raise ArithmeticError('純資産と銘柄別損益の資金会計が一致しません')
    drawdowns = drawdown_periods(daily.nav)
    metrics = performance(daily, trades, cfg.initial_cash)
    metrics.update(initial_cash=cfg.initial_cash, purchase_budget=cfg.purchase_budget,
                   rejected_orders=len(rejected), duplicate_signals=sum(r['reason'] == 'duplicate_position' for r in rejected),
                   insufficient_cash_orders=sum(r['reason'] == 'insufficient_cash' for r in rejected),
                   average_invested_ratio=float((daily.market_value / daily.nav).mean()),
                   stale_open_positions=int(held.stale_mark.sum()),
                   valuation_status='provisional_stale_prices' if held.stale_mark.any() else 'current_prices',
                   comparison_status='pending_stale_price_review' if held.stale_mark.any() else 'benchmark_required')
    metrics['max_drawdown_period'] = drawdowns.loc[drawdowns.max_drawdown.idxmin()].to_dict() if len(drawdowns) else None
    metrics['longest_drawdown_period'] = drawdowns.loc[drawdowns.trading_sessions.idxmax()].to_dict() if len(drawdowns) else None
    return FiniteCapitalResult(daily, trades, held,
        pd.DataFrame(rejected, columns=['ticker', 'date', 'signal_date', 'reason', 'candidate_rank']),
        pd.DataFrame(updates, columns=['ticker', 'trade_id', 'date', 'effective_from', 'event',
                                     'stop', 'target', 'ratchet_step', 'initial_risk']),
        drawdowns, contributions, _period_returns(daily.nav, 'YE', cfg.initial_cash),
        _period_returns(daily.nav, 'ME', cfg.initial_cash), metrics, asdict(cfg))


def excluded_row_exposure(result, excluded_rows):
    """除外日に保有中だった取引と、買い見送りを条件別に集計する。"""
    cfg = result.config
    rows = excluded_rows[['ticker', 'Date']].copy()
    rows['Date'] = pd.to_datetime(rows.Date)
    if rows.duplicated(['ticker', 'Date']).any() or rows.Date.isna().any():
        raise ValueError('除外日付が欠損または重複しています')
    rows = rows[rows.ticker.isin(result.contributions.ticker)
                & rows.Date.ge(pd.Timestamp(cfg['start'])) & rows.Date.le(pd.Timestamp(cfg['end']))]
    closed = result.trades[['ticker', 'trade_id', 'entry_date', 'exit_date', 'entry_cost']].copy()
    opened = result.positions[['ticker', 'trade_id', 'entry_date', 'entry_cost']].copy()
    opened['exit_date'] = pd.Timestamp(cfg['end']) + pd.Timedelta(days=1)
    intervals = pd.concat([frame for frame in (closed, opened) if len(frame)], ignore_index=True) if len(closed) or len(opened) else closed
    intervals['entry_date'] = pd.to_datetime(intervals.entry_date)
    intervals['exit_date'] = pd.to_datetime(intervals.exit_date)
    matches = rows.merge(intervals, on='ticker', how='left')
    held = matches[matches.entry_date.le(matches.Date) & matches.exit_date.gt(matches.Date)]
    if held.duplicated(['ticker', 'Date']).any():
        raise ArithmeticError('除外日の同一銘柄に複数の保有が存在します')
    held = held[['ticker', 'Date', 'trade_id', 'entry_date', 'entry_cost']].rename(
        columns={'trade_id': 'held_trade_id', 'entry_date': 'held_entry_date', 'entry_cost': 'held_entry_cost'})
    effects = rows.merge(held, on=['ticker', 'Date'], how='left')
    effects['held_position'] = effects.held_trade_id.notna()
    rejected = result.rejections[result.rejections.reason.eq('not_tradable')][['ticker', 'date']].copy()
    rejected['date'] = pd.to_datetime(rejected.date)
    rejected = rejected.rename(columns={'date': 'Date'}).drop_duplicates()
    rejected['buy_rejected'] = True
    effects = effects.merge(rejected, on=['ticker', 'Date'], how='left')
    effects['buy_rejected'] = effects.buy_rejected.eq(True)
    return effects, dict(excluded_price_rows_in_evaluation=len(rows),
                         excluded_price_rows_while_held=int(effects.held_position.sum()),
                         excluded_price_rows_with_buy_rejection=int(effects.buy_rejected.sum()))
