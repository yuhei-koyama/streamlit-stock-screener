"""C/Dの入口・Rの更新・約定価格・期末評価・比較を照合する。"""
from pathlib import Path
import json
import sys
import zipfile

import numpy as np
import pandas as pd

root = Path(__file__).resolve().parent
repo = root.parents[2]
sys.path.insert(0, str(repo))
from stock_screener.backtest_io import load_dataset
from stock_screener.benchmark_comparison import load_all_country_nav
from stock_screener.exploratory_data import digest

run, comparison = root / 'run', root / 'comparison'
baseline = repo / 'artifacts/backtest/holding-period-comparison-20261105-01/no-limit-run'
data = repo / 'artifacts/backtest/independent-yahoo-20261105-02/data'
state = json.loads((run / 'run.json').read_text(encoding='utf-8'))
base_state = json.loads((baseline / 'run.json').read_text(encoding='utf-8'))
assert state['status'] == 'completed' and state['completed_conditions'] == 14
assert state['exit_policies'] == ['C', 'D'] and state['settings']['time_exit_enabled'] is False
assert state['input_hashes'] == base_state['input_hashes']
_, execution, _, _, calendar, hashes = load_dataset(data, mode='exploratory')
assert hashes == state['input_hashes']
next_day = dict(zip(calendar[:-1], calendar[1:]))
nav = load_all_country_nav(repo / 'artifacts/backtest/all-country-comparison-20261105-01/253425-original.csv')
summary = pd.read_csv(comparison / 'summary.csv', float_precision='round_trip')
checks = []
for result in summary.itertuples():
    name = f'{result.strategy_id}__{result.exit_policy}'
    folder = run / name
    trades = pd.read_csv(folder / 'trades.csv', float_precision='round_trip')
    positions = pd.read_csv(folder / 'positions.csv', float_precision='round_trip')
    updates = pd.read_csv(folder / 'order_updates.csv', parse_dates=['date', 'effective_from'], float_precision='round_trip')
    reference = baseline / f'{result.strategy_id}__A'
    original_trades = pd.read_csv(reference / 'trades.csv', float_precision='round_trip')
    original_positions = pd.read_csv(reference / 'positions.csv', float_precision='round_trip')
    columns = ['trade_id', 'ticker', 'signal_date', 'entry_date', 'entry_price', 'quantity', 'entry_cost']
    entries = pd.concat([frame[columns] for frame in (trades, positions) if len(frame)], ignore_index=True)
    original_entries = pd.concat([frame[columns] for frame in (original_trades, original_positions) if len(frame)], ignore_index=True)
    pd.testing.assert_frame_equal(entries.sort_values('trade_id').reset_index(drop=True),
                                  original_entries.sort_values('trade_id').reset_index(drop=True), check_exact=True)
    pd.testing.assert_frame_equal(pd.read_csv(folder / 'rejections.csv'), pd.read_csv(reference / 'rejections.csv'))
    assert set(trades.exit_reason).issubset({'stop', 'gap_stop', 'take_profit'})
    joined = updates.merge(entries[['trade_id', 'ticker', 'entry_price', 'entry_date']], on='trade_id', validate='many_to_one')
    np.testing.assert_allclose(joined.initial_risk, joined.entry_price * .08, rtol=0, atol=1e-10)
    np.testing.assert_allclose(joined.stop, joined.entry_price - joined.initial_risk + joined.ratchet_step * joined.initial_risk,
                               rtol=0, atol=1e-10)
    expected_target = joined.entry_price + (3 + (joined.ratchet_step if result.exit_policy == 'D' else 0)) * joined.initial_risk
    np.testing.assert_allclose(joined.target, expected_target, rtol=0, atol=1e-10)
    initial = joined.loc[joined.event.eq('entry')]
    assert len(initial) == len(entries) and initial.trade_id.is_unique
    assert initial.ratchet_step.eq(0).all()
    assert initial.date.eq(pd.to_datetime(initial.entry_date)).all()
    assert initial.effective_from.eq(initial.date).all()
    changes = joined.loc[joined.event.eq('update')]
    assert changes.effective_from.eq(changes.date.map(next_day)).all()
    assert changes.effective_from.le(pd.Timestamp(state['settings']['end'])).all()
    assert joined.groupby('trade_id', sort=False).ratchet_step.diff().dropna().gt(0).all()
    for ticker, group in changes.groupby('ticker'):
        close = execution[ticker].Close.reindex(pd.DatetimeIndex(group.date)).to_numpy()
        lower = group.entry_price.to_numpy() + group.ratchet_step.to_numpy() * group.initial_risk.to_numpy()
        upper = group.entry_price.to_numpy() + (group.ratchet_step.to_numpy() + 1) * group.initial_risk.to_numpy()
        assert (close >= lower).all() and (close < upper).all()
    last = joined.groupby('trade_id', sort=False).last()
    for table, stop_field, target_field in ((trades, 'exit_stop', 'exit_target'), (positions, 'stop', 'target')):
        if len(table):
            selected = last.loc[table.trade_id]
            np.testing.assert_allclose(selected.stop.to_numpy(), table[stop_field].to_numpy(), rtol=0, atol=1e-10)
            np.testing.assert_allclose(selected.target.to_numpy(), table[target_field].to_numpy(), rtol=0, atol=1e-10)
            np.testing.assert_array_equal(selected.ratchet_step.to_numpy(), table.ratchet_step.to_numpy())
    for trade in trades.itertuples():
        row = execution[trade.ticker].loc[pd.Timestamp(trade.exit_date)]
        if trade.exit_reason == 'take_profit':
            expected_price = trade.exit_target
            assert row.High >= trade.exit_target
            if row.Open < trade.exit_target:
                assert row.Low > trade.exit_stop
        elif trade.exit_reason == 'gap_stop':
            assert row.Open <= trade.exit_stop
            expected_price = row.Open * (1 - state['settings']['slippage'])
        else:
            assert row.Open > trade.exit_stop and row.Low <= trade.exit_stop
            expected_price = trade.exit_stop * (1 - state['settings']['slippage'])
        np.testing.assert_allclose(trade.exit_price, expected_price, rtol=0, atol=1e-10)
        expected_ambiguous = (trade.exit_reason == 'stop' and row.Open < trade.exit_target
                              and row.High >= trade.exit_target and row.Low <= trade.exit_stop)
        assert bool(trade.ambiguous_oco) == bool(expected_ambiguous)
    expected_net = trades.quantity * trades.exit_price * (1 - state['settings']['commission']) - 1
    np.testing.assert_allclose(trades.net_return, expected_net, rtol=0, atol=1e-12)
    valued = pd.read_csv(comparison / name / 'all_trades.csv', float_precision='round_trip')
    assert len(valued) == len(entries) and set(valued.trade_id) == set(entries.trade_id)
    indexed = valued.set_index('trade_id')
    np.testing.assert_allclose(indexed.loc[trades.trade_id].strategy_return.to_numpy(dtype=float),
                               trades.net_return.to_numpy(dtype=float), rtol=0, atol=1e-15)
    expected_open = positions.quantity * positions.mark - 1
    np.testing.assert_allclose(indexed.loc[positions.trade_id].strategy_return.to_numpy(dtype=float),
                               expected_open.to_numpy(dtype=float), rtol=0, atol=1e-15)
    expected_benchmark = np.array([nav.loc[pd.Timestamp(row.valuation_date)] / nav.loc[pd.Timestamp(row.entry_date)] - 1
                                   for row in valued.itertuples()])
    np.testing.assert_allclose(valued.all_country_return, expected_benchmark, rtol=0, atol=1e-15)
    np.testing.assert_allclose(valued.excess_return, valued.strategy_return - expected_benchmark, rtol=0, atol=1e-15)
    np.testing.assert_allclose([result.all_mean_strategy_return, result.all_mean_benchmark_return, result.all_mean_excess_return],
                               [valued.strategy_return.mean(), expected_benchmark.mean(), valued.excess_return.mean()], rtol=0, atol=1e-15)
    for field in ('ticker', 'entry_year', 'asset_class'):
        group = pd.read_csv(comparison / name / f'by_{field}_including_open.csv', float_precision='round_trip')
        assert group.all_trade_count.sum() == len(valued)
        np.testing.assert_allclose((group.all_mean_excess_return * group.all_trade_count).sum() / len(valued),
                                   valued.excess_return.mean(), rtol=0, atol=1e-15)
    assert (comparison / name / 'order_updates.csv').read_bytes() == (folder / 'order_updates.csv').read_bytes()
    checks.append(dict(strategy_id=result.strategy_id, exit_policy=result.exit_policy,
                       entries=len(entries), update_count=len(changes),
                       ambiguous_oco_trades=int(trades.ambiguous_oco.sum()),
                       exact_entries=True, order_prices_verified=True, terminal_and_benchmark_verified=True))
for name, expected in state['code_hashes'].items():
    assert digest(run / 'code' / name) == expected
for directory in (run, comparison):
    with zipfile.ZipFile(str(directory) + '.zip') as archive:
        assert archive.testzip() is None
        assert archive.read('REPORT.md') == (directory / 'REPORT.md').read_bytes()
        assert sum(item.file_size for item in archive.infolist()) <= 80_000_000
validation = dict(status='passed', verified_conditions=14,
                  positive_mean_excess_conditions=int(summary.all_mean_excess_return.gt(0).sum()),
                  input_hashes_unchanged=True, conditions=checks)
(root / 'verification.json').write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(dict(status='passed', verified_conditions=14,
                     positive_mean_excess_conditions=validation['positive_mean_excess_conditions'],
                     ambiguous_oco_trades=sum(row['ambiguous_oco_trades'] for row in checks)), indent=2))
