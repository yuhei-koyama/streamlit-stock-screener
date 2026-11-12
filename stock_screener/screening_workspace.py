"""日足検索の判定・流動性・購入単位・出口水準と保存を扱う。"""
from dataclasses import dataclass, field
import json
import hashlib
import io
import math
import os
from pathlib import Path
import time
import uuid

import numpy as np
import pandas as pd

from .backtest_signals import MINERVINI, RSI2, RSI14, STRATEGIES, screening_signals
from .linda_versions import TV, LEGACY, ANTI_STOCH, VERSIONS, TV_DEFAULTS, evaluate_version
from .ratchet import initialize_ratchet, update_ratchet, price_exit
from .finite_capital import PriceRow, Decision
from .backtest import _exit_at_close
from .search import search_rsi, search_minervini

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / 'data' / 'screener'
NAMES = {key: value[0] for key, value in VERSIONS.items()} | {
    MINERVINI: 'ミネルヴィニ派生版', RSI2: 'RSI2', RSI14: 'RSI14'}
STATE_LABELS = {True: '成立', False: '不成立', None: '判定不可'}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, default=str), encoding='utf-8')
    temporary.replace(path)


def sessions(start, end):
    calendar_path = ROOT/'outputs'/'screener'/'calendar.json'
    calendar = json.loads((calendar_path if calendar_path.exists() else REFERENCE/'calendar.json').read_text(encoding='utf-8'))
    start, end = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    if start < pd.Timestamp(calendar['start']) or end > pd.Timestamp(calendar['end']):
        raise ValueError('営業日規則の対応期間外です。カレンダーの更新が必要です。')
    days = pd.date_range(start, end)
    closed = days.isin(pd.to_datetime(calendar['holidays'])) | ((days.month == 1) & (days.day <= 3)) | ((days.month == 12) & (days.day == 31))
    return days[(days.dayofweek < 5) & ~closed]


def refresh_calendar():
    from urllib.request import urlopen
    source = 'https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv'
    content = urlopen(source,timeout=30).read()
    table = pd.read_csv(io.BytesIO(content),encoding='cp932')
    if '国民の祝日・休日月日' not in table:
        raise ValueError('公式祝日の列を確認できません')
    dates = pd.to_datetime(table['国民の祝日・休日月日'],errors='raise')
    if dates.dt.year.max() < pd.Timestamp.now(tz='Asia/Tokyo').year:
        raise ValueError('今年の公式祝日が不足しています')
    original = json.loads((REFERENCE/'calendar.json').read_text(encoding='utf-8'))
    holidays = sorted(set(original['holidays'])|set(dates.dt.strftime('%Y-%m-%d')))
    data = dict(start='2014-01-01',end=f'{dates.dt.year.max()}-12-31',holidays=holidays,
                sources=[source,*original['sources']],retrieved_at=pd.Timestamp.now(tz='UTC').isoformat(),
                source_sha256=hashlib.sha256(content).hexdigest())
    write_json(ROOT/'outputs'/'screener'/'calendar.json',data)
    return data


def decision_date(requested, use_today=False, now=None):
    today = pd.Timestamp(now or pd.Timestamp.now(tz='Asia/Tokyo')).date()
    requested = min(pd.Timestamp(requested).date(), today)
    if requested == today and not use_today:
        requested = (pd.Timestamp(today) - pd.Timedelta(1, unit='D')).date()
    available = sessions(pd.Timestamp(requested) - pd.Timedelta(15, unit='D'), requested)
    return available[-1]


@dataclass
class SearchSettings:
    strategies: list = field(default_factory=lambda: [TV])
    combination: str = 'OR'
    liquidity_enabled: bool = True
    minimum_turnover: float = 1_000_000_000
    maximum_candidates: int = 100
    budget: float | None = None
    commission: float = 0.0
    slippage: float = .0005
    exit_policy: str = 'A'
    initial_stop: float = .10
    parameters: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.liquidity_enabled,bool):
            raise ValueError('流動性条件のON／OFFが不正です')
        if not self.strategies or len(set(self.strategies)) != len(self.strategies) or any(s not in STRATEGIES for s in self.strategies):
            raise ValueError('入口を1つ以上選択してください')
        if self.combination not in ('OR', 'AND') or self.exit_policy not in ('A','B','C','D'):
            raise ValueError('組合せ方法または出口が不正です')
        if (not math.isfinite(self.minimum_turnover) or self.minimum_turnover < 0
                or not isinstance(self.maximum_candidates,int) or isinstance(self.maximum_candidates,bool)
                or not 1 <= self.maximum_candidates <= 10000):
            raise ValueError('流動性条件または件数が不正です')
        if self.budget is not None and (not math.isfinite(self.budget) or self.budget <= 0):
            raise ValueError('予算は正の金額を指定してください')
        if any(not math.isfinite(v) or not 0 <= v < 1 for v in (self.commission, self.slippage)):
            raise ValueError('費用は0%以上100%未満で指定してください')
        if not math.isfinite(self.initial_stop) or not 0 < self.initial_stop < 1:
            raise ValueError('損切り率が不正です')


