"""Option OI flow: per-strike OI through the day, build-up type, wall migration, unwinding, near-ATM PCR trend.

Source: archived 1-minute option candles (Kite historical with oi=1, or the collector's live candles), which
carry the open interest at each minute. For any decision time `as_of`, only rows with ts < as_of are used.

Conventions used for labels (they describe price/OI combinations, NOT who traded - Kite does not say):
  premium up + OI up    -> LONG_BUILDUP        premium down + OI up   -> SHORT_BUILDUP (writing)
  premium up + OI down  -> SHORT_COVERING      premium down + OI down -> LONG_UNWINDING
A call wall is the strike with the most call OI at or above ATM (resistance by convention), a put wall the
strike with the most put OI at or below ATM. A wall is DEFENDED when its OI keeps rising as price approaches,
UNWINDING when its OI falls by >= 5% over 30 minutes.
"""

from __future__ import annotations

import glob
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import structlog

from mmm.analytics.levels import Level
from mmm.settings import get_config
from mmm.timeutil import IST

log = structlog.get_logger(__name__)

BUILDUP_MIN_OI_PCT = 2.0
UNWIND_PCT = -5.0
DEFEND_PCT = 3.0


def buildup_label(price_chg: float | None, oi_chg_pct: float | None) -> str:
    if price_chg is None or oi_chg_pct is None or np.isnan(price_chg) or np.isnan(oi_chg_pct) or abs(oi_chg_pct) < BUILDUP_MIN_OI_PCT:
        return "NEUTRAL"
    if oi_chg_pct > 0:
        return "LONG_BUILDUP" if price_chg > 0 else "SHORT_BUILDUP"
    return "SHORT_COVERING" if price_chg > 0 else "LONG_UNWINDING"


@dataclass
class Wall:
    side: str              # CALL | PUT
    strike: float
    oi: int
    strength: float        # wall OI / median OI on that side (>= 1)
    oi_chg_30_pct: float | None
    status: str            # DEFENDED | UNWINDING | NEUTRAL
    strike_at_open: float | None

    @property
    def migration(self) -> str:
        if self.strike_at_open is None or self.strike_at_open == self.strike:
            return "SAME"
        return "UP" if self.strike > self.strike_at_open else "DOWN"

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["migration"] = self.migration
        return d


@dataclass
class OIState:
    as_of: datetime
    expiry: date
    spot: float
    atm: float
    table: pd.DataFrame                    # per strike: ce/pe oi, changes, premium, build-up
    call_wall: Wall | None
    put_wall: Wall | None
    pcr_total: float | None
    pcr_near: float | None
    pcr_near_open: float | None
    pcr_near_30: float | None
    strikes_present: int
    coverage_note: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def pcr_trend(self) -> str:
        if self.pcr_near is None or self.pcr_near_30 is None:
            return "UNKNOWN"
        d = self.pcr_near - self.pcr_near_30
        return "RISING" if d > 0.05 else ("FALLING" if d < -0.05 else "FLAT")

    def wall_levels(self) -> list[Level]:
        out = []
        for w, kind in ((self.call_wall, "OI_R"), (self.put_wall, "OI_S")):
            if w is None:
                continue
            strength = 50 + 40 * min(1.0, max(0.0, (w.strength - 1.0) / 3.0))
            out.append(Level(float(w.strike), kind, "options", strength, tags=[w.status, f"migration {w.migration}"]))
        return out

    def confirmation(self, direction: int) -> float:
        """-1..+1 options-flow support for a trade in `direction` (used by scoring only once validated)."""
        s = 0.0
        if self.pcr_trend == "RISING":
            s += 0.35 * direction
        elif self.pcr_trend == "FALLING":
            s -= 0.35 * direction
        if direction > 0 and self.call_wall is not None:
            s += 0.35 if self.call_wall.status == "UNWINDING" else (-0.35 if self.call_wall.status == "DEFENDED" and self.call_wall.strike - self.spot < 100 else 0)
        if direction < 0 and self.put_wall is not None:
            s += 0.35 if self.put_wall.status == "UNWINDING" else (-0.35 if self.put_wall.status == "DEFENDED" and self.spot - self.put_wall.strike < 100 else 0)
        atm = self.table[self.table["strike"] == self.atm]
        if not atm.empty:
            r = atm.iloc[0]
            if r["pe_buildup"] == "SHORT_BUILDUP" and direction > 0:
                s += 0.3
            if r["ce_buildup"] == "SHORT_BUILDUP" and direction < 0:
                s += 0.3
        return float(np.clip(s, -1, 1))

    def as_dict(self) -> dict[str, Any]:
        return {"as_of": self.as_of, "expiry": self.expiry, "spot": self.spot, "atm": self.atm,
                "call_wall": self.call_wall.as_dict() if self.call_wall else None, "put_wall": self.put_wall.as_dict() if self.put_wall else None,
                "pcr_total": self.pcr_total, "pcr_near": self.pcr_near, "pcr_near_open": self.pcr_near_open, "pcr_near_30": self.pcr_near_30,
                "pcr_trend": self.pcr_trend, "strikes_present": self.strikes_present, "coverage_note": self.coverage_note, "notes": self.notes}


