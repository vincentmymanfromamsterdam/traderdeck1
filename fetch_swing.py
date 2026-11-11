"""
TRADERDECK — Swing Screener (Stock DNA + Regime)
=================================================
Computes, for the Carnivore portfolios + personal watchlist:

  Stock DNA (Iain / Swing Data):   ADR%, ATR, ATR-multiple from 50EMA, % off 52W high,
                                   avg $ volume, RS Rating (percentile vs S&P 500 + universe),
                                   Stage (Weinstein), LoD distance
  Structure / confluence:          distance (in ATR) to EMA9 / EMA21 / EMA50 / 10-week MA /
                                   AVWAP from last swing low; confluence count within 1 ATR
  Behaviour:                       pocket pivot, volume dry-up, tightness,
                                   weakness-in-strength (green on red SPY days)
  Regime (Luk watchlist breadth):  % of universe above rising EMA9 AND EMA21 -> green/yellow/red,
                                   with a rolling history

Output: data/swing_data.json
Run:    python fetch_swing.py
"""

import datetime as dt
import json
import os
import sys

try:
    import numpy as np
    import pandas as pd
    import yfinance as yf
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "yfinance", "pandas", "numpy", "-q"])
    import numpy as np
    import pandas as pd
    import yfinance as yf

OUTPUT_PATH     = "data/swing_data.json"
PORTFOLIO_PATH  = "data/carnivore_portfolios.json"

# ─────────────────────────────────────────────────────────
#  PERSONAL WATCHLIST  (v7.4: data/watchlist.json, edited from the dashboard)
# ─────────────────────────────────────────────────────────
WATCHLIST_PATH = "data/watchlist.json"


def load_watchlist():
    """Tickers from data/watchlist.json ({"tickers": [{"ticker", "added", "source"}]}).
    The dashboard writes this file through the GitHub contents API; Carnivore names
    are added separately below and are not part of it."""
    try:
        with open(WATCHLIST_PATH) as f:
            d = json.load(f)
        return [str(x["ticker"]).upper().strip() for x in d.get("tickers", []) if x.get("ticker")]
    except Exception as e:
        print(f"  (no watchlist file: {e})")
        return []


WATCHLIST = load_watchlist()

# Hard filters (Stock DNA). Thresholds from "Mijn Swing Strategie".
DNA = {
    "adr_pct_min":        3.0,
    "avg_dollar_vol_min": 50_000_000,
    "rs_rating_min":      85,
    "off_52w_high_max":   25.0,   # % below 52W high
    "atr_mult_max":       10.0,   # ATR multiple from EMA50
}
EXT_CAUTION = 10.0   # ATR multiple: do not chase
EXT_REDUCE  = 15.0   # ATR multiple: consider trimming

REGIME_GREEN  = 60.0  # % of universe above rising EMA9+EMA21
REGIME_YELLOW = 40.0


# ─────────────────────────────────────────────────────────
#  UNIVERSE
# ─────────────────────────────────────────────────────────
def load_universe():
    sources = {"watchlist": load_watchlist(), "carnivore_sr": [], "carnivore_lt": []}
    try:
        with open(PORTFOLIO_PATH) as f:
            p = json.load(f)
        sources["carnivore_sr"] = [x["ticker"] for x in p.get("sector_rotation", []) if x.get("ticker")]
        sources["carnivore_lt"] = [x["ticker"] for x in p.get("long_term", []) if x.get("ticker")]
    except Exception as e:
        print(f"  (no portfolio file: {e})")

    seen, universe = set(), []
    for t in sources["carnivore_sr"] + sources["carnivore_lt"] + sources["watchlist"]:
        t = t.upper().strip()
        if t and t not in seen:
            seen.add(t)
            universe.append(t)
    return universe, sources


def load_sp500():
    """Top-500 US stocks by market cap (live). Falls back to the static list in fetch_data.py."""
    try:
        from sector_map import get_benchmark_tickers
        try:
            from fetch_data import SP500_TICKERS
        except Exception:
            SP500_TICKERS = []
        return get_benchmark_tickers(500, fallback=SP500_TICKERS)
    except Exception as e:
        print(f"  (benchmark list unavailable: {e})")
        return []


