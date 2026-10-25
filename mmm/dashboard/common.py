"""Shared data access and plotting for the dashboard.

Data comes from the local archive (Parquet + SQLite). When nothing has been captured yet, pages fall back to
clearly labelled SYNTHETIC DEMO data so the UI can be explored. Demo data is never written to the archive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from mmm import demo
from mmm.analytics.levels import Level
from mmm.analytics.options.chain import enrich_chain
from mmm.analytics.structure import Swing
from mmm.data import parquet_store
from mmm.data.instruments import build_universe, load_instruments
from mmm.data.state_db import StateDB
from mmm.settings import get_config
from mmm.timeutil import IST, now_ist

DEMO_BANNER = "⚠️ **SYNTHETIC DEMO DATA** — nothing has been captured from Zerodha yet. Run `mmm auth login`, `mmm data instruments`, `mmm data backfill` and `mmm collect`. Numbers below are generated, not market data."
DERIVED_NOTE = "IV, Greeks, PCR, max pain, expected move and basis are DERIVED internally (Black-Scholes, configured r/q) — they are not Zerodha fields."


@dataclass
class Bundle:
    is_demo: bool
    as_of: datetime
    spot: float
    minute_bars: pd.DataFrame
    chain: pd.DataFrame                 # enriched
    expiries: list[date]
    futures_quote: dict[str, Any]
    constituents: pd.DataFrame
    universe_size: int
    vix: float | None = None
    notes: list[str] = field(default_factory=list)


@st.cache_resource
def state_db() -> StateDB:
    return StateDB()


def _real_minute_bars(index_token: int, days: int = 60) -> pd.DataFrame:
    start = now_ist().date() - timedelta(days=days)
    return parquet_store.read_candles("minute", index_token, start=start)


@st.cache_data(ttl=20, show_spinner=False)
def load_bundle(days: int = 40) -> Bundle:
    cfg = get_config()
    inst = load_instruments()
    state = state_db()
    notes: list[str] = []
    if not inst.empty:
        idx = inst[(inst["exchange"] == cfg.universe.index_exchange) & (inst["tradingsymbol"] == cfg.universe.index_symbol)]
        if not idx.empty:
            token = int(idx.iloc[0]["instrument_token"])
            bars = _real_minute_bars(token, days)
            if len(bars) > 500:
                quotes = {q["instrument_token"]: q for q in state.quotes()}
                spot_q = quotes.get(token)
                spot = float(spot_q["last_price"]) if spot_q and spot_q.get("last_price") else float(bars["close"].iloc[-1])
                as_of = pd.Timestamp(spot_q["received_ts"]).to_pydatetime() if spot_q and spot_q.get("received_ts") else bars["ts"].iloc[-1].to_pydatetime()
                uni = build_universe(inst, spot)
                chain_rows = state.chain()
                chain = pd.DataFrame(chain_rows) if chain_rows else pd.DataFrame()
                if not chain.empty:
                    chain = enrich_chain(chain, spot, as_of)
                else:
                    notes.append("no option-chain snapshot in state DB yet (collector not running)")
                fut_q: dict[str, Any] = {}
                if not uni.futures.empty:
                    fut_q = quotes.get(int(uni.futures.iloc[0]["instrument_token"]), {})
                cons = pd.DataFrame([quotes[t] for t in uni.constituents["instrument_token"].astype(int) if t in quotes]) if not uni.constituents.empty else pd.DataFrame()
                vix_q = quotes.get(int(uni.vix["instrument_token"])) if uni.vix is not None else None
                return Bundle(False, as_of, spot, bars, chain, uni.expiries, fut_q, cons, len(cfg.universe.constituents),
                              float(vix_q["last_price"]) if vix_q and vix_q.get("last_price") else None, notes)
    # ---- demo fallback ----
    bars = demo.demo_minute_bars(days=days)
    as_of = bars["ts"].iloc[-1].to_pydatetime() + timedelta(minutes=1)
    spot = float(bars["close"].iloc[-1])
    e1 = demo.next_weekly_expiry(as_of.date())
    e2 = e1 + timedelta(days=7)
    chain = pd.concat([demo.demo_option_chain(spot, e1, as_of, seed=7), demo.demo_option_chain(spot, e2, as_of, seed=8, base_iv=0.135)])
    chain = enrich_chain(chain, spot, as_of)
    return Bundle(True, as_of, spot, bars, chain, [e1, e2], demo.demo_futures_quote(spot), demo.demo_constituent_quotes(),
                  50, 13.8, ["demo"])


def banner(b: Bundle) -> None:
    if b.is_demo:
        st.warning(DEMO_BANNER)
    else:
        age = (now_ist() - b.as_of).total_seconds()
        st.caption(f"Data as of {b.as_of:%Y-%m-%d %H:%M:%S} IST ({age:,.0f}s ago) · source: Zerodha Kite via local archive")
    for n in b.notes:
        if n != "demo":
            st.info(n)


def page_header(title: str, subtitle: str = "") -> None:
    st.title(title)
    if subtitle:
        st.caption(subtitle)


def fmt(x: float | None, nd: int = 2, pct: bool = False, default: str = "–") -> str:
    if x is None or (isinstance(x, float) and (x != x)):
        return default
    return f"{x:,.{nd}f}%" if pct else f"{x:,.{nd}f}"


def candlestick(df: pd.DataFrame, title: str = "", levels: list[Level] | None = None, swings: list[Swing] | None = None,
                height: int = 520, show_volume: bool = False, extra_lines: dict[str, pd.Series] | None = None) -> go.Figure:
    rows = 2 if show_volume else 1
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True, row_heights=[0.78, 0.22] if show_volume else [1], vertical_spacing=0.03)
    fig.add_trace(go.Candlestick(x=df["ts"], open=df["open"], high=df["high"], low=df["low"], close=df["close"], name="price",
                                 increasing_line_color="#26a69a", decreasing_line_color="#ef5350"), row=1, col=1)
    if extra_lines:
        for name, s in extra_lines.items():
            fig.add_trace(go.Scatter(x=df["ts"], y=s, name=name, mode="lines", line=dict(width=1.2)), row=1, col=1)
    if levels:
        colors = {"PDH": "#ff9800", "PDL": "#ff9800", "PDC": "#9e9e9e", "PWH": "#e040fb", "PWL": "#e040fb", "ORH": "#29b6f6", "ORL": "#29b6f6",
                  "SH": "#cfd8dc", "SL": "#cfd8dc", "OPEN": "#cfd8dc", "ZONE_R": "#ef5350", "ZONE_S": "#26a69a", "ROUND": "#616161"}
        for lv in levels:
            if lv.kind == "ROUND" and "major" not in lv.tags:
                continue
            fig.add_hline(y=lv.price, line=dict(color=colors.get(lv.kind, "#90a4ae"), width=1, dash="dot" if lv.kind.startswith("ZONE") else "solid"),
                          opacity=min(0.35 + lv.strength / 150, 0.95), annotation_text=f"{lv.kind} {lv.price:,.0f}", annotation_position="right",
                          annotation_font_size=10, row=1, col=1)
    if swings:
        hs = [s for s in swings if s.kind == "H"]
        ls = [s for s in swings if s.kind == "L"]
        fig.add_trace(go.Scatter(x=[s.ts for s in hs], y=[s.price for s in hs], mode="markers+text", text=[s.label or "" for s in hs],
                                 textposition="top center", marker=dict(symbol="triangle-down", color="#ef5350", size=9), name="swing H"), row=1, col=1)
        fig.add_trace(go.Scatter(x=[s.ts for s in ls], y=[s.price for s in ls], mode="markers+text", text=[s.label or "" for s in ls],
                                 textposition="bottom center", marker=dict(symbol="triangle-up", color="#26a69a", size=9), name="swing L"), row=1, col=1)
    if show_volume:
        fig.add_trace(go.Bar(x=df["ts"], y=df["volume"], name="volume", marker_color="#546e7a"), row=2, col=1)
    fig.update_layout(title=title, height=height, xaxis_rangeslider_visible=False, margin=dict(l=10, r=80, t=40, b=10),
                      legend=dict(orientation="h", y=1.02), template="plotly_dark")
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"]), dict(bounds=[15.5, 9.25], pattern="hour")])
    return fig


def trade_marker(fig: go.Figure, entry: float, stop: float, t1: float, t2: float) -> go.Figure:
    for y, c, n in ((entry, "#ffffff", "entry"), (stop, "#ef5350", "stop"), (t1, "#26a69a", "T1"), (t2, "#66bb6a", "T2")):
        fig.add_hline(y=y, line=dict(color=c, width=1.5, dash="dash"), annotation_text=f"{n} {y:,.0f}", annotation_position="left")
    return fig


def session_bars(bars: pd.DataFrame, d: date) -> pd.DataFrame:
    return bars[bars["ts"].dt.date == d]


def expiry_label(e: date, as_of: datetime) -> str:
    dte = (datetime.combine(e, datetime.min.time(), tzinfo=IST) - as_of.replace(hour=0, minute=0, second=0, microsecond=0)).days
    return f"{e:%d %b %Y} ({dte} DTE)"