def combine_states(states, mode):
    if mode == 'OR':
        return True if True in states else False if all(s is False for s in states) else None
    if mode == 'AND':
        return False if False in states else True if all(s is True for s in states) else None
    raise ValueError('組合せ方法が不正です')


def required_history_years(settings):
    rows = 252
    for strategy in settings.strategies:
        values = settings.parameters.get(strategy,{})
        if strategy==TV:
            config = TV_DEFAULTS | values
            rows = max(rows,config['k_len']+config['smooth_k']+config['d_len']+2,
                       config['fast_len']+config['slow_len']+config['signal_len'])
        elif strategy==LEGACY:
            config = dict(fast_len=3,slow_len=10,signal_sma=16,price_sma=16,min_rows=30) | values
            rows = max(rows,config['min_rows'],config['price_sma'],config['fast_len']+config['slow_len']+config['signal_sma'])
        elif strategy==ANTI_STOCH:
            config = dict(k_len=7,fast_len=4,slow_len=10) | values
            rows = max(rows,sum(config.values())+3)
    return max(3,math.ceil(rows/220)+1)


def purchase_estimate(price, lot, budget, commission=0, slippage=.0005):
    if price is None or lot is None or not np.isfinite(price) or price <= 0 or not np.isfinite(lot) or lot < 1 or int(lot) != lot:
        return dict(lot_cost=None, quantity=None, cost=None)
    unit_cost = float(price * (1 + slippage) * (1 + commission) * lot)
    quantity = int(lot) if budget is None else math.floor(budget / unit_cost) * int(lot)
    return dict(lot_cost=unit_cost, quantity=quantity, cost=unit_cost * quantity / lot)


def valid_prices(frame):
    values = frame[['Open', 'High', 'Low', 'Close', 'Volume']]
    valid = np.isfinite(values).all(axis=1) & values.iloc[:, :4].gt(0).all(axis=1) & values.Volume.ge(0)
    return valid & values.High.ge(values[['Open', 'Close', 'Low']].max(axis=1)) & values.Low.le(values[['Open', 'Close', 'High']].min(axis=1))


def price_digest(frame, asof):
    subset = frame.loc[:asof,['Open','High','Low','Close','Adj Close','Volume']].copy()
    subset.index.name = 'Date'
    return hashlib.sha256(subset.to_csv(float_format='%.17g').encode('utf-8')).hexdigest()


def screening_code_hash():
    names = ['screening_workspace.py','backtest_signals.py','linda_versions.py','search.py',
             'anti.py','indicators.py','ratchet.py','backtest.py','finite_capital.py','validation.py']
    hashes = {name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest() for name in names}
    return hashlib.sha256(json.dumps(hashes,sort_keys=True).encode('utf-8')).hexdigest()


def indicator_prices(raw, asof):
    if raw.empty:
        return raw.copy()
    frame = raw.loc[:asof].copy()
    if frame.empty:
        return frame
    grid = sessions(max(frame.index[0],pd.Timestamp('2014-01-01')), asof)
    frame = frame.reindex(grid)
    valid = valid_prices(frame)
    factor = frame['Adj Close'] / frame.Close if 'Adj Close' in frame else pd.Series(1., index=frame.index)
    frame.loc[:, ['Open', 'High', 'Low', 'Close']] = frame[['Open', 'High', 'Low', 'Close']].mul(factor, axis=0)
    frame.loc[~valid | ~np.isfinite(factor) | factor.le(0), ['Open', 'High', 'Low', 'Close', 'Volume']] = np.nan
    return frame


