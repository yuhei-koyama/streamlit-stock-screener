#!/usr/bin/env python3
"""Prepare adjusted-price/virtual-share inputs offline from a completed source cache."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from stock_screener.exploratory_data import prepare_exploratory

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    manifest=prepare_exploratory(args.source,args.output)
    print(f"Prepared {len(manifest['assets'])} assets: adjusted OHLC, virtual one-share units, no dividend cash additions.")
