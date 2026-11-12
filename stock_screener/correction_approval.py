"""ユーザー承認済みの固定レコードから補正対象と原本を照合する。"""
from hashlib import sha256
import io
import json
from pathlib import Path

import pandas as pd

APPROVAL_FILE = Path(__file__).resolve().parents[1] / 'artifacts/backtest/period-2016-2020-20261106-01/rounding-approval-20261107-01/approval.json'
APPROVAL_SHA256 = '4b4e0be907ff4498b61e83f6d792b51eb1512a8b5c168ab1c5c9bc8239ff5a90'


def load_rounding_approval(source, collection_content):
    record_content = APPROVAL_FILE.read_bytes()
    if sha256(record_content).hexdigest() != APPROVAL_SHA256:
        raise ValueError('承認レコードのハッシュが一致しません')
    record = json.loads(record_content)
    if record.get('schema_version') != 1:
        raise ValueError('承認レコードの形式が未対応です')
    if sha256(collection_content).hexdigest() != record['source_collection_sha256']:
        raise ValueError('承認対象の元データコレクションと一致しません')
    list_path = APPROVAL_FILE.parent / record['approved_list']
    if list_path.parent != APPROVAL_FILE.parent:
        raise ValueError('承認一覧の保存先が不正です')
    list_content = list_path.read_bytes()
    if sha256(list_content).hexdigest() != record['approved_list_sha256']:
        raise ValueError('承認一覧のハッシュが一致しません')
    rows = pd.read_csv(io.BytesIO(list_content),parse_dates=['Date'],float_precision='round_trip')
    required = {'ticker','Date','Open','Close','original_high','proposed_high','original_low','proposed_low','difference'}
    if not required.issubset(rows) or rows.empty or len(rows) != record['approved_rows']:
        raise ValueError('承認一覧の列または件数が不正です')
    if rows[['ticker','Date']].isna().any().any() or rows.duplicated(['ticker','Date']).any():
        raise ValueError('承認一覧の銘柄・日付が欠損または重複しています')
    collection = json.loads(collection_content)
    for name, expected in collection['hashes'].items():
        path = Path(source)/name
        if path.parent != Path(source) or not path.is_file() or sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'承認対象の元データのハッシュが一致しません: {name}')
    return record, rows, record_content, list_content
