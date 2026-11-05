"""保存済みの独立仮想取引と入口シグナル・損益集計を照合する。"""
from pathlib import Path
import hashlib
import json
import sys
import zipfile

import numpy as np
import pandas as pd

root = Path(__file__).resolve().parent
repo = root.parents[2]
sys.path.insert(0, str(repo))
from stock_screener.backtest_io import load_dataset
from stock_screener.backtest_signals import screening_signals

run = root / 'run'
status = json.loads((run / 'run.json').read_text(encoding='utf-8'))
assert status['status'] == 'completed' and status['completed_conditions'] == 14
assert status['evaluation_method'] == 'independent' and status['kind'] == 'market'
summary = pd.read_csv(run / 'summary.csv', float_precision='round_trip')
assert len(summary) == 14
manifest, execution, inputs, classes, calendar, hashes = load_dataset(root / 'data', mode='exploratory')
assert hashes == status['input_hashes']
correction = json.loads((root / 'data/correction.json').read_text(encoding='utf-8'))
assert correction['output_hashes'] == hashes
assert (correction['corrected_rows'], correction['rounding_scale_rows'], correction['substantive_rows']) == (63, 56, 7)
for name, expected in status['code_hashes'].items():
    assert hashlib.sha256((run / 'code' / name).read_bytes()).hexdigest() == expected
source = repo / 'artifacts/backtest/yahoo-20261105-01/source'
collection = json.loads((source / 'collection.json').read_text(encoding='utf-8'))
for name, expected in collection['hashes'].items():
    assert hashlib.sha256((source / name).read_bytes()).hexdigest() == expected

sessions = calendar[(calendar >= status['settings']['start']) & (calendar <= status['settings']['end'])]
next_session = dict(zip(sessions[:-1], sessions[1:]))
session_number = {date: number for number, date in enumerate(sessions)}
checks = []
for strategy in summary.strategy_id.unique():
    expected_signals = set()
    for ticker, prices in inputs.items():
        flags = screening_signals(prices, strategy, inputs['1306.T'].Close)
        selected = flags.loc[flags.index.isin(sessions) & flags.ready & flags.buy_signal]
        expected_signals.update(f'{ticker}:{date.date()}' for date in selected.index)
    for policy in ('A', 'B'):
        name = f'{strategy}__{policy}'
        folder = run / name
        trades = pd.read_csv(folder / 'trades.csv', parse_dates=['signal_date', 'entry_date', 'exit_date'], float_precision='round_trip')
        positions = pd.read_csv(folder / 'positions.csv', parse_dates=['signal_date', 'entry_date'], float_precision='round_trip')
        rejected = pd.read_csv(folder / 'rejections.csv', parse_dates=['signal_date'])
        stats = summary.loc[(summary.strategy_id == strategy) & (summary.exit_policy == policy)].iloc[0]
        rejected_ids = [f'{row.ticker}:{row.signal_date.date()}' for row in rejected.itertuples()]
        actual_ids = trades.trade_id.tolist() + positions.trade_id.tolist() + rejected_ids
        assert len(actual_ids) == len(set(actual_ids))
        assert set(actual_ids) == expected_signals
        assert len(trades) == stats.trade_count and len(positions) == stats.open_positions
        assert set(rejected.reason).issubset({'outside_test_period', 'not_tradable'})
        for table in (trades, positions):
            if table.empty:
                continue
            assert table.entry_cost.eq(1).all()
            assert table.quantity.gt(0).all()
            assert table.entry_date.eq(table.signal_date.map(next_session)).all()
            np.testing.assert_allclose(table.quantity * table.entry_price * (1 + status['settings']['commission']),
                                       1.0, rtol=0, atol=1e-12)
        expected_return = trades.quantity * trades.exit_price * (1 - status['settings']['commission']) - 1
        np.testing.assert_allclose(trades.net_return, expected_return, rtol=0, atol=1e-12)
        elapsed = trades.exit_date.map(session_number) - trades.entry_date.map(session_number)
        assert trades.holding_sessions.eq(elapsed).all()
        assert trades.exit_date.ge(trades.entry_date).all()
        assert trades.exit_date.le(pd.Timestamp(status['settings']['end'])).all()
        if len(trades):
            np.testing.assert_allclose([stats.mean_net_return, stats.median_net_return, stats.win_rate],
                                       [trades.net_return.mean(), trades.net_return.median(),
                                        trades.net_return.gt(0).mean()], rtol=0, atol=1e-12)
        for group in ('ticker', 'entry_year', 'asset_class'):
            grouped = pd.read_csv(folder / f'by_{group}.csv')
            assert grouped.trade_count.sum() == len(trades)
        checks.append(dict(strategy_id=strategy, exit_policy=policy,
                           signal_count=len(expected_signals), closed_trades=len(trades),
                           open_positions=len(positions), rejected_orders=len(rejected)))
with zipfile.ZipFile(root / 'run.zip') as archive:
    assert archive.testzip() is None
    assert json.loads(archive.read('run.json'))['status'] == 'completed'
    assert archive.read('corrections.csv') == (root / 'data/corrections.csv').read_bytes()

result = dict(status='passed', validated_conditions=len(checks),
              source_files_hash_verified=len(collection['hashes']), corrected_rows=63,
              checks=['全買いシグナルの過不足と重複', '翌営業日寄付', '同額元本',
                      '費用控除後損益率', '保有日数', '銘柄・年・資産別集計',
                      '原本・入力・コードのハッシュ', '補正履歴', 'ZIP整合性'],
              conditions=checks)
(root / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(dict(status='passed', validated_conditions=len(checks),
                     verified_source_files=len(collection['hashes'])), indent=2))
