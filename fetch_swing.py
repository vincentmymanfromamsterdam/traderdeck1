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
#  PERSONAL WATCHLIST  (edit freely — Carnivore tickers are added automatically)
# ─────────────────────────────────────────────────────────
WATCHLIST = [
    "SNDK", "DOCN", "RKLB",                 # Iain examples
    "NVDA", "AVGO", "MU", "AMD", "ARM",     # Semis
    "PLTR", "CRWD", "PANW", "NET",          # Software / cyber
    "HOOD", "COIN", "MSTR",                 # Fintech / crypto proxies
    "VRT", "GEV", "CEG",                    # Power / AI infra
    "LLY", "ISRG",                          # Health care leaders
    "ASTS", "LUNR",                         # Space
]

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
    sources = {"watchlist": list(WATCHLIST), "carnivore_sr": [], "carnivore_lt": []}
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
        if close > s200_now and (above50 or (s200_slope_20 or 0) >= FLAT_200):
            # Advance. A multi-week pullback below the 50 EMA while the 200 is
            # still clearly rising is a Stage 2 correction, not a top — that is
            # exactly the pullback-to-confluence setup we trade.
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
    # Only meaningful inside a base or an uptrend — in a Stage 4 decline the
    # same volume signature is almost always a short-covering bounce.
    pocket_pivot = bool(stage in (1, 2) and up_today and len(down_vols) > 0
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
        "early": {"reclaim50": reclaim50, "ma200_turn": ma200_turn, "pocket_pivot": pocket_pivot,
                  "accum": bool(accum_ratio is not None and accum_ratio >= 1.3),
                  "higher_low": higher_low, "rs_turn": False},
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

    start = (dt.date.today() - dt.timedelta(days=400)).strftime("%Y-%m-%d")
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

    # RS raw for the whole benchmark pool (cheap: close series only)
    rs_pool = {}
    for t in bench_pool:
        df = frame(t)
        if df is None or len(df) < 70:
            continue
        r = rs_raw(df["Close"].dropna())
        if r is not None:
            rs_pool[t] = r
    pool_vals = np.array(sorted(rs_pool.values()))

    def percentile(x):
        if x is None or len(pool_vals) == 0:
            return None
        return int(max(1, min(99, round(np.searchsorted(pool_vals, x, side="right") / len(pool_vals) * 99))))

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
    today = dt.date.today().isoformat()
    history = [h for h in history if h.get("date") != today]
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
