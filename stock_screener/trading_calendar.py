"""保存済み公式祝日と東証の年末年始休業に基づく日足の営業日区分。"""
from hashlib import sha256
import json

import pandas as pd

LEGACY_POLICY = 'jpx_cash_sessions_2014_2025_v1'
POLICY = 'jpx_cash_sessions_2014_2025_v2'
START = '2014-01-01'
END = '2025-12-31'
SOURCE_URL = 'https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv'
SOURCE_SHA256 = 'cec37a743c96995cdb9cb52b685c9003634682a9b0e1a640a6b9b96881fe964a'
EXCHANGE_URL = 'https://www.jpx.co.jp/corporate/about-jpx/calendar/'
EXCEPTIONAL_CLOSURES = [dict(
    date='2020-10-01', reason='exchange_full_day_halt',
    source_url='https://www.jpx.co.jp/news/1030/20201001-04.html')]


# 内閣府原本から2014〜2025年の全祝日・振替休日・特例休日を抽出した。
HOLIDAY_DATES = """
    2014-01-01 2014-01-13 2014-02-11 2014-03-21 2014-04-29 2014-05-03
    2014-05-04 2014-05-05 2014-05-06 2014-07-21 2014-09-15 2014-09-23
    2014-10-13 2014-11-03 2014-11-23 2014-11-24 2014-12-23 2015-01-01
    2015-01-12 2015-02-11 2015-03-21 2015-04-29 2015-05-03 2015-05-04
    2015-05-05 2015-05-06 2015-07-20 2015-09-21 2015-09-22 2015-09-23
    2015-10-12 2015-11-03 2015-11-23 2015-12-23 2016-01-01 2016-01-11
    2016-02-11 2016-03-20 2016-03-21 2016-04-29 2016-05-03 2016-05-04
    2016-05-05 2016-07-18 2016-08-11 2016-09-19 2016-09-22 2016-10-10
    2016-11-03 2016-11-23 2016-12-23 2017-01-01 2017-01-02 2017-01-09
    2017-02-11 2017-03-20 2017-04-29 2017-05-03 2017-05-04 2017-05-05
    2017-07-17 2017-08-11 2017-09-18 2017-09-23 2017-10-09 2017-11-03
    2017-11-23 2017-12-23 2018-01-01 2018-01-08 2018-02-11 2018-02-12
    2018-03-21 2018-04-29 2018-04-30 2018-05-03 2018-05-04 2018-05-05
    2018-07-16 2018-08-11 2018-09-17 2018-09-23 2018-09-24 2018-10-08
    2018-11-03 2018-11-23 2018-12-23 2018-12-24 2019-01-01 2019-01-14
    2019-02-11 2019-03-21 2019-04-29 2019-04-30 2019-05-01 2019-05-02
    2019-05-03 2019-05-04 2019-05-05 2019-05-06 2019-07-15 2019-08-11
    2019-08-12 2019-09-16 2019-09-23 2019-10-14 2019-10-22 2019-11-03
    2019-11-04 2019-11-23 2020-01-01 2020-01-13 2020-02-11 2020-02-23
    2020-02-24 2020-03-20 2020-04-29 2020-05-03 2020-05-04 2020-05-05
    2020-05-06 2020-07-23 2020-07-24 2020-08-10 2020-09-21 2020-09-22
    2020-11-03 2020-11-23 2021-01-01 2021-01-11 2021-02-11 2021-02-23
    2021-03-20 2021-04-29 2021-05-03 2021-05-04 2021-05-05 2021-07-22
    2021-07-23 2021-08-08 2021-08-09 2021-09-20 2021-09-23 2021-11-03
    2021-11-23 2022-01-01 2022-01-10 2022-02-11 2022-02-23 2022-03-21
    2022-04-29 2022-05-03 2022-05-04 2022-05-05 2022-07-18 2022-08-11
    2022-09-19 2022-09-23 2022-10-10 2022-11-03 2022-11-23 2023-01-01
    2023-01-02 2023-01-09 2023-02-11 2023-02-23 2023-03-21 2023-04-29
    2023-05-03 2023-05-04 2023-05-05 2023-07-17 2023-08-11 2023-09-18
    2023-09-23 2023-10-09 2023-11-03 2023-11-23 2024-01-01 2024-01-08
    2024-02-11 2024-02-12 2024-02-23 2024-03-20 2024-04-29 2024-05-03
    2024-05-04 2024-05-05 2024-05-06 2024-07-15 2024-08-11 2024-08-12
    2024-09-16 2024-09-22 2024-09-23 2024-10-14 2024-11-03 2024-11-04
    2024-11-23 2025-01-01 2025-01-13 2025-02-11 2025-02-23 2025-02-24
    2025-03-20 2025-04-29 2025-05-03 2025-05-04 2025-05-05 2025-05-06
    2025-07-21 2025-08-11 2025-09-15 2025-09-23 2025-10-13 2025-11-03
    2025-11-23 2025-11-24
""".split()
_HOLIDAYS = pd.DatetimeIndex(HOLIDAY_DATES)


