#!/usr/bin/env python3
"""承認対象の一覧と入力ハッシュを照合し、探索入力の価格範囲を補正する。"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock_screener.data_quality import correct_exploratory_data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--approved-issues', required=True)
    parser.add_argument('--reference-run', required=True)
    args = parser.parse_args()
    result = correct_exploratory_data(args.data, args.output, args.approved_issues, args.reference_run)
    print(f"Corrected {result['corrected_rows']} rows: "
          f"{result['rounding_scale_rows']} rounding-scale, {result['substantive_rows']} substantive.")