def evaluate_entry(frame, strategy, parameters, benchmark=None):
    if not parameters:
        return screening_signals(frame, strategy, benchmark)
    if strategy in VERSIONS:
        return evaluate_version(frame, strategy, **parameters)
    if strategy in (RSI2, RSI14):
        out = search_rsi(frame, period=2 if strategy == RSI2 else 14, **parameters)
        out['buy_signal'], out['sell_signal'] = out['match'], False
        out['strategy_id'] = strategy
        return out
    if strategy == MINERVINI:
        if benchmark is None:
            raise ValueError('RS参照1306.Tが必要です')
        out = search_minervini(frame,benchmark,**parameters)
        out['buy_signal'],out['sell_signal'],out['strategy_id'] = out['match'],False,strategy
        return out
    raise ValueError('この入口の数値は共有計算式の検証値を使用します')


def scan_catalog(catalog, prices, asof, settings, progress=None):
    """流動性と予算を先に評価し、上位候補に同日付の共有式を適用する。"""
    if catalog.empty:
        return pd.DataFrame(columns=['ticker','name','state','reason','turnover']),{}
    asof = pd.Timestamp(asof)
    reference = indicator_prices(prices.get('1306.T', pd.DataFrame()), asof)
    reference_close = reference.Close if not reference.empty else None
    records, details = [], {}
    total = max(1,len(catalog))
    for number,asset in enumerate(catalog.itertuples(),start=1):
        if progress and (number%25==0 or number==len(catalog)):
            progress(.4*number/total,f'流動性・予算　{number:,}／{len(catalog):,}')
        raw = prices.get(asset.ticker, pd.DataFrame())
        market_start = getattr(asset, 'market_start_date', None)
        if pd.notna(market_start) and market_start:
            raw = raw.loc[pd.Timestamp(market_start):]
        record = dict(ticker=asset.ticker, name=asset.name, asset_class=asset.asset_class,
                      lot_size=None, close=None, turnover=None, atr_ratio=None, state='判定不可',
                      reason='', quantity=None, cost=None, lot_cost=None, date=str(asof.date()))
        record['market_start_date'] = str(market_start) if pd.notna(market_start) and market_start else None
        lot = getattr(asset, 'lot_size', np.nan)
        known_from = getattr(asset,'lot_known_from', None)
        if pd.notna(known_from) and known_from and asof < pd.Timestamp(known_from):
            lot = np.nan
        record['lot_size'] = float(lot) if pd.notna(lot) else None
        record['lot_known_from'] = str(known_from) if pd.notna(known_from) else None
        source = getattr(asset,'lot_source',None)
        record['lot_source'] = source if pd.notna(source) else None
        if raw.empty or asof not in raw.index or not bool(valid_prices(raw.loc[[asof]]).iloc[0]):
            record['reason'] = '判定日の有効価格がありません'
        elif raw.loc[asof,'Volume'] <= 0:
            record['reason'] = '判定日の出来高が0のため取引状態を確認待ち'
        else:
            frame = indicator_prices(raw, asof)
            raw_grid = raw.reindex(frame.index)
            turnover = (raw_grid.Close * raw_grid.Volume).where(valid_prices(raw_grid)).rolling(20).mean().iloc[-1]
            close = float(raw.loc[asof, 'Close'])
            estimate = purchase_estimate(close, lot, settings.budget, settings.commission, settings.slippage)
            record.update(close=close, turnover=float(turnover) if pd.notna(turnover) else None, **estimate)
            previous = frame.Close.shift(1)
            tr = pd.concat([frame.High-frame.Low, (frame.High-previous).abs(), (frame.Low-previous).abs()], axis=1).max(axis=1)
            # Wilder方式の初期値は14本の算術平均とする。
            atr, previous_atr, count = [], None, 0
            for value in tr.where(frame.Close.notna()):
                if pd.isna(value):
                    atr, previous_atr, count = [], None, 0
                elif count < 14:
                    atr.append(value)
                    count += 1
                    if count == 14:
                        previous_atr = float(np.mean(atr))
                else:
                    previous_atr = (previous_atr * 13 + value) / 14
            record['atr_ratio'] = previous_atr / frame.Close.iloc[-1] if previous_atr is not None else None
            if settings.liquidity_enabled and pd.isna(turnover):
                record['reason'] = '平均売買代金の20営業日分が不足'
            elif settings.liquidity_enabled and turnover < settings.minimum_turnover:
                record.update(state='対象外', reason='平均売買代金の下限未満')
            elif settings.budget is not None and estimate['quantity'] is None:
                record['reason'] = '売買単位を確認待ち'
            elif settings.budget is not None and estimate['quantity'] == 0:
                record.update(state='対象外', reason='予算で1売買単位を購入できません')
            else:
                record['reason'] = '入口判定待ち'
                details[asset.ticker] = {'prices': frame, 'raw': raw.loc[:asof], 'signals': {}}
        records.append(record)
    ordered = sorted([r for r in records if r['reason'] == '入口判定待ち'], key=lambda r: (-(r['turnover'] if r['turnover'] is not None else -np.inf), r['ticker']))
    selected = ordered[:settings.maximum_candidates] if settings.liquidity_enabled else ordered
    candidates = {r['ticker'] for r in selected}
    for number,record in enumerate(records,start=1):
        if progress and (number%10==0 or number==len(records)):
            progress(.4+.6*number/total,f'入口条件　{number:,}／{len(records):,}')
        ticker = record['ticker']
        if record['reason'] != '入口判定待ち':
            continue
        if ticker not in candidates:
            record.update(state='対象外', reason='売買代金順の件数上限外')
            details.pop(ticker,None)
            continue
        states, reasons = [], []
        for strategy in settings.strategies:
            try:
                if strategy == MINERVINI and (reference_close is None or asof not in reference_close.index or pd.isna(reference_close.loc[asof])):
                    raise ValueError('RS参照1306.Tの同日価格が不足')
                result = evaluate_entry(details[ticker]['prices'], strategy, settings.parameters.get(strategy, {}), reference_close)
                latest = result.loc[asof]
                state = bool(latest.buy_signal) if latest.ready else None
                reason = '条件成立' if state is True else '条件不成立' if state is False else '指標の準備本数が不足'
                details[ticker]['signals'][strategy] = result
            except (ValueError, KeyError, TypeError) as exc:
                state, reason = None, str(exc)
            states.append(state)
            record[strategy] = STATE_LABELS[state]
            reasons.append(f'{NAMES[strategy]}　{reason}')
        record.update(state=STATE_LABELS[combine_states(states, settings.combination)], reason=' ／ '.join(reasons))
    if progress:
        progress(1.,'判定完了')
    return pd.DataFrame(records).sort_values(['turnover', 'ticker'], ascending=[False, True], na_position='last'), details


