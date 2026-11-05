"""期限あり・なしの入口、価格、期末評価、オルカン比較を照合する。"""
from pathlib import Path
import json
import sys
import zipfile

import numpy as np
import pandas as pd

root = Path(__file__).resolve().parent
repo = root.parents[2]
sys.path.insert(0, str(repo))
from stock_screener.benchmark_comparison import load_all_country_nav
from stock_screener.exploratory_data import digest

old_run = repo / 'artifacts/backtest/independent-yahoo-20261105-02/run'
new_run = root / 'no-limit-run'
old_comparison = root / 'with-limit-comparison'
new_comparison = root / 'no-limit-comparison'
nav_file = repo / 'artifacts/backtest/all-country-comparison-20261105-01/253425-original.csv'
nav = load_all_country_nav(nav_file)
old_state = json.loads((old_run / 'run.json').read_text(encoding='utf-8'))
new_state = json.loads((new_run / 'run.json').read_text(encoding='utf-8'))
assert new_state['status'] == 'completed' and new_state['completed_conditions'] == 14
assert new_state['settings']['time_exit_enabled'] is False
assert new_state['input_hashes'] == old_state['input_hashes']
for field in ('start', 'end', 'initial_stop', 'commission', 'slippage'):
    assert new_state['settings'][field] == old_state['settings'][field]
old_summary = pd.read_csv(old_comparison / 'summary.csv', float_precision='round_trip')
new_summary = pd.read_csv(new_comparison / 'summary.csv', float_precision='round_trip')
records, checks = [], []
for new_row in new_summary.itertuples():
    strategy, policy = new_row.strategy_id, new_row.exit_policy
    name = f'{strategy}__{policy}'
    old_row = old_summary.loc[(old_summary.strategy_id == strategy) & (old_summary.exit_policy == policy)].iloc[0]
    entries = []
    for source in (old_run, new_run):
        closed = pd.read_csv(source / name / 'trades.csv', float_precision='round_trip')
        opened = pd.read_csv(source / name / 'positions.csv', float_precision='round_trip')
        columns = ['trade_id', 'ticker', 'signal_date', 'entry_date', 'entry_price', 'quantity', 'entry_cost']
        bought = pd.concat([table[columns] for table in (closed, opened) if len(table)], ignore_index=True)
        entries.append(bought.sort_values('trade_id').reset_index(drop=True))
        if source == new_run:
            assert not closed.exit_reason.eq('time').any()
            if policy == 'A':
                assert set(closed.exit_reason).issubset({'gap_stop', 'stop'})
    pd.testing.assert_frame_equal(entries[0], entries[1], check_exact=True)
    old_rejections = pd.read_csv(old_run / name / 'rejections.csv')
    new_rejections = pd.read_csv(new_run / name / 'rejections.csv')
    pd.testing.assert_frame_equal(old_rejections, new_rejections)
    observations = []
    for source, comparison in ((old_run, old_comparison), (new_run, new_comparison)):
        valued = pd.read_csv(comparison / name / 'all_trades.csv', float_precision='round_trip')
        assert len(valued) == len(entries[0]) and valued.trade_id.is_unique
        pd.testing.assert_index_equal(pd.Index(sorted(valued.trade_id)), pd.Index(sorted(entries[0].trade_id)))
        closed = pd.read_csv(source / name / 'trades.csv', float_precision='round_trip').set_index('trade_id')
        opened = pd.read_csv(source / name / 'positions.csv', float_precision='round_trip').set_index('trade_id')
        actual = valued.set_index('trade_id')
        np.testing.assert_allclose(actual.loc[closed.index].strategy_return, closed.net_return, rtol=0, atol=1e-15)
        expected_open = opened.quantity * opened.mark - 1
        np.testing.assert_allclose(actual.loc[opened.index].strategy_return.to_numpy(dtype=float),
                                   expected_open.to_numpy(dtype=float), rtol=0, atol=1e-15)
        if len(opened):
            assert actual.loc[opened.index].valuation_date.eq(new_state['settings']['end']).all()
        expected_nav = np.array([nav.loc[pd.Timestamp(row.valuation_date)] / nav.loc[pd.Timestamp(row.entry_date)] - 1
                                 for row in valued.itertuples()])
        np.testing.assert_allclose(valued.all_country_return, expected_nav, rtol=0, atol=1e-15)
        np.testing.assert_allclose(valued.excess_return, valued.strategy_return - expected_nav, rtol=0, atol=1e-15)
        row = old_row if comparison == old_comparison else new_row
        np.testing.assert_allclose([row.all_mean_strategy_return, row.all_mean_benchmark_return, row.all_mean_excess_return],
                                   [valued.strategy_return.mean(), expected_nav.mean(), valued.excess_return.mean()],
                                   rtol=0, atol=1e-15)
        for group in ('ticker', 'entry_year', 'asset_class'):
            grouped = pd.read_csv(comparison / name / f'by_{group}_including_open.csv', float_precision='round_trip')
            assert grouped.all_trade_count.sum() == len(valued)
            np.testing.assert_allclose((grouped.all_mean_excess_return * grouped.all_trade_count).sum() / len(valued),
                                       valued.excess_return.mean(), rtol=0, atol=1e-15)
        observations.append(actual)
    paired = observations[1][['strategy_return', 'excess_return']].join(
        observations[0][['strategy_return', 'excess_return']], lsuffix='_no_limit', rsuffix='_with_limit', validate='one_to_one')
    paired['return_change'] = paired.strategy_return_no_limit - paired.strategy_return_with_limit
    paired['excess_change'] = paired.excess_return_no_limit - paired.excess_return_with_limit
    paired.to_csv(root / f'paired_{name}.csv')
    records.append(dict(
        strategy_id=strategy, exit_policy=policy, entry_count=len(entries[0]),
        with_limit_mean_return=float(old_row.all_mean_strategy_return),
        no_limit_mean_return=float(new_row.all_mean_strategy_return),
        return_change=float(paired.return_change.mean()),
        with_limit_mean_excess=float(old_row.all_mean_excess_return),
        no_limit_mean_benchmark=float(new_row.all_mean_benchmark_return),
        no_limit_mean_excess=float(new_row.all_mean_excess_return),
        excess_change=float(paired.excess_change.mean()),
        with_limit_open_positions=int(old_row.open_positions),
        no_limit_open_positions=int(new_row.open_positions),
        with_limit_average_calendar_days=float(pd.to_datetime(observations[0].valuation_date).sub(
            pd.to_datetime(observations[0].entry_date)).dt.days.mean()),
        no_limit_average_calendar_days=float(pd.to_datetime(observations[1].valuation_date).sub(
            pd.to_datetime(observations[1].entry_date)).dt.days.mean()),
        no_limit_median_excess=float(new_row.all_median_excess_return),
        no_limit_outperform_rate=float(new_row.all_outperform_rate)))
    checks.append(dict(strategy_id=strategy, exit_policy=policy, entries_verified=len(entries[0]),
                       no_time_exits=True, exact_entry_match=True, terminal_valuation_verified=True))

