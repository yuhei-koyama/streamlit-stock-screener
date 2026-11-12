#!/usr/bin/env python3
"""検索UIの既定値が固定した検証コードと一致することを照合する。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from stock_screener.backtest_signals import STRATEGIES,screening_signals


def verify(baseline,output):
    baseline,output=Path(baseline).resolve(),Path(output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    days=pd.bdate_range('2021-01-04',periods=750)
    index=np.arange(len(days))
    close=100+index*.1+5*np.sin(index/6)
    frame=pd.DataFrame(dict(Open=close-.2,High=close+1,Low=close-1,Close=close,Volume=1_000_000+100_000*np.cos(index/4)),index=days)
    frame.iloc[160]=np.nan
    frame.to_csv(output/'prices.csv',index_label='Date',float_format='%.17g')
    reference=pd.Series(100+index*.05,index=days,name='Close')
    reference.to_csv(output/'reference.csv',index_label='Date',float_format='%.17g')
    worker=output/'baseline_worker.py'
    worker.write_text('''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import pandas as pd
from stock_screener.backtest_signals import STRATEGIES,screening_signals
root=Path(__file__).resolve().parent
frame=pd.read_csv(root/'prices.csv',index_col='Date',parse_dates=True,float_precision='round_trip')
reference=pd.read_csv(root/'reference.csv',index_col='Date',parse_dates=True,float_precision='round_trip').Close
for strategy in STRATEGIES:
    screening_signals(frame,strategy,reference).to_csv(root/(strategy+'-baseline.csv'),float_format='%.17g')
''',encoding='utf-8')
    subprocess.run([sys.executable,str(worker),str(baseline)],check=True,timeout=60,
                   env={**os.environ,'PYTHONIOENCODING':'utf-8'})
    frame=pd.read_csv(output/'prices.csv',index_col='Date',parse_dates=True,float_precision='round_trip')
    reference=pd.read_csv(output/'reference.csv',index_col='Date',parse_dates=True,float_precision='round_trip').Close
    hashes={}
    for strategy in STRATEGIES:
        target=output/(strategy+'-current.csv')
        screening_signals(frame,strategy,reference).to_csv(target,float_format='%.17g')
        original=output/(strategy+'-baseline.csv')
        if target.read_bytes()!=original.read_bytes():
            raise AssertionError('検証時の既定値と一致しません。'+strategy)
        hashes[strategy]=hashlib.sha256(target.read_bytes()).hexdigest()
    (output/'verification.json').write_text(json.dumps(dict(status='passed',strategies=7,
        comparison='全列のCSVバイト列一致',fixture_sha256=hashlib.sha256((output/'prices.csv').read_bytes()).hexdigest(),
        baseline_code_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (baseline/'stock_screener').glob('*.py')},
        current_code_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'stock_screener').glob('*.py')},
        result_hashes=hashes),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('7入口の既定値は固定した検証コードと全列一致しました')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-code',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    verify(args.baseline_code,args.output)
