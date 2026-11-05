#!/usr/bin/env python3
"""Run only the approved fixed suite from a verified, local dataset."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from stock_screener.backtest_io import run_suite

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    summary=run_suite(args.data,args.output)
    print(summary.to_string(index=False))