# ─────────────────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────────────────
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def atr(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()

def ret(close, n):
    if len(close) <= n:
        return None
    a, b = float(close.iloc[-1]), float(close.iloc[-1 - n])
    return (a / b - 1) * 100 if b else None

RS_LOOKBACK_1M = 21      # trading days back for the "RS a month ago" percentile


def rs_raw(close):
    """IBD-style weighted 12-month performance: 40% Q1, 20% Q2, 20% Q3, 20% Q4."""
    parts = [(63, 0.4), (126, 0.2), (189, 0.2), (252, 0.2)]
    total, w_sum = 0.0, 0.0
    for n, w in parts:
        r = ret(close, n)
        if r is not None:
            total += w * r
            w_sum += w
    return total / w_sum if w_sum else None

def r2(v, d=2):
    if v is None:
        return None
    try:
        if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
            return None
        return round(float(v), d)
    except Exception:
        return None


def set_tt_rs(m):
    """Trend Template criterion 8 (RS >= 70) once the RS percentile is known."""
    if m.get("tt_flags"):
        m["tt_flags"]["rs_70"] = bool((m.get("rs_rating") or 0) >= 70)
        m["tt"] = int(sum(m["tt_flags"].values()))


def analyse(ticker, df, spy_chg):
    """Return a metrics dict for one ticker. df = OHLCV daily, oldest first."""
    df = df.dropna(subset=["Close", "High", "Low", "Volume"])
    if len(df) < 60:
        return None

    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
    close = float(c.iloc[-1])
    chg_1d = (close / float(c.iloc[-2]) - 1) * 100

    e9, e21, e50 = ema(c, 9), ema(c, 21), ema(c, 50)
    s50  = c.rolling(50).mean()          # 10-week MA on the daily
    s200 = c.rolling(200).mean() if len(c) >= 200 else None
    a14  = atr(df, 14)
    atr_now = float(a14.iloc[-1])
    if not atr_now or np.isnan(atr_now):
        return None

    def rising(s, lag=5):
        return bool(float(s.iloc[-1]) > float(s.iloc[-1 - lag])) if len(s) > lag else False

    # ── Stock DNA ─────────────────────────────────────────
    adr_pct = float(((h - l) / c).iloc[-20:].mean() * 100)
    atr_pct = atr_now / close * 100
    atr_mult_50 = (close - float(e50.iloc[-1])) / atr_now
    pct_from_50 = (close / float(e50.iloc[-1]) - 1) * 100
    hi52 = float(h.iloc[-252:].max())
    off_52w_high = (close / hi52 - 1) * 100
    avg_dollar_vol = float((c * v).iloc[-20:].mean())
    day_range = float(h.iloc[-1] - l.iloc[-1])
    lod_dist = (close - float(l.iloc[-1])) / day_range * 100 if day_range > 0 else None

    # ── Stage (Weinstein) ─────────────────────────────────
    # Stage 1 = base: the 200 has gone flat after a decline, or price has
    # reclaimed a still-falling 200. Stage 3 = top/distribution. The old
    # version called everything under a non-rising 200 "Stage 4", which put
    # most real Stage 1 bases in the Stage 4 bucket.
    FLAT_200 = 0.01                       # +/-1% over 20 bars counts as flat
    above50 = close > float(e50.iloc[-1])
    e50_up = rising(e50, 10)
    s200_slope_20 = s200_slope_60 = None
    if s200 is not None and not np.isnan(float(s200.iloc[-1])):
        s200_now = float(s200.iloc[-1])

        def _slope(lag):
            if len(s200) <= lag:
                return None
            prev = float(s200.iloc[-1 - lag])
            if np.isnan(prev) or prev == 0:
                return None
            return s200_now / prev - 1

        s200_slope_20, s200_slope_60 = _slope(20), _slope(60)
        if close > s200_now and ((s200_slope_20 or 0) >= FLAT_200
                                 or (above50 and (s200_slope_20 or 0) > -FLAT_200)):
            # Advance, Weinstein-strict (v6.4): price above the 200 AND the 200 is
            # either clearly rising, or flat with price above the 50 EMA. A name
            # above a still-FALLING 200 is no longer Stage 2 — it is a reclaim, i.e.
            # Stage 1 (or 3 if it is rolling over from an advance). That is what made
            # RKT invisible on its 4 Nov 2026 turn: five signals, but labelled Stage 2.
            # A multi-week pullback below the 50 above a rising 200 stays Stage 2 —
            # that is the pullback-to-confluence setup we trade.
            stage = 2
        elif s200_slope_20 is None:
            stage = 1
        elif close < s200_now and s200_slope_20 <= -FLAT_200:
            stage = 4                                        # decline
        else:
            # Base or top: above a flat 200 having lost the 50, or below a
            # rising 200 (the first crack). The 60-bar slope says which.
            stage = 3 if (s200_slope_60 is not None and s200_slope_60 > 0) else 1
    else:
        stage = 2 if (above50 and e50_up) else 1

    # ── AVWAP from last swing low (lowest low in last 120 days) ──
    look = df.iloc[-120:]
    anchor_idx = look["Low"].idxmin()
    seg = df.loc[anchor_idx:]
    tp = (seg["High"] + seg["Low"] + seg["Close"]) / 3
    avwap = float((tp * seg["Volume"]).sum() / seg["Volume"].sum()) if seg["Volume"].sum() > 0 else None
    days_since_anchor = int(len(seg) - 1)

    # ── Confluence: distance (in ATR) to each support layer ──
    levels = {
        "ema9":  float(e9.iloc[-1]),
        "ema21": float(e21.iloc[-1]),
        "ema50": float(e50.iloc[-1]),
        "ma10w": float(s50.iloc[-1]) if not np.isnan(float(s50.iloc[-1])) else None,
        "avwap": avwap,
    }
    dist_atr = {k: ((close - lv) / atr_now if lv else None) for k, lv in levels.items()}
    confluence = [k for k, d in dist_atr.items() if d is not None and abs(d) <= 1.0]

    # ── Trade plan: stop under the structure, entry zone = structure..price ──
    # Support = levels at or below price within 1.5 ATR (the zone a pullback is landing on).
    support = {k: lv for k, lv in levels.items() if lv and 0 <= (close - lv) / atr_now <= 1.5}
    if support:
        stop_basis = min(support, key=support.get)
        plan_stop = support[stop_basis] - 0.3 * atr_now
        plan_entry_lo = min(support.values())
        plan_entry_hi = close
    else:
        stop_basis = "atr"
        plan_stop = close - 1.5 * atr_now
        plan_entry_lo = plan_entry_hi = close
    plan_stop_pct = (close - plan_stop) / close * 100 if close else None

    # ── Behaviour ─────────────────────────────────────────
    vol5, vol20 = float(v.iloc[-5:].mean()), float(v.iloc[-20:].mean())
    vol_ratio = vol5 / vol20 if vol20 else None
    rvol = float(v.iloc[-1]) / vol20 if vol20 else None          # today's volume vs 20-day average

    last10 = df.iloc[-11:-1]
    down_vols = last10.loc[last10["Close"] < last10["Close"].shift(1).fillna(last10["Close"]), "Volume"]
    up_today = close > float(c.iloc[-2])
    # Suppressed only in a Stage 4 decline, where the same volume signature is
    # almost always a short-covering bounce (IONQ/RGTI/ARQQ/QBTS, nov 2026). In a
    # Stage 3 correction it marks the turn: TMO printed three of these on 27-29
    # May 2026, the exact reversal days.
    pocket_pivot = bool(stage != 4 and up_today and len(down_vols) > 0
                        and float(v.iloc[-1]) > float(down_vols.max()))

    rng3 = float(h.iloc[-3:].max() - l.iloc[-3:].min())
    tight = bool(rng3 / atr_now <= 1.5)

    # ── Early signs of a Stage 1 -> 2 transition (section 07 Notable) ──
    # rs_turn is added later in fetch_scan.py, once RS percentiles exist.
    e50_now = float(e50.iloc[-1])
    reclaim50 = bool(close > e50_now
                     and bool((c.iloc[-6:-1] < e50.iloc[-6:-1]).any())
                     and len(e50) > 10 and e50_now >= float(e50.iloc[-11]))
    # Weinstein's actual transition signal: the 200 has turned up over 20 bars
    # while it is still down over 60. A flat 200 on its own says nothing — it is
    # close to the definition of Stage 1, so it fired on almost every candidate.
    ma200_turn = bool(s200_slope_20 is not None and s200_slope_60 is not None
                      and s200_slope_20 > 0 and s200_slope_60 < 0)
    seg15 = df.iloc[-16:]
    d15, v15 = seg15["Close"].diff().iloc[1:], seg15["Volume"].iloc[1:]
    up_vol, dn_vol = float(v15[d15 > 0].sum()), float(v15[d15 < 0].sum())
    accum_ratio = up_vol / dn_vol if dn_vol > 0 else None
    higher_low = bool(len(l) >= 20 and float(l.iloc[-10:].min()) > float(l.iloc[-20:-10].min()))

    # -- Undercut & Reclaim (section 07, third route) ---------------------------
    # Iain Murphy's setup #2: a support level that has held for a while is
    # undercut intraday and reclaimed on the close, on heavy volume. The shakeout
    # flushes weak holders and the reclaim says the sellers are done; the stop
    # sits just under the undercut low, which is what earns this setup its own
    # gate -- a tight stop with a fast answer.
    # Measured on 394 liquid names over 120 sessions: a bare undercut+reclaim has
    # no edge (+0.3% median over 10 days vs +0.2% for the universe). Requiring the
    # support to have HELD for >= 5 bars and volume >= 2x lifts that to +2.9%
    # median / 62% win, and halves the stop-out rate when price is above the 200.
    # TYL printed one on 29 Oct 2026 (support 355.55 from 19 Oct, low 348.52,
    # close 373.93 on 2.53x volume) and ran +12.5% in the ten days after.
    # What matters is not how deep the dip went but WHERE THE REVERSAL ENDED: the
    # close of the U&R day must sit near the previous close. Above it is always
    # fine (a gap-down that fully reverses, XMTR +16.5% on 3 Nov 2026, is exactly
    # the kind of day worth watching), but a close far below means the reversal
    # left most of the gap open -- the level was jumped through, not tested, and
    # the chart is broken for weeks (WK -14.9%, IT -13.2%, RYTM -7.5%).
    # Note this is a tradability rule, not a performance one: on the sample the
    # names it drops showed a HIGHER raw forward return. They are excluded to keep
    # one recognisable pattern per list -- on the first live run 9 of 14 rows were
    # earnings repricings -- and because their swing entry comes on a later retest,
    # well outside this gate's five-session window.
    # The pattern also runs over TWO days: day 1 breaks the support and closes
    # BELOW it, day 2 closes back above on volume. Same five requirements, with
    # the reference for requirement 5 moved back one bar -- to the close before
    # day 1, where the damage started. Taking day 1's OPEN as the reference (the
    # obvious choice) would leave a gap on day 1 outside the measurement, so a
    # name could close neatly at day 1's open with the whole gap still open.
    # Rare: 4 hits in 120 sessions over 394 names, ~0.14/day on the full pool.
    # The same volume threshold separates signal from noise here as in the
    # one-day case -- at >= 2x: +4.4% median / 75% win over 10 days; relaxed to
    # 1.5x: 12 hits and -1.2% median, i.e. back to the universe baseline. Two
    # independent variants agreeing on that threshold is the strongest evidence
    # in this gate; n = 4 is still too small to call it proven.
    # Day 1's volume is typically BELOW average (median 0.93x) -- a breakdown
    # nobody sold into, which is the point.
    UR_LOOKBACK, UR_MIN_HELD, UR_MAX_UNDER = 30, 5, 0.07
    UR_VOL, UR_LOD, UR_MAX_AGE, UR_MAX_CLOSE_BELOW_PC = 2.0, 0.6, 5, -3.0
    ur = None
    if len(df) >= 40:
        lo_v, hi_v, cl_v, vo_v, e50_v = l.values, h.values, c.values, v.values, e50.values
        op_v = df["Open"].values
        v20_v = pd.Series(vo_v).rolling(20).mean().values

        def _support(upto, j_start):
            """Most recent low before j_start that then held as support until `upto`."""
            for j in range(j_start, max(upto - UR_LOOKBACK, 0) - 1, -1):
                if j + 1 >= upto:
                    continue
                ref = float(lo_v[j])
                if ref >= float(np.min(lo_v[j + 1:upto])):
                    continue                          # never acted as support
                return j, ref
            return None, None

        for i in range(max(len(df) - UR_MAX_AGE, UR_LOOKBACK + 2), len(df)):
            day_lo, day_hi, day_cl = float(lo_v[i]), float(hi_v[i]), float(cl_v[i])
            rng = day_hi - day_lo
            if rng <= 0 or np.isnan(v20_v[i]) or v20_v[i] <= 0:
                continue
            if (day_cl - day_lo) / rng < UR_LOD:      # close must be in the upper part
                continue
            vol_x = float(vo_v[i]) / float(v20_v[i])
            if vol_x < UR_VOL:
                continue
            found = None

            # -- one day: today undercuts the support and reclaims it on the close
            prev_cl = float(cl_v[i - 1])
            if (day_cl / prev_cl - 1) * 100 > UR_MAX_CLOSE_BELOW_PC:
                j, ref = _support(i, i - 1 - UR_MIN_HELD)
                if ref is not None and day_lo < ref < day_cl and (ref - day_lo) / ref <= UR_MAX_UNDER:
                    found = (1, ref, i - 1 - j, day_lo, prev_cl, None)

            # -- two days: day 1 closed below the support, today closes back above
            if found is None:
                k = i - 1
                base = float(cl_v[k - 1])
                if (day_cl / base - 1) * 100 > UR_MAX_CLOSE_BELOW_PC:
                    d1_lo, d1_cl = float(lo_v[k]), float(cl_v[k])
                    j, ref = _support(k, k - 1 - UR_MIN_HELD)
                    if (ref is not None and d1_lo < ref and (ref - d1_lo) / ref <= UR_MAX_UNDER
                            and d1_cl < ref < day_cl):
                        v1 = float(vo_v[k]) / float(v20_v[k]) if v20_v[k] else None
                        found = (2, ref, k - 1 - j, min(d1_lo, day_lo), base, v1)

            if found is not None:
                span, ref, held, stop_lo, base, v1 = found
                ur = {"date": str(df.index[i].date()), "span": span,
                      "ref": r2(ref), "low": r2(stop_lo),
                      "under_pct": r2((ref - stop_lo) / ref * 100, 2), "vol_x": r2(vol_x),
                      "vol_x_day1": r2(v1), "held": int(held), "age": int(len(df) - 1 - i),
                      # close_vs_pc / low_vs_pc are measured against the close the rule
                      # gated on: the previous close for a one-day U&R, the close before
                      # day 1 for a two-day one. gap_pct is always the reclaim day's gap.
                      "close_vs_pc": r2((day_cl / base - 1) * 100, 1),
                      "low_vs_pc": r2((stop_lo / base - 1) * 100, 1),
                      "gap_pct": r2((float(op_v[i]) / float(cl_v[i - 1]) - 1) * 100, 1),
                      "stop_dist_pct": r2((day_cl - stop_lo) / day_cl * 100, 1),
                      "at_ema50": bool(stop_lo < float(e50_v[i]) <= day_cl)}

    # -- Base breakout (v7, setup module) ---------------------------------------
    # Measured 2015-2026 on 1,810 liquid names (MEETRESULTATEN-T1-T2.md, T2):
    # a buy-stop on the pivot has a small edge over a random Stage-2 day
    # (+0.6% vs +0.4% median over 10 sessions) except under pressure (regime C:
    # 0.0%, 8 of 10 years <= 0). Correction (D) is not worse in the median but
    # 1 in 3 fail. The best entry measured was the RETEST in regime A (+1.3%,
    # 60% win). Quality filters (tightness, dry-up, RS >= 80) did not help, so
    # there are none here -- the regime label in the dashboard carries the risk.
    BO_WIN, BO_BASE_MIN, BO_MAX_DEPTH, BO_MAX_EXT, BO_VOL = 260, 25, 0.35, 0.05, 1.5
    SETUP_MAX_AGE = 5
    bo = None
    s200_v = s200.values if s200 is not None else None
    if s200_v is not None and len(df) >= 221:
        lo_b, hi_b, cl_b, vo_b = l.values, h.values, c.values, v.values
        s50_v = s50.values
        av50_b = pd.Series(vo_b).rolling(50).mean().shift(1).values

        def _bo_at(i, need_vol):
            k = i - 1
            if k - 20 < 0 or np.isnan(s200_v[k]) or np.isnan(s200_v[k - 20]) or np.isnan(s50_v[k]):
                return None
            if not (cl_b[k] > s50_v[k] > s200_v[k] and s200_v[k] > s200_v[k - 20]):
                return None
            # Full 260-bar window for a live signal; for the base COUNT further back
            # a shorter window (>= 120 bars) is allowed, so the count is not limited
            # to the last few months of the download.
            w_len = min(BO_WIN, i)
            if w_len < 120:
                return None
            win = hi_b[i - w_len:i]
            jmax = int(np.argmax(win)); pivot = float(win[jmax]); age_p = w_len - jmax
            if age_p < BO_BASE_MIN or not (cl_b[i] > pivot >= cl_b[k]):
                return None
            depth = 1 - float(lo_b[i - age_p:i].min()) / pivot
            if depth > BO_MAX_DEPTH or cl_b[i] > pivot * (1 + BO_MAX_EXT):
                return None
            vx = float(vo_b[i]) / float(av50_b[i]) if av50_b[i] and not np.isnan(av50_b[i]) else None
            if need_vol and (vx is None or vx < BO_VOL):
                return None
            return pivot, age_p, depth, vx

        # base count: breakouts (without the volume requirement) since the last
        # close below the 200 -- within the downloaded window
        start = 121
        last_below = start
        for i in range(start, len(df)):
            if not np.isnan(s200_v[i]) and cl_b[i] < s200_v[i]:
                last_below = i
        n_before = 0
        for i in range(last_below + 1, len(df)):
            hit = _bo_at(i, need_vol=False)
            if hit is None:
                continue
            n_before += 1
            if i < len(df) - SETUP_MAX_AGE:
                continue
            full = _bo_at(i, need_vol=True)
            if full is None:
                continue
            pivot, age_p, depth, vx = full
            after = range(i + 1, len(df))
            retest = any(lo_b[j] <= pivot * 1.02 and cl_b[j] > pivot for j in after)
            bo = {"date": str(df.index[i].date()), "pivot": r2(pivot),
                  "ext_pct": r2((cl_b[i] / pivot - 1) * 100, 1), "vol_x": r2(vx),
                  "base_wk": int(round(age_p / 5)), "depth_pct": r2(depth * 100, 0),
                  "base_n": n_before, "now_vs_pivot": r2((close / pivot - 1) * 100, 1),
                  "retest": bool(retest), "age": int(len(df) - 1 - i)}

    # -- Buyable gap-up (v7, Morales & Kacher p187-203) ---------------------------
    # Gap = open minus yesterday's HIGH >= 0.75 x ATR(40) of yesterday, on volume
    # >= 1.5x the 50-day average, from above the 50-day. Stop = low of the gap
    # day; "held" says whether that low still stands. Not yet measured (T4) --
    # data/setups_log.json records every new hit so it can be measured live.
    GU_ATR_K, GU_VOL, GU_CLOSE_POS = 0.75, 1.5, 0.5
    gu = None
    if len(df) >= 60:
        o_g, h_g, l_g, c_g, v_g = df["Open"].values, h.values, l.values, c.values, v.values
        prev_c = np.r_[np.nan, c_g[:-1]]
        tr_g = np.nanmax(np.vstack([h_g - l_g, np.abs(h_g - prev_c), np.abs(l_g - prev_c)]), axis=0)
        atr40_g = pd.Series(tr_g).rolling(40).mean().values
        av50_g = pd.Series(v_g).rolling(50).mean().values
        s50_g = s50.values
        for i in range(max(len(df) - SETUP_MAX_AGE, 51), len(df)):
            k = i - 1
            if np.isnan(atr40_g[k]) or np.isnan(av50_g[k]) or np.isnan(s50_g[k]) or av50_g[k] <= 0:
                continue
            gap = float(o_g[i] - h_g[k])
            if gap < GU_ATR_K * atr40_g[k] or v_g[i] < GU_VOL * av50_g[k] or not c_g[k] > s50_g[k]:
                continue
            low_g = float(l_g[i])
            held = bool(i == len(df) - 1 or float(np.min(l_g[i + 1:])) >= low_g)
            # v7.3: the gap must still be open at the CLOSE (close above yesterday's high)
            # and the close in the upper half of the day's range. RVMD 5 Nov 2026 opened
            # +4.7% (0.87 ATR) on 3.9x volume and closed on its low, -11% on the day: a
            # gap-up reversal, not a buyable gap. Rejected gaps are still logged (valid=False)
            # so T4 can measure whether this filter earns its place.
            rng_g = float(h_g[i] - l_g[i])
            close_pos = (float(c_g[i]) - float(l_g[i])) / rng_g if rng_g > 0 else 0.0
            open_at_close = bool(c_g[i] > h_g[k])
            hit = {"date": str(df.index[i].date()), "gap_pct": r2((o_g[i] / c_g[k] - 1) * 100, 1),
                   "gap_atr": r2(gap / atr40_g[k]), "vol_x": r2(v_g[i] / av50_g[k]),
                   "low": r2(low_g), "held": held,
                   "dist_low_pct": r2((close / low_g - 1) * 100, 1), "age": int(len(df) - 1 - i),
                   "close_pos": r2(close_pos), "open_at_close": open_at_close,
                   "close_vs_prev_high": r2((float(c_g[i]) / float(h_g[k]) - 1) * 100, 1),
                   "valid": bool(open_at_close and close_pos >= GU_CLOSE_POS)}
            if hit["valid"] or gu is None or not gu.get("valid"):
                gu = hit                                   # newest valid hit wins; else newest hit

    # -- Trend Template (Minervini p94), 7 of 8 here; RS >= 70 is set where RS exists
    tt_flags = None
    if s200 is not None and len(c) >= 252 and not np.isnan(float(s200.iloc[-1])):
        s150 = c.rolling(150).mean()
        s50n, s150n, s200n = float(s50.iloc[-1]), float(s150.iloc[-1]), float(s200.iloc[-1])
        lo52 = float(l.iloc[-252:].min())
        tt_flags = {
            "above_150_200": bool(close > s150n and close > s200n),
            "s150_gt_200":   bool(s150n > s200n),
            "s200_rising":   bool(s200n > float(s200.iloc[-22])),
            "s50_gt_150_200": bool(s50n > s150n and s50n > s200n),
            "above_50":      bool(close > s50n),
            "above_low_30":  bool(close >= 1.3 * lo52),
            "near_high_25":  bool(close >= 0.75 * hi52),
            "rs_70":         False,
        }

    hi20_pos = int(np.argmax(h.iloc[-20:].values))
    days_since_20d_high = 19 - hi20_pos

    # Weakness in strength: green on red SPY days (last 20 sessions)
    wis_count, wis_today = 0, False
    if spy_chg is not None:
        stock_chg = c.pct_change() * 100
        joined = pd.concat([stock_chg, spy_chg], axis=1, join="inner").dropna().iloc[-20:]
        if len(joined):
            red_spy = joined[joined.iloc[:, 1] < -0.5]
            wis_count = int((red_spy.iloc[:, 0] > 0).sum())
            last = joined.iloc[-1]
            wis_today = bool(last.iloc[1] < -0.5 and last.iloc[0] > 0)

    return {
        "ticker": ticker,
        "price": r2(close), "chg_1d": r2(chg_1d),
        # DNA
        "adr_pct": r2(adr_pct), "atr": r2(atr_now), "atr_pct": r2(atr_pct),
        "atr_mult_50": r2(atr_mult_50), "pct_from_50": r2(pct_from_50, 1),
        "off_52w_high": r2(off_52w_high, 1), "avg_dollar_vol": r2(avg_dollar_vol, 0),
        "lod_dist": r2(lod_dist, 0), "stage": stage,
        "above_s200": (bool(close > s200_now) if (s200 is not None and not np.isnan(float(s200.iloc[-1]))) else None),
        "s200_slope_20": r2(s200_slope_20, 4), "s200_slope_60": r2(s200_slope_60, 4),
        "rs_raw": rs_raw(c), "rs_rating": None,
        # structure
        "ema9": r2(levels["ema9"]), "ema21": r2(levels["ema21"]), "ema50": r2(levels["ema50"]),
        "ma10w": r2(levels["ma10w"]), "avwap": r2(avwap), "avwap_days": days_since_anchor,
        "ema9_rising": rising(e9, 3), "ema21_rising": rising(e21, 5), "ema50_rising": e50_up,
        "above_ema9": bool(close > levels["ema9"]), "above_ema21": bool(close > levels["ema21"]),
        "above_ema50": bool(above50),
        "dist_atr": {k: r2(d) for k, d in dist_atr.items()},
        "confluence": confluence, "confluence_n": len(confluence),
        # behaviour
        "vol_ratio_5_20": r2(vol_ratio), "rvol": r2(rvol), "pocket_pivot": pocket_pivot, "tight": tight,
        "accum_ratio": r2(accum_ratio),
        "ur": ur, "bo": bo, "gu": gu,
        "tt_flags": tt_flags, "tt": (sum(tt_flags.values()) if tt_flags else None),
        "early": {"reclaim50": reclaim50, "ma200_turn": ma200_turn, "pocket_pivot": pocket_pivot,
                  "accum": bool(accum_ratio is not None and accum_ratio >= 1.3),
                  "higher_low": higher_low, "rs_turn": False,
                  "ur": bool(ur)},
        # trade plan
        "plan_stop": r2(plan_stop), "plan_stop_pct": r2(plan_stop_pct), "plan_stop_basis": stop_basis,
        "plan_entry_lo": r2(plan_entry_lo), "plan_entry_hi": r2(plan_entry_hi),
        "days_since_20d_high": days_since_20d_high,
        "wis_count_20d": wis_count, "wis_today": wis_today,
    }


# ─────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────
def main():
    print("TRADERDECK — Swing Screener")
    from sector_map import exclude_dead, record_results
    universe, sources = load_universe()
    universe = exclude_dead(universe, label="universe")
    sp500 = load_sp500()
    bench_pool = sorted(set(sp500) | set(universe))
    tickers = sorted(set(bench_pool) | {"SPY"})
    print(f"  Universe: {len(universe)} tickers · RS benchmark pool: {len(bench_pool)}")

    # 480 days: same window as fetch_scan.py — a 252-bar RS window plus the
    # 21-bar offset for rs_rating_1m needs ~274 trading days.
    start = (dt.date.today() - dt.timedelta(days=730)).strftime("%Y-%m-%d")   # v7.1: same window as fetch_scan.py
    print(f"  Downloading {len(tickers)} tickers from {start}...", end=" ", flush=True)
    data = yf.download(tickers, start=start, interval="1d", group_by="ticker",
                       auto_adjust=True, progress=False, threads=True)
    print("OK")

    def frame(t):
        try:
            if t not in data.columns.get_level_values(0):
                return None
            return data[t].dropna(how="all")
        except Exception:
            return None

    spy = frame("SPY")
    spy_chg = spy["Close"].pct_change() * 100 if spy is not None else None
    record_results(tickers, [t for t in tickers if frame(t) is not None and len(frame(t)) >= 5])

    # RS raw for the whole benchmark pool (cheap: close series only), plus the
    # same thing on the series as it stood RS_LOOKBACK_1M trading days ago.
    rs_pool, rs_pool_1m = {}, {}
    for t in bench_pool:
        df = frame(t)
        if df is None or len(df) < 70:
            continue
        cl = df["Close"].dropna()
        r = rs_raw(cl)
        if r is not None:
            rs_pool[t] = r
        if len(cl) > RS_LOOKBACK_1M + 70:
            r1 = rs_raw(cl.iloc[:-RS_LOOKBACK_1M])
            if r1 is not None:
                rs_pool_1m[t] = r1
    pool_vals = np.array(sorted(rs_pool.values()))
    pool_vals_1m = np.array(sorted(rs_pool_1m.values()))

    def _pct(x, pool):
        if x is None or len(pool) == 0:
            return None
        return int(max(1, min(99, round(np.searchsorted(pool, x, side="right") / len(pool) * 99))))

    def percentile(x):
        return _pct(x, pool_vals)

    def rs_1m_of(df):
        cl = df["Close"].dropna()
        if len(cl) <= RS_LOOKBACK_1M + 70:
            return None
        return _pct(rs_raw(cl.iloc[:-RS_LOOKBACK_1M]), pool_vals_1m)

    try:
        from sector_map import load_earnings
        earnings = load_earnings()
    except Exception as e:
        print(f"  (earnings calendar unavailable: {e})"); earnings = {}

    rows = []
    for t in universe:
        df = frame(t)
        if df is None:
            print(f"  skip {t}: no data")
            continue
        try:
            m = analyse(t, df, spy_chg)
        except Exception as e:
            print(f"  skip {t}: {e}")
            continue
        if not m:
            continue
        m["rs_rating"] = percentile(m.pop("rs_raw"))
        m["rs_rating_1m"] = rs_1m_of(df)
        m["rs_chg_1m"] = (m["rs_rating"] - m["rs_rating_1m"]) \
            if (m["rs_rating"] is not None and m["rs_rating_1m"] is not None) else None
        set_tt_rs(m)
        er = earnings.get(t)
        m["earnings_date"] = er
        m["earnings_days"] = (dt.date.fromisoformat(er) - dt.date.today()).days if er else None

        # DNA pass/fail per filter
        checks = {
            "adr":   (m["adr_pct"] or 0) >= DNA["adr_pct_min"],
            "vol":   (m["avg_dollar_vol"] or 0) >= DNA["avg_dollar_vol_min"],
            "rs":    (m["rs_rating"] or 0) >= DNA["rs_rating_min"],
            "stage": m["stage"] == 2,
            "ema50": bool(m["above_ema50"]),
            "high":  abs(m["off_52w_high"] or 100) <= DNA["off_52w_high_max"],
            "ext":   (m["atr_mult_50"] if m["atr_mult_50"] is not None else 99) <= DNA["atr_mult_max"],
        }
        m["dna"] = checks
        m["dna_pass"] = all(checks.values())
        m["dna_score"] = sum(checks.values())
        am = m["atr_mult_50"] if m["atr_mult_50"] is not None else 0
        m["extension"] = "reduce" if am > EXT_REDUCE else "caution" if am > EXT_CAUTION else "ok"
        m["in_sr"] = t in sources["carnivore_sr"]
        m["in_lt"] = t in sources["carnivore_lt"]
        m["in_watchlist"] = t in sources["watchlist"]
        # Setup readiness: Stage 2 leader pulling into confluence on dry volume
        m["setup_score"] = (
            m["dna_score"]
            + m["confluence_n"]
            + (1 if (m["vol_ratio_5_20"] or 1) < 0.8 else 0)
            + (1 if m["tight"] else 0)
            + (1 if m["pocket_pivot"] else 0)
            + min(m["wis_count_20d"], 3)
        )
        rows.append(m)

    rows.sort(key=lambda x: (x["setup_score"], x["rs_rating"] or 0), reverse=True)

    # ── Regime: watchlist breadth ────────────────────────
    n = len(rows)
    strong = [r for r in rows if r["above_ema9"] and r["above_ema21"] and r["ema9_rising"] and r["ema21_rising"]]
    pct_strong = round(len(strong) / n * 100, 1) if n else None
    pct_above21 = round(sum(1 for r in rows if r["above_ema21"]) / n * 100, 1) if n else None
    pct_stage2 = round(sum(1 for r in rows if r["stage"] == 2) / n * 100, 1) if n else None
    regime = ("green" if pct_strong >= REGIME_GREEN else "yellow" if pct_strong >= REGIME_YELLOW else "red") if pct_strong is not None else None

    # SP500 reference with the same definition
    sp_strong, sp_n = 0, 0
    for t in sp500:
        df = frame(t)
        if df is None or len(df) < 30:
            continue
        c = df["Close"].dropna()
        e9, e21 = ema(c, 9), ema(c, 21)
        sp_n += 1
        if float(c.iloc[-1]) > float(e9.iloc[-1]) > 0 and float(c.iloc[-1]) > float(e21.iloc[-1]) \
           and float(e9.iloc[-1]) > float(e9.iloc[-4]) and float(e21.iloc[-1]) > float(e21.iloc[-6]):
            sp_strong += 1
    sp_pct_strong = round(sp_strong / sp_n * 100, 1) if sp_n else None

    # history (append today, keep 60)
    history = []
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH) as f:
                history = json.load(f).get("regime", {}).get("history", []) or []
        except Exception:
            history = []
    today = str(spy.index[-1].date()) if spy is not None and len(spy) else dt.date.today().isoformat()   # v7.7: bar date
    history = [h for h in history if h.get("date") < today and dt.date.fromisoformat(h["date"]).weekday() < 5]
    history.append({"date": today, "pct_strong": pct_strong, "sp500_pct_strong": sp_pct_strong, "regime": regime})
    history = history[-60:]

    spy_today = r2(float(spy_chg.iloc[-1]), 2) if spy_chg is not None else None

    payload = {
        "updated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "updated_date": today,
        "thresholds": {**DNA, "ext_caution": EXT_CAUTION, "ext_reduce": EXT_REDUCE,
                       "regime_green": REGIME_GREEN, "regime_yellow": REGIME_YELLOW},
        "regime": {
            "label": regime, "pct_strong": pct_strong, "pct_above_ema21": pct_above21,
            "pct_stage2": pct_stage2, "universe_n": n,
            "sp500_pct_strong": sp_pct_strong, "sp500_n": sp_n,
            "spy_chg_1d": spy_today,
            "wis_today": [r["ticker"] for r in rows if r["wis_today"]],
            "history": history,
        },
        "sources": sources,
        "universe": rows,
    }

    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)

    passed = [r["ticker"] for r in rows if r["dna_pass"]]
    print(f"  Regime: {regime} ({pct_strong}% strong; S&P {sp_pct_strong}%)")
    print(f"  DNA pass ({len(passed)}): {', '.join(passed) or '—'}")
    print(f"  Saved {OUTPUT_PATH} ({n} tickers)")


if __name__ == "__main__":
    main()
