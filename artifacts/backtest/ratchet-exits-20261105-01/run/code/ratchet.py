"""R基準の段階更新と日足の価格出口を扱う。"""
from dataclasses import dataclass
import math


PROFIT_R = 3
EXIT_SETS = {'ab': ('A', 'B'), 'cd': ('C', 'D')}


@dataclass(frozen=True)
class PriceExit:
    base_price: float
    reason: str
    apply_slippage: bool = True
    ambiguous: bool = False


def initialize_ratchet(position, initial_stop):
    risk = position['entry_price'] * initial_stop
    position.update(initial_risk=risk, ratchet_step=0,
                    stop=position['entry_price'] - risk,
                    target=position['entry_price'] + PROFIT_R * risk)


def update_ratchet(position, close, policy):
    """終値の到達段階を求め、次の営業日用の注文水準へ更新する。"""
    price, risk = position['entry_price'], position['initial_risk']
    previous = position['ratchet_step']
    step = max(previous, math.floor((close - price) / risk))
    # 除算の丸めで段階を誤らないよう、価格の境界を直接比較する。
    if close < price + step * risk:
        step -= 1
    if close >= price + (step + 1) * risk:
        step += 1
    if step <= previous:
        return False
    position['ratchet_step'] = step
    position['stop'] = price - risk + step * risk
    if policy == 'D':
        position['target'] = price + (PROFIT_R + step) * risk
    return True


def price_exit(position, row, include_intraday=False):
    """寄付を先に判定し、日足内の両価格到達はストップ優先にする。"""
    stop, target = position['stop'], position.get('target')
    if row.Open <= stop:
        return PriceExit(float(row.Open), 'gap_stop')
    if target is not None and row.Open >= target:
        return PriceExit(float(target), 'take_profit', apply_slippage=False)
    if not include_intraday:
        return None
    stop_hit = row.Low <= stop
    target_hit = target is not None and row.High >= target
    if stop_hit:
        return PriceExit(float(stop), 'stop', ambiguous=bool(target_hit))
    if target_hit:
        return PriceExit(float(target), 'take_profit', apply_slippage=False)
    return None
