#!/usr/bin/env python3
"""保存した独立仮想取引へ、オルカンの同額・同期間比較を付加する。"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock_screener.benchmark_comparison import run_comparison
from stock_screener.backtest_io import package_results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', required=True)
    parser.add_argument('--fund-csv', required=True)
    parser.add_argument('--fund-metadata', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--include-open',action='store_true',help='Include end-of-period marked open positions')
    parser.add_argument('--zip', action='store_true')
    args = parser.parse_args()
    if args.zip and Path(args.output + '.zip').exists():
        parser.error('Result ZIP already exists')
    summary = run_comparison(args.source_run, args.fund_csv, args.fund_metadata, args.output,
                             include_open_positions=args.include_open)
    print(summary.to_string(index=False))
    if args.zip:
        print('Result ZIP:', package_results(args.output))