def exit_projection(entry_price, close, high, policy, stop, quantity, commission=0, slippage=.0005,
                    ohlc=None, signals=None, strategy_id=None):
    position = dict(entry_price=entry_price, stop=entry_price*(1-stop), peak=entry_price,entry_session=0)
    if policy in ('C', 'D'):
        initialize_ratchet(position, stop, .08)
    records = []
    pending = None
    actual_proceeds = None
    for session,(timestamp, value) in enumerate(close.items()):
        if pd.isna(value) or (ohlc is not None and not bool(valid_prices(ohlc.loc[[timestamp]]).iloc[0])):
            position['path_missing'] = True
            records.append(dict(date=timestamp, stop=np.nan, target=np.nan, state='価格不足'))
            continue
        if ohlc is not None:
            row = ohlc.loc[timestamp]
            if row.Volume <= 0:
                position['path_missing'] = True
                records.append(dict(date=timestamp,stop=np.nan,target=np.nan,state='出来高0・経路確認待ち'))
                continue
            execution = PriceRow(row.Open,row.High,row.Low,row.Close,row.Volume,True,True)
            event = price_exit(position,execution)
            if event is None and pending:
                fill, reason = float(row.Open), pending
                apply_slippage = True
            else:
                event = event or price_exit(position,execution,include_intraday=True)
                fill = event.base_price if event else None
                reason = event.reason if event else None
                apply_slippage = event.apply_slippage if event else True
            if fill is not None:
                actual_price = fill*(1-slippage) if apply_slippage else fill
                actual_proceeds = actual_price*quantity*(1-commission)
                position.update(closed=True,exit_date=str(timestamp.date()),exit_reason=reason,exit_price=actual_price)
                records.append(dict(date=timestamp,stop=position['stop'],target=position.get('target'),state='参考決済　'+reason))
                break
            if policy=='B' and strategy_id:
                signal = signals.loc[timestamp] if signals is not None and timestamp in signals.index else None
                decision = Decision(bool(signal.ready),bool(signal.sell_signal),signal.get('rsi',np.nan)) if signal is not None else None
                pending = _exit_at_close(position,execution,decision,session,strategy_id,policy,time_exit_enabled=False)
        if policy in ('C', 'D'):
            update_ratchet(position, float(value), policy)
        elif policy == 'B' and high is not None and ohlc is None:
            position['peak'] = max(position['peak'], float(high.loc[timestamp]))
            position['stop'] = max(position['stop'], position['peak']*.9)
        records.append(dict(date=timestamp, stop=position['stop'], target=position.get('target'), state='翌営業日用の参考水準'))
    cost = entry_price * quantity * (1+commission)
    proceeds = position['stop'] * (1-slippage) * quantity * (1-commission)
    return position, (actual_proceeds if actual_proceeds is not None else proceeds)-cost, pd.DataFrame(records)


