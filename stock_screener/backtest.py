"""Daily long-only execution/accounting. Inputs are explicitly nominal prices.

The engine consumes screener outputs and never calculates entry indicators.
"""
from dataclasses import dataclass, asdict
import math
import numpy as np
import pandas as pd

from .backtest_signals import MINERVINI, RSI2, RSI14


@dataclass(frozen=True)
class Config:
    start: str = '2021-01-04'
    end: str = '2025-12-30'
    initial_cash: float = 10_000_000
    max_positions: int = 5
    allocation: float = .20
    commission: float = .001
    slippage: float = .0005
    initial_stop: float = .08
    time_exit_enabled: bool = True

    def __post_init__(self):
        if not isinstance(self.time_exit_enabled, bool):
            raise ValueError('time_exit_enabled must be boolean')
        if pd.Timestamp(self.start) >= pd.Timestamp(self.end):
            raise ValueError('start must precede end')
        if not math.isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ValueError('initial_cash must be positive')
        if isinstance(self.max_positions, bool) or not isinstance(self.max_positions, int) or self.max_positions < 1:
            raise ValueError('max_positions must be a positive integer')
        if not 0 < self.allocation <= 1 or not 0 < self.initial_stop < 1:
            raise ValueError('invalid allocation/stop')
        if not 0 <= self.commission < 1 or not 0 <= self.slippage < 1:
            raise ValueError('invalid costs')


@dataclass
class Result:
    daily: pd.DataFrame
    trades: pd.DataFrame
    rejections: pd.DataFrame
    positions: pd.DataFrame
    metrics: dict
    annual_returns: pd.Series
    monthly_returns: pd.Series
    asset_groups: pd.DataFrame
    config: dict


def _dates(index):
    index = pd.DatetimeIndex(index)
    if index.tz is not None:
        index = index.tz_convert('Asia/Tokyo').tz_localize(None)
    return index.normalize()


def _normalized(frame):
    out = frame.copy()
    out.index = _dates(out.index)
    if not out.index.is_unique or not out.index.is_monotonic_increasing:
        raise ValueError('index must be ascending and unique')
    return out


def _validate_execution(frame):
    required = ['Open','High','Low','Close','Volume','LotSize','Split','Dividend','DividendPayDate']
    if not set(required).issubset(frame.columns):
        raise ValueError(f'Nominal execution data requires {required}')
    for col in required[:-1]:
        if not pd.api.types.is_numeric_dtype(frame[col]):
            raise ValueError(f'{col} must be numeric')
    prices = frame[['Open','High','Low','Close']]
    if (prices.notna() & ~np.isfinite(prices)).any().any() or (prices <= 0).any().any():
        raise ValueError('prices must be positive and finite')
    if ((frame.High < frame.Low) | (frame.High < frame[['Open','Close']].max(axis=1))
        | (frame.Low > frame[['Open','Close']].min(axis=1))).any():
        raise ValueError('invalid OHLC range')
    if ((frame.Volume < 0) | (frame.Volume.notna() & ~np.isfinite(frame.Volume))).any():
        raise ValueError('Volume must be nonnegative and finite when present')
    for name in ('CanBuy','CanSell'):
        if name in frame and (not pd.api.types.is_bool_dtype(frame[name]) or frame[name].isna().any()):
            raise ValueError(f'{name} must contain explicit non-null booleans')
    for name in ('LotSize','Split'):
        if frame[name].isna().any() or not np.isfinite(frame[name]).all() or (frame[name] <= 0).any():
            raise ValueError(f'{name} must be finite and positive; do not guess')
    if (frame.LotSize % 1 != 0).any():
        raise ValueError('LotSize must be integer')
    if frame.Dividend.isna().any() or not np.isfinite(frame.Dividend).all() or (frame.Dividend < 0).any():
        raise ValueError('invalid Dividend')
    for date, row in frame[frame.Dividend > 0].iterrows():
        if pd.isna(row.DividendPayDate) or pd.Timestamp(row.DividendPayDate).normalize() < date:
            raise ValueError('Dividend requires a known payment date on/after ex-date')


def _tradable(row, side):
    if row is None or not np.isfinite(row[['Open','High','Low','Close','Volume']].astype(float)).all():
        return False
    return row.Volume > 0 and bool(row.get('CanBuy' if side == 'buy' else 'CanSell', True))