def calendar_metadata(policy=None):
    policy = POLICY if policy is None else policy
    if policy not in (LEGACY_POLICY, POLICY):
        raise ValueError('未対応の取引日カレンダー版です')
    metadata = dict(policy=policy, start=START, end=END,
                    holiday_source_url=SOURCE_URL, holiday_source_sha256=SOURCE_SHA256,
                    holiday_dates_sha256=sha256('\n'.join(HOLIDAY_DATES).encode()).hexdigest(),
                    exchange_reference_url=EXCHANGE_URL,
                    closures=['Saturday', 'Sunday', 'public_holiday', 'January_1_3', 'December_31'])
    if policy == POLICY:
        exceptions = [dict(item) for item in EXCEPTIONAL_CLOSURES]
        content = json.dumps(exceptions,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')
        metadata.update(exceptional_closures=exceptions, exceptional_closures_sha256=sha256(content).hexdigest())
    return metadata


def trading_day_mask(index, policy=None):
    metadata = calendar_metadata(policy)
    dates = pd.DatetimeIndex(index)
    if dates.tz is not None:
        dates = dates.tz_convert('Asia/Tokyo').tz_localize(None)
    dates = dates.normalize()
    if dates.hasnans or ((dates < START) | (dates > END)).any():
        raise ValueError('取引日カレンダーの対応範囲は2014〜2025年です')
    new_year = (dates.month == 1) & (dates.day <= 3)
    year_end = (dates.month == 12) & (dates.day == 31)
    exceptions = pd.to_datetime([item['date'] for item in metadata.get('exceptional_closures', [])])
    return (dates.dayofweek < 5) & ~dates.isin(_HOLIDAYS) & ~new_year & ~year_end & ~dates.isin(exceptions)


def split_trading_rows(frame, policy=None):
    """営業日は出来高に関係なく保持し、休場日の原票と理由を分離する。"""
    mask = trading_day_mask(frame.index, policy)
    excluded = frame.loc[~mask].copy()
    dates = pd.DatetimeIndex(excluded.index)
    if dates.tz is not None:
        dates = dates.tz_convert('Asia/Tokyo').tz_localize(None)
    dates = dates.normalize()
    reasons = pd.Series('exchange_year_end_or_new_year', index=excluded.index)
    reasons.loc[dates.dayofweek >= 5] = 'weekend'
    reasons.loc[dates.isin(_HOLIDAYS)] = 'public_holiday'
    for exception in calendar_metadata(policy).get('exceptional_closures', []):
        reasons.loc[dates == pd.Timestamp(exception['date'])] = exception['reason']
    excluded['exclusion_reason'] = reasons
    return frame.loc[mask].copy(), excluded


def validate_execution_calendar(execution, signals, calendar, reference=None, policy=None):
    """新規計算では形式の新旧に関係なく全入力の休場日混入を拒否する。"""
    indices = [('営業日カレンダー', calendar)]
    indices.extend((f'約定入力 {ticker}', frame.index) for ticker, frame in execution.items())
    indices.extend((f'指標入力 {ticker}', frame.index) for ticker, frame in signals.items())
    if reference is not None:
        indices.append(('RS参照', reference.index))
    for label, index in indices:
        if not trading_day_mask(index, policy).all():
            raise ValueError(f'{label}に休場日が含まれています。原本を保持し、営業日だけの入力へ再準備してください')