for output in (new_run, old_comparison, new_comparison):
    state = json.loads((output / 'run.json').read_text(encoding='utf-8'))
    assert state['status'] == 'completed' and state['completed_conditions'] == 14
    with zipfile.ZipFile(str(output) + '.zip') as archive:
        assert archive.testzip() is None
        assert archive.read('REPORT.md') == (output / 'REPORT.md').read_bytes()
        assert sum(item.file_size for item in archive.infolist()) <= 80_000_000
for name, expected in new_state['code_hashes'].items():
    assert digest(new_run / 'code' / name) == expected
input_root = repo / 'artifacts/backtest/independent-yahoo-20261105-02/data'
for name, expected in new_state['input_hashes'].items():
    assert digest(input_root / name) == expected
summary = pd.DataFrame(records)
summary.to_csv(root / 'summary.csv', index=False)
verification = dict(status='passed', verified_conditions=14,
                    no_limit_positive_mean_excess_conditions=int(summary.no_limit_mean_excess.gt(0).sum()),
                    original_input_hashes_unchanged=True, exact_entries_match=True,
                    terminal_valuation_verified=True, archive_checks_passed=True, conditions=checks)
(root / 'verification.json').write_text(json.dumps(verification, ensure_ascii=False, indent=2), encoding='utf-8')
print(summary.to_string(index=False))
print('Validation passed for 14 conditions.')