def performance(daily, trades, initial_cash):
    nav = daily.nav
    years = (nav.index[-1] - nav.index[0]).days / 365.2425
    total = nav.iloc[-1] / initial_cash - 1
    cagr = (nav.iloc[-1] / initial_cash) ** (1 / years) - 1 if years > 0 else None
    dd = nav / nav.cummax() - 1
    returns = nav.pct_change().dropna()
    sd = returns.std(ddof=1)
    pnl = trades.net_pnl if not trades.empty else pd.Series(dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    mdd = float(dd.min())
    return dict(cagr=float(cagr) if cagr is not None else None, total_return=float(total),
                max_drawdown=mdd,
                sharpe=float(returns.mean()/sd*np.sqrt(252)) if pd.notna(sd) and sd > 0 else None,
                calmar=float(cagr/abs(mdd)) if cagr is not None and mdd < 0 else None,
                trade_count=len(pnl), win_rate=float((pnl > 0).mean()) if len(pnl) else None,
                average_win_return=float(trades.loc[pnl > 0,'net_return'].mean()) if len(wins) else None,
                average_loss_return=float(trades.loc[pnl < 0,'net_return'].mean()) if len(losses) else None,
                profit_factor=float(wins.sum()/abs(losses.sum())) if len(losses) else None,
                average_holding_sessions=float(trades.holding_sessions.mean()) if len(pnl) else None,
                total_costs=float(daily.costs.iloc[-1]),
                average_cash_ratio=float((daily.cash/daily.nav).mean()),
                max_positions=int(daily.positions.max()), final_nav=float(nav.iloc[-1]),
                final_receivables=float(daily.receivables.iloc[-1]),
                open_positions=int(daily.positions.iloc[-1]))


def _prepare_backtest(execution, signals, strategy_id, exit_policy, calendar, asset_classes, cfg):
    if exit_policy not in ('A','B','C','D'):
        raise ValueError('exit_policy must be A, B, C or D')
    if not execution or set(execution) != set(signals) or set(execution) != set(asset_classes):
        raise ValueError('execution/signals/asset_classes must cover the same nonempty universe')
    market = {t:_normalized(f) for t,f in execution.items()}
    decisions = {t:_normalized(f) for t,f in signals.items()}
    for ticker, frame in market.items():
        _validate_execution(frame)
        if asset_classes[ticker] not in ('stock','etf'):
            raise ValueError('asset class must be stock or etf')
        if not {'buy_signal','sell_signal','ready','strategy_id'}.issubset(decisions[ticker]):
            raise ValueError('signals missing required fields')
        if not decisions[ticker].strategy_id.eq(strategy_id).all():
            raise ValueError('mixed strategy ids')
        if not decisions[ticker].index.equals(frame.index):
            raise ValueError('execution and signal dates must match exactly')
        for name in ('buy_signal','sell_signal','ready'):
            if not pd.api.types.is_bool_dtype(decisions[ticker][name]) or decisions[ticker][name].isna().any():
                raise ValueError('signal flags must be non-null booleans')
        if strategy_id in (RSI2, RSI14) and 'rsi' not in decisions[ticker]:
            raise ValueError('RSI recovery requires screener RSI output')
    all_days = _dates(calendar)
    if not all_days.is_unique or not all_days.is_monotonic_increasing:
        raise ValueError('calendar must be ascending and unique')
    if len(all_days) == 0 or all_days.min() > pd.Timestamp(cfg.start) or all_days.max() < pd.Timestamp(cfg.end):
        raise ValueError('calendar must cover the full requested period; do not silently shorten a backtest')
    for frame in market.values():
        if not frame.index.isin(all_days).all():
            raise ValueError('calendar must include all input sessions including warmup')
    days = all_days[(all_days >= pd.Timestamp(cfg.start)) & (all_days <= pd.Timestamp(cfg.end))]
    if len(days) < 2:
        raise ValueError('at least two evaluation sessions required')
    return market, decisions, days


def _exit_at_close(pos, row, signal, session, strategy_id, exit_policy, time_exit_enabled=True):
    if row is not None and pd.notna(row.High):
        pos['peak']=max(pos['peak'],float(row.High))
        if exit_policy=='B' and strategy_id==MINERVINI:
            pos['stop']=max(pos['stop'],pos['peak']*.9)
    holding=session-pos['entry_session']+1
    max_days=20 if exit_policy in ('A','C','D') else (5 if strategy_id in (RSI2,RSI14) else 40 if strategy_id==MINERVINI else 20)
    reason = 'time' if time_exit_enabled and holding >= max_days else None
    if exit_policy=='B' and signal is not None and signal.ready:
        if strategy_id in (RSI2,RSI14):
            if signal.rsi >= 50:
                reason='rsi_recovery'
        elif strategy_id!=MINERVINI and signal.sell_signal:
            reason='opposite_signal'
    return reason


def run_backtest(execution, signals, strategy_id, exit_policy, calendar, asset_classes, config=None):
    """Signals at today's close can only execute at the next calendar session.

    execution: nominal OHLCV and explicit corporate actions/lot sizes, not auto-adjusted.
    signals: output of backtest_signals.screening_signals using the screener price series.
    """
    cfg = config or Config()
    if exit_policy not in ('A','B'):
        raise ValueError('Portfolio evaluation supports exits A/B; use independent evaluation for C/D')
    market, decisions, days = _prepare_backtest(
        execution, signals, strategy_id, exit_policy, calendar, asset_classes, cfg)
    cash = float(cfg.initial_cash)
    positions, last_marks = {}, {}
    receivables, trades, rejected, daily = [], [], [], []
    buys, sells = {}, {}
    cumulative_costs = 0.
    # Row lookup once, rather than millions of repeated DataFrame index operations.
    rows = {t:dict(f.iterrows()) for t,f in market.items()}
    flags = {t:dict(f.iterrows()) for t,f in decisions.items()}

    def row_at(ticker, date):
        return rows[ticker].get(date)

    def reject(ticker,date,reason,signal_date):
        rejected.append(dict(ticker=ticker,date=date,reason=reason,signal_date=signal_date))

    def sell(ticker,date,session,base_price,reason):
        nonlocal cash, cumulative_costs
        pos = positions.pop(ticker)
        price = base_price * (1-cfg.slippage)
        gross = pos['quantity'] * price
        fee = gross * cfg.commission
        slip = pos['quantity'] * (base_price-price)
        cumulative_costs += fee+slip
        cash += gross-fee
        pnl = gross-fee+pos['dividends']-pos['entry_cost']
        trades.append(dict(ticker=ticker,asset_class=asset_classes[ticker],
                           signal_date=pos['signal_date'],entry_date=pos['entry_date'],exit_date=date,
                           entry_price=pos['entry_fill_price'],entry_quantity=pos['entry_quantity'],
                           adjusted_entry_price=pos['entry_price'],exit_price=price,quantity=pos['quantity'],
                           exit_reason=reason,entry_cost=pos['entry_cost'],net_pnl=pnl,
                           net_return=pnl/pos['entry_cost'],dividends=pos['dividends'],
                           costs=pos['entry_fees_slip']+fee+slip,
                           holding_sessions=session-pos['entry_session']))
        sells.pop(ticker,None)

    for session, date in enumerate(days):
        # Corporate actions occur before today's orders; dividends belong to overnight holders.
        for ticker,pos in list(positions.items()):
            row = row_at(ticker,date)
            if row is not None:
                split = float(row.Split)
                quantity = pos['quantity']*split
                if not np.isclose(quantity,round(quantity)):
                    raise ValueError('fractional split entitlement requires cash-in-lieu data')
                pos['quantity'] = int(round(quantity))
                for name in ('entry_price','peak','stop'):
                    pos[name] /= split
                if ticker in last_marks:
                    last_marks[ticker] /= split
                if row.Dividend > 0:
                    amount = pos['quantity']*float(row.Dividend)
                    pos['dividends'] += amount
                    receivables.append((pd.Timestamp(row.DividendPayDate).normalize(),amount))
        cash += sum(amount for due,amount in receivables if due <= date)
        receivables = [(due,amount) for due,amount in receivables if due > date]
        closed_today=set()
        for ticker in sorted(list(sells)):
            row=row_at(ticker,date)
            if ticker in positions and _tradable(row,'sell'):
                reason = 'gap_stop' if row.Open <= positions[ticker]['stop'] else sells[ticker]
                sell(ticker,date,session,float(row.Open),reason)
                closed_today.add(ticker)
        # Overnight gap stops are open sells too, so their cash precedes open buys.
        for ticker,pos in list(positions.items()):
            row=row_at(ticker,date)
            if _tradable(row,'sell') and row.Open <= pos['stop']:
                sell(ticker,date,session,float(row.Open),'gap_stop')
                closed_today.add(ticker)
        for ticker,signal_date in sorted(buys.items()):
            row=row_at(ticker,date)
            if ticker in closed_today or ticker in positions:
                continue
            if not _tradable(row,'buy'):
                reject(ticker,date,'not_tradable',signal_date)
                continue
            if len(positions) >= cfg.max_positions:
                reject(ticker,date,'position_limit',signal_date)
                continue
            open_value = 0.
            for held,pos in positions.items():
                held_row=row_at(held,date)
                mark=float(held_row.Open) if held_row is not None and pd.notna(held_row.Open) else last_marks[held]
                open_value += pos['quantity']*mark
            nav_open=cash+open_value+sum(amount for _,amount in receivables)
            budget=min(cash,nav_open*cfg.allocation)
            price=float(row.Open)*(1+cfg.slippage)
            lot=int(row.LotSize)
            quantity=int(budget/(price*(1+cfg.commission))//lot)*lot
            if quantity <= 0:
                reject(ticker,date,'insufficient_cash_or_lot',signal_date)
                continue
            gross=quantity*price
            fee=gross*cfg.commission
            slip=quantity*(price-float(row.Open))
            cash -= gross+fee
            cumulative_costs += fee+slip
            positions[ticker]=dict(quantity=quantity,entry_cost=gross+fee,
                                   entry_price=price,entry_fill_price=price,entry_quantity=quantity,
                                   entry_date=date,entry_session=session,
                                   signal_date=signal_date,peak=price,stop=price*(1-cfg.initial_stop),
                                   dividends=0.,entry_fees_slip=fee+slip)
            last_marks[ticker]=float(row.Open)
        buys={}
        # A stop effective before this session cannot use this session's new high.
        for ticker,pos in list(positions.items()):
            row=row_at(ticker,date)
            if _tradable(row,'sell') and row.Low <= pos['stop']:
                base=min(float(row.Open),pos['stop'])
                sell(ticker,date,session,base,'gap_stop' if row.Open <= pos['stop'] else 'stop')
                closed_today.add(ticker)
        for ticker,pos in positions.items():
            row=row_at(ticker,date)
            if row is not None and pd.notna(row.Close):
                last_marks[ticker]=float(row.Close)
            signal=flags[ticker].get(date)
            reason = _exit_at_close(pos, row, signal, session, strategy_id, exit_policy,
                                    time_exit_enabled=cfg.time_exit_enabled)
            if reason:
                sells[ticker]=reason
        for ticker in sorted(market):
            signal=flags[ticker].get(date)
            if ticker not in positions and signal is not None and signal.ready and signal.buy_signal:
                buys[ticker]=date
        value=sum(pos['quantity']*last_marks[t] for t,pos in positions.items())
        owing=sum(amount for _,amount in receivables)
        stale=sum(row_at(t,date) is None or pd.isna(row_at(t,date).Close) for t in positions)
        if cash < -1e-6:
            raise ArithmeticError('negative cash')
        daily.append(dict(date=date,cash=cash,market_value=value,receivables=owing,
                          nav=cash+value+owing,positions=len(positions),costs=cumulative_costs,
                          stale_positions=stale))
    daily=pd.DataFrame(daily).set_index('date')
    trades=pd.DataFrame(trades)
    metrics=performance(daily,trades,cfg.initial_cash)
    def period_returns(rule):
        ends=daily.nav.resample(rule).last()
        previous=ends.shift(1)
        previous.iloc[0]=cfg.initial_cash
        return ends/previous-1
    grouped=[]
    for group in ('stock','etf'):
        subset=trades[trades.asset_class==group] if not trades.empty else trades
        grouped.append(dict(asset_class=group,closed_trades=len(subset),
                            realized_net_pnl=float(subset.net_pnl.sum()) if len(subset) else 0.,
                            win_rate=float((subset.net_pnl>0).mean()) if len(subset) else None,
                            open_market_value=sum(p['quantity']*last_marks[t] for t,p in positions.items() if asset_classes[t]==group)))
    for ticker,signal_date in buys.items():
        reject(ticker,days[-1],'outside_test_period',signal_date)
    open_positions=pd.DataFrame([dict(ticker=t,asset_class=asset_classes[t],**p,
                                      mark=last_marks[t],pending_exit=sells.get(t,''))
                                 for t,p in positions.items()])
    return Result(daily,trades,pd.DataFrame(rejected),open_positions,metrics,
                  period_returns('YE'),period_returns('ME'),pd.DataFrame(grouped),
                  dict(**asdict(cfg),strategy_id=strategy_id,exit_policy=exit_policy))
