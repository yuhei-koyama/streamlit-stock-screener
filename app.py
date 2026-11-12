"""日足の銘柄検索・銘柄詳細・保存済み検証結果を表示する入口。

旧画面から使われるデータ取得・指標・チャートの公開関数も保持する。
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Callable

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
import yfinance as yf

from stock_screener.anti import evaluate_anti_310, evaluate_anti_stochastic



from stock_screener.universe import MAX_TICKERS, PRESET_TICKERS


STRATEGY_LABELS = {
    "lbr_anti_310_adaptation_v1": "3/10適応版（SMA 3/10/16）",
    "lbr_anti_stochastic_v1": "ストキャスティクス版（7/4/10）",
}

RESULT_COLUMNS = (
    "strategy_id", "definition_version", "phase", "direction", "reason",
    "fast", "slow", "histogram", "fast_slope", "slow_slope",
    "distance_to_slow", "pullback_count", "crossed_slow",
    "setup_long", "setup_short", "hook_long", "hook_short",
)


def normalize_tickers(raw: str) -> tuple[list[str], list[str]]:
    """東証ティッカーを正規化し、入力順を保ったまま重複を除く。"""
    tickers: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[,\s、]+", raw):
        value = token.strip().upper()
        if not value:
            continue
        if re.fullmatch(r"[0-9][0-9A-Z]{3}", value):
            value = f"{value}.T"
        if not re.fullmatch(r"[0-9][0-9A-Z]{3}\.T", value):
            invalid.append(token.strip())
            continue
        if value not in seen:
            seen.add(value)
            tickers.append(value)
    return tickers, invalid


@st.cache_data(ttl=900, show_spinner=False)
def fetch_daily_prices(ticker: str, period: str) -> pd.DataFrame:
    """検証条件を明示してYahoo Financeの日足を取得する。"""
    return yf.Ticker(ticker).history(
        period=period,
        interval="1d",
        auto_adjust=True,
        actions=False,
    )


def exclude_current_day_bar(data: pd.DataFrame, enabled: bool) -> pd.DataFrame:
    """当日の日足を保守的に除き、未確定足を判定へ渡さない。"""
    if not enabled or data.empty:
        return data.copy()
    latest = pd.Timestamp(data.index[-1])
    if latest.tzinfo is not None:
        latest = latest.tz_convert("Asia/Tokyo").tz_localize(None)
    today = pd.Timestamp.now(tz="Asia/Tokyo").tz_localize(None).normalize()
    if latest.normalize() >= today:
        return data.iloc[:-1].copy()
    return data.copy()


def compact_text(value: Any) -> str:
    if value is None or value is pd.NA:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    if isinstance(value, dict):
        return ", ".join(f"{key}={item}" for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value)
    return str(value)


def timestamp_text(value: Any, fallback: Any) -> str:
    candidate = fallback if value is None or value is pd.NA else value
    if isinstance(candidate, float) and pd.isna(candidate):
        candidate = fallback
    try:
        return pd.Timestamp(candidate).isoformat()
    except (TypeError, ValueError):
        return compact_text(candidate)


def validate_result(result: pd.DataFrame) -> None:
    missing = [column for column in RESULT_COLUMNS if column not in result.columns]
    if missing:
        raise ValueError(f"判定結果に必須列がありません: {', '.join(missing)}")
    if result.empty:
        raise ValueError("判定結果が空です")


def latest_summary(ticker: str, result: pd.DataFrame) -> dict[str, Any]:
    validate_result(result)
    latest = result.iloc[-1]
    fallback_timestamp = result.index[-1]
    return {
        "Ticker": ticker,
        "Strategy": STRATEGY_LABELS.get(
            compact_text(latest.get("strategy_id")),
            compact_text(latest.get("strategy_id")),
        ),
        "Definition": compact_text(latest.get("definition_version")),
        "判定対象": timestamp_text(latest.get("data_cutoff_timestamp"), fallback_timestamp),
        "Phase": compact_text(latest.get("phase")),
        "Direction": compact_text(latest.get("direction")),
        "Setup": bool(latest.get("setup_long", False) or latest.get("setup_short", False)),
        "Hook": bool(latest.get("hook_long", False) or latest.get("hook_short", False)),
        "Pullback bars": int(latest.get("pullback_count", 0) or 0),
        "Crossed Slow": bool(latest.get("crossed_slow", False)),
        "Fast": latest.get("fast"),
        "Slow": latest.get("slow"),
        "Histogram": latest.get("histogram"),
        "Reason": compact_text(latest.get("reason")),
        "Parameters": compact_text(latest.get("parameter_set")),
    }


def calculate_strategies(
    data: pd.DataFrame,
    selected_strategy: str,
    pullback_bars: int,
    allow_cross: bool,
) -> list[pd.DataFrame]:
    calls: list[Callable[[], pd.DataFrame]] = []
    if selected_strategy in {"両方", "3/10適応版"}:
        calls.append(
            lambda: evaluate_anti_310(
                data,
                pullback_bars=pullback_bars,
                allow_cross=allow_cross,
            )
        )
    if selected_strategy in {"両方", "ストキャスティクス版"}:
        calls.append(lambda: evaluate_anti_stochastic(data))
    return [call() for call in calls]


def signal_mask(result: pd.DataFrame, kind: str, direction: str) -> pd.Series:
    column = f"{kind}_{direction}"
    if column not in result.columns:
        return pd.Series(False, index=result.index)
    return result[column].fillna(False).astype(bool)


def add_event_markers(
    figure: go.Figure,
    result: pd.DataFrame,
    kind: str,
    direction: str,
) -> None:
    mask = signal_mask(result, kind, direction)
    if not mask.any():
        return
    is_long = direction == "long"
    is_hook = kind == "hook"
    symbol = ("triangle-up" if is_long else "triangle-down") if is_hook else "diamond"
    color = ("#0a8f4d" if is_long else "#d1495b") if is_hook else "#f0a202"
    figure.add_trace(
        go.Scatter(
            x=result.index[mask],
            y=result.loc[mask, "fast"],
            mode="markers",
            name=f"{kind.title()} {direction.title()}",
            marker={"symbol": symbol, "size": 11, "color": color},
            hovertemplate="%{x}<br>Fast=%{y:.4f}<extra></extra>",
        ),
        row=2,
        col=1,
    )


def build_chart(ticker: str, prices: pd.DataFrame, result: pd.DataFrame) -> go.Figure:
    validate_result(result)
    title = STRATEGY_LABELS.get(
        compact_text(result.iloc[-1].get("strategy_id")),
        compact_text(result.iloc[-1].get("strategy_id")),
    )
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        row_heights=[0.58, 0.42],
        subplot_titles=(f"{ticker} 調整済みOHLC", title),
    )
    figure.add_trace(
        go.Candlestick(
            x=prices.index,
            open=prices["Open"],
            high=prices["High"],
            low=prices["Low"],
            close=prices["Close"],
            name="Price",
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Scatter(x=result.index, y=result["fast"], name="Fast", line={"color": "#1f77b4"}),
        row=2,
        col=1,
    )
    figure.add_trace(
        go.Scatter(x=result.index, y=result["slow"], name="Slow", line={"color": "#d62728"}),
        row=2,
        col=1,
    )
    figure.add_trace(
        go.Bar(
            x=result.index,
            y=result["histogram"],
            name="Fast - Slow",
            marker_color="#8c9eff",
            opacity=0.35,
        ),
        row=2,
        col=1,
    )
    for event_kind in ("setup", "hook"):
        for event_direction in ("long", "short"):
            add_event_markers(figure, result, event_kind, event_direction)
    figure.update_layout(
        height=680,
        margin={"l": 10, "r": 10, "t": 55, "b": 10},
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
    )
    figure.update_xaxes(rangeslider_visible=False)
    return figure


def run_screening(
    tickers: list[str],
    period: str,
    selected_strategy: str,
    pullback_bars: int,
    allow_cross: bool,
    drop_today: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[tuple[str, str], Any]]:
    summaries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    chart_data: dict[tuple[str, str], Any] = {}
    progress = st.progress(0.0)
    status = st.empty()

    for position, ticker in enumerate(tickers, start=1):
        status.text(f"取得・判定中 ({position}/{len(tickers)}): {ticker}")
        try:
            raw = fetch_daily_prices(ticker, period)
        except Exception as exc:  # yfinance側の障害を銘柄単位で隔離する
            errors.append(
                {"Ticker": ticker, "区分": "取得失敗", "詳細": f"{type(exc).__name__}: {exc}"}
            )
            progress.progress(position / len(tickers))
            continue

        if raw.empty:
            errors.append({"Ticker": ticker, "区分": "データなし", "詳細": "日足を取得できませんでした"})
            progress.progress(position / len(tickers))
            continue

        prices = exclude_current_day_bar(raw, drop_today)
        if prices.empty:
            errors.append({"Ticker": ticker, "区分": "データ不足", "詳細": "判定可能な確定日足がありません"})
            progress.progress(position / len(tickers))
            continue

        try:
            results = calculate_strategies(prices, selected_strategy, pullback_bars, allow_cross)
            for result in results:
                summary = latest_summary(ticker, result)
                summaries.append(summary)
                strategy_id = compact_text(result.iloc[-1]["strategy_id"])
                chart_data[(ticker, strategy_id)] = (prices, result)
        except Exception as exc:
            errors.append(
                {"Ticker": ticker, "区分": "計算失敗", "詳細": f"{type(exc).__name__}: {exc}"}
            )
        progress.progress(position / len(tickers))

    status.text("スクリーニング完了")
    return summaries, errors, chart_data




def main() -> None:
    from stock_screener.workspace_ui import render_workspace
    render_workspace()


if __name__ == "__main__":
    main()