def _option_meta() -> pd.DataFrame:
    """token -> expiry, strike, option_type for every NIFTY option seen in any instrument snapshot."""
    from mmm.data import paths

    cfg = get_config().universe
    base = paths.data_dir() / "instruments"
    frames = []
    for f in sorted(base.glob("date=*/instruments.parquet")) if base.exists() else []:
        try:
            df = pd.read_parquet(f, columns=["instrument_token", "name", "expiry", "strike", "instrument_type", "exchange"])
        except Exception as exc:  # noqa: BLE001
            log.warning("instrument_snapshot_unreadable", file=str(f), error=str(exc))
            continue
        frames.append(df[(df["exchange"] == cfg.fno_exchange) & (df["name"] == cfg.fno_name) & df["instrument_type"].isin(["CE", "PE"])])
    if not frames:
        return pd.DataFrame(columns=["token", "expiry", "strike", "option_type"])
    m = pd.concat(frames, ignore_index=True).drop_duplicates("instrument_token")
    return pd.DataFrame({"token": m["instrument_token"].astype("int64"), "expiry": pd.to_datetime(m["expiry"]).dt.date,
                         "strike": m["strike"].astype(float), "option_type": m["instrument_type"].astype(str)})


class ChainHistory:
    """Long frame of option minute rows (ts, token, expiry, strike, option_type, close, oi, volume) with causal access."""

    def __init__(self, rows: pd.DataFrame):
        self.rows = rows.sort_values("ts").reset_index(drop=True) if not rows.empty else rows
        if not self.rows.empty:
            self._date = self.rows["ts"].dt.date.to_numpy()
            pairs = pd.DataFrame({"d": self._date, "e": self.rows["expiry"].to_numpy()}).drop_duplicates()
            self._expiries: dict[date, list[date]] = {d: sorted(g["e"]) for d, g in pairs.groupby("d")}
        else:
            self._date, self._expiries = np.array([], dtype=object), {}
        self.days: set[date] = set(self._expiries)
        self._pivots: dict[tuple[date, date], tuple[np.ndarray, pd.DataFrame, pd.DataFrame] | None] = {}

    @property
    def empty(self) -> bool:
        return self.rows.empty

    @property
    def n_days(self) -> int:
        return len(self.days)

    @classmethod
    def from_archive(cls, start: date | None = None, end: date | None = None) -> ChainHistory:
        import duckdb

        from mmm.data import paths

        meta = _option_meta()
        if meta.empty:
            return cls(pd.DataFrame(columns=["ts", "token", "expiry", "strike", "option_type", "close", "oi", "volume"]))
        pattern = paths.candles_glob("minute")
        tokens = set(meta["token"])
        files = [f for f in glob.glob(pattern) if (m := re.search(r"token=(\d+)", f)) and int(m.group(1)) in tokens]
        if not files:
            return cls(pd.DataFrame(columns=["ts", "token", "expiry", "strike", "option_type", "close", "oi", "volume"]))
        con = duckdb.connect()
        con.execute("SET TimeZone='Asia/Kolkata'")
        flist = "[" + ",".join(f"'{f}'" for f in files) + "]"
        q = f"SELECT ts, close, oi, volume, filename FROM read_parquet({flist}, filename=true)"
        conds = []
        if start:
            conds.append(f"ts >= TIMESTAMPTZ '{start.isoformat()} 00:00:00+05:30'")
        if end:
            conds.append(f"ts <= TIMESTAMPTZ '{end.isoformat()} 23:59:59+05:30'")
        if conds:
            q += " WHERE " + " AND ".join(conds)
        df = con.execute(q).df()
        con.close()
        if df.empty:
            return cls(pd.DataFrame(columns=["ts", "token", "expiry", "strike", "option_type", "close", "oi", "volume"]))
        df["token"] = df["filename"].str.extract(r"token=(\d+)")[0].astype("int64")
        df = df.drop(columns="filename").merge(meta, on="token", how="inner")
        df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(IST)
        return cls(df[["ts", "token", "expiry", "strike", "option_type", "close", "oi", "volume"]])

    def slice(self, lo: date, hi: date) -> ChainHistory:
        d = self._date
        return ChainHistory(self.rows[(d >= lo - timedelta(days=5)) & (d <= hi)])

    # --- per-day pivots ---------------------------------------------------------------------------------
    def expiry_for(self, as_of: datetime) -> date | None:
        d = as_of.date()
        exps = [e for e in self._expiries.get(d, []) if e >= d]
        if not exps:
            return None
        if exps[0] == d and as_of.hour >= 13 and len(exps) > 1:
            return exps[1]
        return exps[0]

    def _pivot(self, day: date, expiry: date) -> tuple[np.ndarray, pd.DataFrame, pd.DataFrame] | None:
        key = (day, expiry)
        if key in self._pivots:
            return self._pivots[key]
        prev_days = sorted(d for d in self.days if d < day)
        lo_day = prev_days[-1] if prev_days else day
        m = (self._date >= lo_day) & (self._date <= day) & (self.rows["expiry"].to_numpy() == expiry)
        r = self.rows[m]
        if r.empty or day not in set(self._date[m]):
            self._pivots[key] = None
            return None
        r = r.assign(col=r["strike"].astype(float).map("{:.0f}".format) + r["option_type"])
        oi = r.pivot_table(index="ts", columns="col", values="oi", aggfunc="last").sort_index().ffill()
        px = r.pivot_table(index="ts", columns="col", values="close", aggfunc="last").sort_index().ffill()
        ts = oi.index.tz_convert(None).to_numpy()
        self._pivots[key] = (ts, oi, px)
        return self._pivots[key]

    @staticmethod
    def _idx_before(ts: np.ndarray, t: datetime) -> int:
        return int(np.searchsorted(ts, np.datetime64(pd.Timestamp(t).tz_convert("UTC").tz_localize(None), "ns"), side="left")) - 1

    def _row_before(self, ts: np.ndarray, frame: pd.DataFrame, t: datetime) -> pd.Series | None:
        i = self._idx_before(ts, t)
        return frame.iloc[i] if i >= 0 else None

    def state(self, as_of: datetime, spot: float, near_strikes: int = 3) -> OIState | None:
        expiry = self.expiry_for(as_of)
        if expiry is None:
            return None
        pv = self._pivot(as_of.date(), expiry)
        if pv is None:
            return None
        ts, oi, px = pv
        i_now = self._idx_before(ts, as_of)
        if i_now < 0 or oi.index[i_now].date() != as_of.date():
            return None                                    # nothing printed yet today
        now_oi, now_px = oi.iloc[i_now], px.iloc[i_now]
        day_start = datetime(as_of.year, as_of.month, as_of.day, 9, 15, tzinfo=IST)
        base_oi = self._row_before(ts, oi, day_start)      # previous session's last OI (change since previous close)
        if base_oi is None:
            base_oi = oi[oi.index >= day_start].iloc[0]
        o30, p30 = self._row_before(ts, oi, as_of - timedelta(minutes=30)), self._row_before(ts, px, as_of - timedelta(minutes=30))
        step = get_config().universe.strike_step
        atm = round(spot / step) * step
        strikes = sorted({float(c[:-2]) for c in oi.columns})
        rows = []
        for k in strikes:
            rec: dict[str, Any] = {"strike": k}
            for side in ("CE", "PE"):
                col = f"{k:.0f}{side}"
                cur = now_oi.get(col, np.nan)
                rec[f"{side.lower()}_oi"] = cur
                b = base_oi.get(col, np.nan) if base_oi is not None else np.nan
                rec[f"{side.lower()}_chg"] = cur - b
                a30 = o30.get(col, np.nan) if o30 is not None else np.nan
                rec[f"{side.lower()}_chg_30_pct"] = (cur - a30) / a30 * 100 if a30 and not np.isnan(a30) else np.nan
                pnow = now_px.get(col, np.nan) if now_px is not None else np.nan
                pb = p30.get(col, np.nan) if p30 is not None else np.nan
                rec[f"{side.lower()}_px"] = pnow
                rec[f"{side.lower()}_px_chg_30"] = pnow - pb
                rec[f"{side.lower()}_buildup"] = buildup_label(pnow - pb, rec[f"{side.lower()}_chg_30_pct"])
            rows.append(rec)
        table = pd.DataFrame(rows)
        if table.empty:
            return None
        near = table[(table["strike"] - atm).abs() <= near_strikes * step]

        def _pcr(frame: pd.DataFrame, ce: str, pe: str) -> float | None:
            c, p = frame[ce].sum(), frame[pe].sum()
            return float(p / c) if c and not (pd.isna(c) or pd.isna(p)) else None

        pcr_total = _pcr(table, "ce_oi", "pe_oi")
        pcr_near = _pcr(near, "ce_oi", "pe_oi")
        open_row = oi[oi.index >= day_start]
        pcr_open = pcr_30 = None
        near_cols_ce = [f"{k:.0f}CE" for k in near["strike"] if f"{k:.0f}CE" in oi.columns]
        near_cols_pe = [f"{k:.0f}PE" for k in near["strike"] if f"{k:.0f}PE" in oi.columns]
        if not open_row.empty and near_cols_ce and near_cols_pe:
            r0 = open_row.iloc[0]
            pcr_open = float(r0[near_cols_pe].sum() / r0[near_cols_ce].sum()) if r0[near_cols_ce].sum() else None
        if o30 is not None and near_cols_ce and near_cols_pe and o30[near_cols_ce].sum():
            pcr_30 = float(o30[near_cols_pe].sum() / o30[near_cols_ce].sum())

        def _wall(side: str) -> Wall | None:
            col = "ce_oi" if side == "CALL" else "pe_oi"
            cand = table[table["strike"] >= atm] if side == "CALL" else table[table["strike"] <= atm]
            cand = cand[cand[col].notna() & (cand[col] > 0)]
            if cand.empty:
                return None
            r = cand.loc[cand[col].idxmax()]
            med = float(table[col][table[col] > 0].median()) if (table[col] > 0).any() else 0.0
            chg = r["ce_chg_30_pct" if side == "CALL" else "pe_chg_30_pct"]
            chg = None if pd.isna(chg) else float(chg)
            status = "NEUTRAL"
            if chg is not None and chg <= UNWIND_PCT:
                status = "UNWINDING"
            elif chg is not None and chg >= DEFEND_PCT and abs(r["strike"] - spot) <= 2 * step:
                status = "DEFENDED"
            at_open = None
            if not open_row.empty:
                r0 = open_row.iloc[0]
                cols = [f"{k:.0f}{'CE' if side == 'CALL' else 'PE'}" for k in cand["strike"]]
                cols = [c for c in cols if c in r0.index and not pd.isna(r0[c])]
                if cols:
                    at_open = float(r0[cols].astype(float).idxmax()[:-2])
            return Wall(side, float(r["strike"]), int(r[col]), float(r[col] / med) if med else 1.0, chg, status, at_open)

        exp_strikes = 2 * get_config().universe.option_strikes_each_side + 1
        cov = f"{len(strikes)} strikes archived for {expiry} (collector universe is {exp_strikes})"
        return OIState(as_of, expiry, spot, float(atm), table, _wall("CALL"), _wall("PUT"), pcr_total, pcr_near, pcr_open, pcr_30,
                       len(strikes), cov, ["DERIVED from archived option minute candles; labels are price/OI conventions"])

    # --- series for the UI -------------------------------------------------------------------------------
    def heatmap(self, day: date, expiry: date | None = None, bucket_minutes: int = 5) -> pd.DataFrame:
        """OI change since the previous close per strike/side, sampled every `bucket_minutes`."""
        expiry = expiry or self.expiry_for(datetime(day.year, day.month, day.day, 15, 0, tzinfo=IST))
        if expiry is None:
            return pd.DataFrame()
        pv = self._pivot(day, expiry)
        if pv is None:
            return pd.DataFrame()
        _ts, oi, _px = pv
        start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
        prev = oi[oi.index < start]
        base = prev.iloc[-1] if not prev.empty else oi[oi.index >= start].iloc[0]
        today = oi[oi.index >= start]
        sampled = today.resample(f"{bucket_minutes}min", origin=start).last().dropna(how="all")
        chg = sampled - base
        long = chg.reset_index().melt(id_vars="ts", var_name="col", value_name="oi_chg")
        long["strike"] = long["col"].str[:-2].astype(float)
        long["side"] = long["col"].str[-2:]
        return long.drop(columns="col")

    def wall_series(self, day: date, spot_series: pd.DataFrame | None = None, every: int = 5) -> pd.DataFrame:
        out = []
        start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
        for k in range(1, 76):
            t = start + timedelta(minutes=every * k)
            spot = None
            if spot_series is not None and not spot_series.empty:
                s = spot_series[spot_series["ts"] < t]
                spot = float(s["close"].iloc[-1]) if not s.empty else None
            if spot is None:
                continue
            st = self.state(t, spot)
            if st is None:
                continue
            out.append({"ts": t, "spot": spot, "call_wall": st.call_wall.strike if st.call_wall else None,
                        "put_wall": st.put_wall.strike if st.put_wall else None, "pcr_near": st.pcr_near})
        return pd.DataFrame(out)
