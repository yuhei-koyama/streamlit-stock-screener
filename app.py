"""LBR Anti のセットアップ／フックを確認する Streamlit UI。

この画面は検証用スクリーナーであり、売買注文、損益計算、バックテストを
行わない。指標と判定は ``stock_screener.anti`` の共通ロジックだけを使う。
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


st.set_page_config(
    page_title="LBR Anti ロジック検証",
    page_icon="📈",
    layout="wide",
)

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
        if re.fullmatch(r"[0-9]{3}[0-9A-Z]", value):
            value = f"{value}.T"
        if not re.fullmatch(r"[0-9]{3}[0-9A-Z]\.T", value):
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
    st.title("LBR Anti ロジック検証")
    st.caption(
        "セットアップとフックの成立過程を確認するための画面です。"
        "売買指示、エントリー、決済、損益、バックテストは提供しません。"
    )

    with st.sidebar:
        st.header("検証設定")
        universe = st.selectbox("対象", [*PRESET_TICKERS, "カスタム（東証ティッカー）"])
        if universe == "カスタム（東証ティッカー）":
            custom = st.text_area(
                "コード（カンマ・空白区切り）",
                value="7203, 1306.T",
                help="銘柄コードだけの入力には .T を補います。米国株・FXは今回の対象外です。",
            )
            tickers, invalid_tickers = normalize_tickers(custom)
        else:
            tickers = PRESET_TICKERS[universe]
            invalid_tickers = []

        selected_strategy = st.selectbox(
            "Anti定義",
            ["両方", "3/10適応版", "ストキャスティクス版"],
        )
        pullback_bars = st.radio(
            "3/10版の押し期間",
            [3, 2],
            horizontal=True,
            help="ストキャスティクス版は3本固定です。",
        )
        allow_cross = st.checkbox(
            "3/10版でFastのSlow通過を許可",
            value=True,
            help="OFFでもFastとSlowの接触は許可します。",
        )
        period = st.selectbox("取得期間", ["6mo", "1y", "2y", "5y"], index=1)
        drop_today = st.checkbox(
            "当日足を除外",
            value=True,
            help="市場終了の判定はせず、当日の日足を常に除外する保守的な設定です。",
        )
        latest_signals_only = st.checkbox("最新バーがSetup/Hookの行だけ表示", value=False)

    if invalid_tickers:
        st.error(f"無効な東証ティッカー: {', '.join(invalid_tickers)}")
    if len(tickers) > MAX_TICKERS:
        st.error(f"一度に指定できる銘柄は{MAX_TICKERS}件までです。")
    can_run = bool(tickers) and not invalid_tickers and len(tickers) <= MAX_TICKERS

    if st.button("スクリーニング実行", type="primary", disabled=not can_run):
        summaries, errors, chart_data = run_screening(
            tickers,
            period,
            selected_strategy,
            int(pullback_bars),
            allow_cross,
            drop_today,
        )
        st.session_state["anti_summaries"] = summaries
        st.session_state["anti_errors"] = errors
        st.session_state["anti_chart_data"] = chart_data
        st.session_state["anti_run_at"] = datetime.now().astimezone().isoformat()

    summaries = st.session_state.get("anti_summaries")
    if summaries is None:
        st.info("設定を確認し、スクリーニングを実行してください。")
        return

    st.caption(f"実行時刻: {st.session_state.get('anti_run_at', '')}")
    summary_frame = pd.DataFrame(summaries)
    if latest_signals_only and not summary_frame.empty:
        summary_frame = summary_frame[summary_frame["Setup"] | summary_frame["Hook"]]

    st.subheader(f"最新状態 ({len(summary_frame)}件)")
    if summary_frame.empty:
        st.info("表示条件に該当する最新状態はありません。")
    else:
        st.dataframe(summary_frame, use_container_width=True, hide_index=True)
        st.download_button(
            "結果CSVをダウンロード",
            summary_frame.to_csv(index=False).encode("utf-8-sig"),
            file_name="lbr_anti_logic_snapshot.csv",
            mime="text/csv",
        )

    errors = st.session_state.get("anti_errors", [])
    if errors:
        with st.expander(f"取得・計算上の問題 ({len(errors)}件)", expanded=True):
            st.dataframe(pd.DataFrame(errors), use_container_width=True, hide_index=True)

    chart_data = st.session_state.get("anti_chart_data", {})
    if chart_data:
        st.subheader("状態遷移チャート")
        options = list(chart_data)
        labels = {
            key: f"{key[0]} — {STRATEGY_LABELS.get(key[1], key[1])}" for key in options
        }
        selected = st.selectbox("銘柄・定義", options, format_func=lambda key: labels[key])
        prices, result = chart_data[selected]
        st.plotly_chart(
            build_chart(selected[0], prices, result),
            use_container_width=True,
            config={"displaylogo": False, "responsive": True},
        )
        with st.expander("最新の判定中間値"):
            columns = [column for column in RESULT_COLUMNS if column in result.columns]
            st.dataframe(result.loc[:, columns].tail(15), use_container_width=True)

    st.warning(
        "Yahoo Financeの調整済み日足を用いたロジック確認用表示です。"
        "データ品質や将来の運用成績を保証せず、売買判断には使用できません。"
    )


if __name__ == "__main__":
    main()
