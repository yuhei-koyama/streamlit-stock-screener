#!/usr/bin/env python3
"""東証の公式一覧を固定し、ローカルで分割取得・入口/出口を検証する。"""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from stock_screener.market_research import freeze,collect,run_batch,aggregate

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('freeze');p.add_argument('--jpx-file',required=True);p.add_argument('--etf-file',required=True)
    p.add_argument('--output',required=True);p.add_argument('--source-date');p.add_argument('--exclusions-file')
    p=sub.add_parser('collect');p.add_argument('--root',required=True);p.add_argument('--limit',type=int,default=100);p.add_argument('--pause',type=float,default=1.)
    p.add_argument('--continue-on-data-error',action='store_true',help='銘柄固有のHTTP400・入力不正を記録して他銘柄の取得を継続。レート制限や通信障害は停止')
    p=sub.add_parser('run');p.add_argument('--root',required=True);p.add_argument('--limit',type=int,default=25);p.add_argument('--phase',choices=['entry','exits'],default='entry')
    p=sub.add_parser('summary');p.add_argument('--root',required=True);p.add_argument('--phase',choices=['entry','exits'],default='entry')
    a=parser.parse_args()
    if a.command=='freeze':r=freeze(a.jpx_file,a.etf_file,a.output,a.source_date,a.exclusions_file)
    elif a.command=='collect':r=collect(a.root,a.limit,a.pause,a.continue_on_data_error)
    elif a.command=='run':r=dict(processed=run_batch(a.root,a.limit,a.phase),phase=a.phase)
    else:r=aggregate(a.root,a.phase)
    print(json.dumps(r,ensure_ascii=False,indent=2))
