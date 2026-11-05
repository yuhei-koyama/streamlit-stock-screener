#!/usr/bin/env python3
"""Run the fixed suite with an explicitly selected strict or exploratory price model."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from stock_screener.backtest_io import run_suite, package_results

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--mode',choices=['strict','exploratory'],default='strict')
    parser.add_argument('--zip',action='store_true',help='Also save OUTPUT.zip for the Streamlit viewer')
    args=parser.parse_args()
    zip_path=Path(str(args.output)+'.zip')
    if args.zip and zip_path.exists():
        parser.error(f'{zip_path} already exists; choose a new output')
    summary=run_suite(args.data,args.output,mode=args.mode,progress=lambda text: print(text,flush=True))
    print(summary.to_string(index=False))
    if args.zip:
        print('Result ZIP:',package_results(args.output))
