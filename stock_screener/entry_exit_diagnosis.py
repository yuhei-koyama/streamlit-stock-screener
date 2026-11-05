"""承認済み日本株の固定保有と出口感応度を診断する。"""
import numpy as np
import pandas as pd

from .backtest import Config

HORIZONS = (5, 10, 20, 40, 60)
STOPS = (.03, .05, .08, .10, .12)


def fixed_horizon(execution, signals, calendar, nav, horizon, config=None):
    """全ready日の将来窓を計算し、シグナル標本と銘柄年基準を分ける。"""
    cfg = config or Config()
    days = pd.DatetimeIndex(calendar)
    days = days[(days >= cfg.start) & (days <= cfg.end)]
    records, exclusions = [], []
    for ticker, frame in execution.items():
        prices = frame.reindex(days)
        flags = signals[ticker].reindex(days)
        ready = flags.ready.fillna(False).astype(bool)
        signal = flags.buy_signal.fillna(False).astype(bool)
        buyable = tradable_days(prices, 'CanBuy').shift(-1, fill_value=False)
        sellable = tradable_days(prices, 'CanSell').shift(-horizon-1, fill_value=False)
        exit_open = prices.Open.shift(-horizon-1)
        price = prices.Open.shift(-1) * (1 + cfg.slippage)
        low = prices.Low.rolling(horizon).min().shift(-horizon)
        high = prices.High.rolling(horizon).max().shift(-horizon)
        path_valid = prices[['High','Low','Close']].notna().all(axis=1).rolling(horizon).sum().shift(-horizon).eq(horizon)
        sufficient = pd.Series(np.arange(len(days)) + 1 + horizon < len(days),index=days)
        valid = ready & sufficient & buyable & sellable & path_valid
        reason = pd.Series('missing_path',index=days)
        reason.loc[~sellable] = 'exit_not_tradable'
        reason.loc[~buyable] = 'entry_not_tradable'
        reason.loc[~sufficient] = 'insufficient_horizon'
        excluded = pd.DataFrame(dict(ticker=ticker,signal_date=days,is_signal=signal.to_numpy(),reason=reason.to_numpy()))
        exclusions.append(excluded.loc[(ready & ~valid).to_numpy()])
        returned = exit_open * (1-cfg.slippage) * (1-cfg.commission) / (price*(1+cfg.commission)) - 1
        benchmark = nav.reindex(days).shift(-horizon-1) / nav.reindex(days).shift(-1) - 1
        if benchmark.loc[valid].isna().any():
            raise ValueError('Missing exact-date benchmark NAV')
        entry_dates = pd.Series(days,index=days).shift(-1)
        exit_dates = pd.Series(days,index=days).shift(-horizon-1)
        outcomes_ticker = pd.DataFrame(dict(
            trade_id=[f'{ticker}:{date.date()}' for date in days],ticker=ticker,
            signal_date=days,entry_date=entry_dates,exit_date=exit_dates,
            entry_year=entry_dates.dt.year,is_signal=signal,
            net_return=returned,all_country_return=benchmark,excess_return=returned-benchmark,
            mae=(pd.concat([low,exit_open],axis=1).min(axis=1)/price-1).clip(upper=0),
            mfe=(pd.concat([high,exit_open],axis=1).max(axis=1)/price-1).clip(lower=0)))
        records.append(outcomes_ticker.loc[valid].reset_index(drop=True))
    outcomes = pd.concat(records,ignore_index=True) if records else pd.DataFrame()
    exclusions = pd.concat(exclusions,ignore_index=True) if exclusions else pd.DataFrame()
    if outcomes.empty:
        return outcomes, pd.DataFrame(), exclusions
    baseline = outcomes.groupby(['ticker', 'entry_year']).agg(
        baseline_return=('net_return', 'mean'), baseline_excess=('excess_return', 'mean'),
        baseline_count=('trade_id', 'size')).reset_index()
    outcomes = outcomes.merge(baseline, on=['ticker', 'entry_year'], validate='many_to_one')
    outcomes['timing_advantage'] = outcomes.net_return - outcomes.baseline_return
    outcomes['timing_excess_advantage'] = outcomes.excess_return - outcomes.baseline_excess
    return outcomes, baseline, exclusions


def tradable_days(prices, flag):
    values = prices[['Open','High','Low','Close','Volume']]
    valid = pd.Series(np.isfinite(values.to_numpy(dtype=float)).all(axis=1),index=prices.index)
    valid &= values.gt(0).all(axis=1)
    if flag in prices:
        valid &= prices[flag].fillna(False).astype(bool)
    return valid


def entry_statistics(group):
    return dict(count=len(group), mean_return=group.net_return.mean(),
                benchmark_return=group.all_country_return.mean(),
                excess_return=group.excess_return.mean(),
                median_excess=group.excess_return.median(),
                matched_baseline_return=group.baseline_return.mean(),
                timing_advantage=group.timing_advantage.mean(),
                timing_excess_advantage=group.timing_excess_advantage.mean(),
                mean_mae=group.mae.mean(), mean_mfe=group.mfe.mean())


def recovery_diagnostics(trades, outcomes, execution):
    """実際の損切り後、固定窓内の後日終値で購入価格へ回復したか調べる。"""
    stopped = trades[trades.exit_reason.isin(['stop', 'gap_stop'])]
    joined = stopped[['trade_id', 'entry_price', 'exit_date']].merge(
        outcomes[outcomes.is_signal], on='trade_id', suffixes=('_stop', '_horizon'), validate='one_to_one')
    records = []
    for row in joined.itertuples(index=False):
        after = execution[row.ticker].loc[
            (execution[row.ticker].index > row.exit_date_stop)
            & (execution[row.ticker].index < row.exit_date_horizon), 'Close']
        within = row.exit_date_stop < row.exit_date_horizon
        records.append(dict(trade_id=row.trade_id, ticker=row.ticker,
                            stopped_before_horizon=within,
                            later_close_recovered=bool(after.ge(row.entry_price).any()) if within else False,
                            mae=row.mae, mfe=row.mfe,
                            horizon_return=row.net_return))
    return pd.DataFrame(records)