def acquire_price_lock(path):
    handle = path.open('a+b')
    if path.stat().st_size == 0:
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ValueError('別の検索がこの銘柄を取得中です。完了後に再実行してください。')
    return handle


def release_price_lock(handle):
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(),fcntl.LOCK_UN)
    handle.close()


class DailyStore:
    def __init__(self, root=None):
        self.root = Path(root or ROOT / 'outputs' / 'screener')
        self.root.mkdir(parents=True, exist_ok=True)

    def read(self, ticker):
        path = self.root / 'prices' / f'{ticker}.csv'
        return pd.read_csv(path, index_col=0, parse_dates=True, float_precision='round_trip') if path.exists() else pd.DataFrame()

    def refresh(self, ticker, asof, force=False, fetcher=None, lookback_years=3):
        import yfinance as yf
        if not isinstance(lookback_years,int) or isinstance(lookback_years,bool) or lookback_years<1:
            raise ValueError('取得年数は正の整数で指定してください')
        if not __import__('re').fullmatch(r'[0-9][0-9A-Z]{3}\.T', ticker):
            raise ValueError('東証コードが不正です')
        path = self.root / 'prices' / f'{ticker}.csv'
        metadata_path = path.with_suffix('.json')
        lock = path.with_suffix('.lock')
        path.parent.mkdir(parents=True, exist_ok=True)
        guard = acquire_price_lock(lock)
        try:
            old = self.read(ticker)
            metadata = json.loads(metadata_path.read_text(encoding='utf-8')) if metadata_path.exists() else {}
            required_start = max(pd.Timestamp(asof) - pd.DateOffset(years=lookback_years),pd.Timestamp('2014-01-01'))
            end = pd.Timestamp.now(tz='Asia/Tokyo').tz_localize(None).normalize() + pd.Timedelta(1,unit='D')
            if (not force and not old.empty and metadata.get('start', '9999') <= str(required_start.date())
                    and metadata.get('end', '') >= str((pd.Timestamp(asof)+pd.Timedelta(1,unit='D')).date())
                    and metadata.get('price_sha256') == hashlib.sha256(path.read_bytes()).hexdigest()
                    and time.time()-metadata.get('retrieved_epoch', 0) < 900):
                return old, False
            fetch = fetcher or yf.Ticker(ticker).history
            start = required_start if old.empty or old.index[0] > required_start else old.index[-1]-pd.Timedelta(10,unit='D')
            kwargs = dict(end=str(end.date()), interval='1d', auto_adjust=False, actions=True, repair=False, raise_errors=True)
            incoming = fetch(start=str(start.date()), **kwargs).copy()
            if incoming.empty:
                raise ValueError('取得した日足が空です')
            incoming.index = incoming.index.tz_convert('Asia/Tokyo').tz_localize(None).normalize() if incoming.index.tz is not None else incoming.index.normalize()
            common = old.index.intersection(incoming.index)
            if len(common) and not np.allclose(old.loc[common, ['Close','Adj Close']], incoming.loc[common, ['Close','Adj Close']], equal_nan=True, rtol=1e-9, atol=1e-9):
                incoming = fetch(start=str(min(old.index[0], required_start).date()), **kwargs).copy()
                if incoming.empty:
                    raise ValueError('価格単位変更の再取得データが空です')
                incoming.index = incoming.index.tz_convert('Asia/Tokyo').tz_localize(None).normalize() if incoming.index.tz is not None else incoming.index.normalize()
                old = pd.DataFrame()
            if not incoming.index.is_unique:
                raise ValueError('取得した日足の日付が重複しています')
            merged = pd.concat([old, incoming]).loc[lambda f: ~f.index.duplicated(keep='last')].sort_index()
            if not {'Open','High','Low','Close','Adj Close','Volume'}.issubset(merged.columns):
                raise ValueError('必要な日足列が不足しています')
            temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
            merged.to_csv(temporary, float_format='%.17g')
            temporary.replace(path)
            write_json(metadata_path, dict(start=str(min(required_start, merged.index[0]).date()), end=str(end.date()),
                                          retrieved_at=pd.Timestamp.now(tz='UTC').isoformat(), retrieved_epoch=time.time(),
                                          price_sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
            return merged, True
        finally:
            release_price_lock(guard)
