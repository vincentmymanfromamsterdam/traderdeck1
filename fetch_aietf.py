"""
TRADERDECK — AI-sector ETFs (v7.6)
==================================
Nine AI-sector ETFs (after a watchlist method by @RealJGBanks, 10 Nov 2026):
  1. weekly/monthly trend of the ETF itself — above/below the 10- and 40-week average,
     a fresh 3-month low in the last 20 sessions;
  2. its top-10 holdings — which held up best while the ETF sold off
     (return since the ETF's swing high of the last 60 sessions, minus the ETF's own);
  3. the dashboard links each holding to a chart and the watchlist.

Holdings come from Yahoo (funds_data.top_holdings) and are refreshed once a week;
non-US listings (a suffix like .KQ/.SW/.T) are kept with their weight only.
RS / Stage / TT are joined in the dashboard from market_scan.json.

Output: data/ai_etfs.json
Run:    python fetch_aietf.py
"""

import datetime as dt
import json
import os
import sys
import time

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

OUTPUT_PATH = "data/ai_etfs.json"
ETFS = [  # (ticker, label, group) — the nine from the post, in its order of groups
    ("SMH", "Semiconductors", "Chips & data centers"),
    ("DRAM", "Memory", "Chips & data centers"),
    ("DTCR", "Data centers", "Chips & data centers"),
    ("IGV", "Software", "Software & automation"),
    ("CIBR", "Cybersecurity", "Software & automation"),
    ("BOTZ", "Robotics", "Software & automation"),
    ("NLR", "Nuclear energy", "Power & compute"),
    ("GRID", "Power grid", "Power & compute"),
    ("WGMI", "Bitcoin mining & compute", "Power & compute"),
]
HOLDINGS_MAX_AGE_DAYS = 7
SWING_WIN = 60            # ETF swing high = highest close of the last 60 sessions
NEW_LOW_WIN, NEW_LOW_REF = 20, 63   # a 3-month low set in the last 20 sessions


def load_prev():
    try:
        with open(OUTPUT_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def is_cash(sym):
    return len(sym) == 5 and sym.endswith("XX")        # money-market funds (FGXXX)


def is_us(sym):
    return "." not in sym and sym.isalpha() and len(sym) <= 5 and not is_cash(sym)


def get_holdings(prev):
    """{etf: [{ticker, name, weight}]} — reuse last week's list while it is fresh."""
    old = {e["ticker"]: e.get("holdings") for e in prev.get("etfs", [])}
    when = prev.get("holdings_updated")
    fresh = when and (dt.date.today() - dt.date.fromisoformat(when)).days < HOLDINGS_MAX_AGE_DAYS
    out, refreshed = {}, False
    for tk, _, _ in ETFS:
        if fresh and old.get(tk):
            out[tk] = old[tk]
            continue
        rows = None
        for k in range(3):
            try:
                th = yf.Ticker(tk).funds_data.top_holdings
                rows = [{"ticker": str(sym).upper(), "name": str(r.get("Name") or "")[:40],
                         "weight": round(float(r.get("Holding Percent") or 0) * 100, 2)}
                        for sym, r in th.iterrows()]
                break
            except Exception as e:
                print(f"  holdings {tk}: {e} (try {k + 1})")
                time.sleep(5 * (k + 1))
        if rows:
            out[tk] = rows
            refreshed = True
        else:
            out[tk] = [dict(h, weight=h.get("weight")) for h in (old.get(tk) or [])]
    return out, (str(dt.date.today()) if refreshed else when)


def pct(a, b):
    return None if b in (0, None) or a is None or pd.isna(a) or pd.isna(b) else round((float(a) / float(b) - 1) * 100, 2)


def etf_metrics(df):
    c, lo = df["Close"], df["Low"]
    last = float(c.iloc[-1])
    wk = c.resample("W-FRI").last().dropna()
    ma10, ma40 = wk.rolling(10).mean().iloc[-1], wk.rolling(40).mean().iloc[-1]
    mo = c.resample("ME").last().dropna()
    ma10m = mo.rolling(10).mean().iloc[-1] if len(mo) >= 10 else np.nan
    y0 = c[c.index.year == c.index[-1].year]
    ref_low = lo.iloc[-NEW_LOW_REF:].min()
    new_low = bool(lo.iloc[-NEW_LOW_WIN:].min() <= ref_low)
    win = c.iloc[-SWING_WIN:]
    h_ts = win.idxmax()
    return {
        "price": round(last, 2),
        "chg_1d": pct(c.iloc[-1], c.iloc[-2]),
        "chg_1w": pct(c.iloc[-1], c.iloc[-6]) if len(c) > 6 else None,
        "chg_1m": pct(c.iloc[-1], c.iloc[-22]) if len(c) > 22 else None,
        "chg_ytd": pct(c.iloc[-1], y0.iloc[0]) if len(y0) else None,
        "off_52w_high": pct(c.iloc[-1], c.iloc[-252:].max()),
        "above_10w": None if pd.isna(ma10) else bool(last > ma10),
        "above_40w": None if pd.isna(ma40) else bool(last > ma40),
        "above_10m": None if pd.isna(ma10m) else bool(last > ma10m),
        "ma10w_slope_up": bool(wk.rolling(10).mean().iloc[-1] > wk.rolling(10).mean().iloc[-2]) if len(wk) > 11 else None,
        "new_low_20": new_low,
        "swing_high": {"date": str(h_ts.date()), "close": round(float(win.max()), 2),
                       "since_pct": pct(c.iloc[-1], win.max())},
        "spark": [round(float(x), 2) for x in c.iloc[-60:].tolist()],
    }


def main():
    print("TRADERDECK — AI-sector ETFs")
    prev = load_prev()
    holdings, h_when = get_holdings(prev)
    us = sorted({h["ticker"] for hs in holdings.values() for h in hs if is_us(h["ticker"])})
    tickers = [e[0] for e in ETFS] + [t for t in us if t not in {e[0] for e in ETFS}]
    start = (dt.date.today() - dt.timedelta(days=420)).strftime("%Y-%m-%d")
    raw = None
    for k in range(3):
        try:
            raw = yf.download(tickers, start=start, interval="1d", group_by="ticker",
                              auto_adjust=True, progress=False, threads=True)
            break
        except Exception as e:
            print(f"  download failed: {e} (try {k + 1})"); time.sleep(10 * (k + 1))
    if raw is None:
        print("  no data — keeping the previous file"); return
    now = dt.datetime.now(dt.timezone.utc)

    def series(t):
        try:
            x = raw[t].dropna(subset=["Close"])
        except Exception:
            return None
        if now.hour < 21:                         # drop a session still in progress
            x = x[x.index < pd.Timestamp(now.date())]
        return x if len(x) > 30 else None

    out_etfs = []
    for tk, label, group in ETFS:
        df = series(tk)
        e = {"ticker": tk, "label": label, "group": group, "holdings": holdings.get(tk, [])}
        if df is None:
            out_etfs.append(e); continue
        e.update(etf_metrics(df))
        h_date = pd.Timestamp(e["swing_high"]["date"])
        rows = []
        for h in e["holdings"]:
            r = {"ticker": h["ticker"], "name": h.get("name"), "weight": h.get("weight"), "us": is_us(h["ticker"]),
                 "cash": is_cash(h["ticker"])}
            hd = series(h["ticker"]) if r["us"] else None
            if hd is not None:
                c = hd["Close"]
                base = c[c.index <= h_date]
                since = pct(c.iloc[-1], base.iloc[-1]) if len(base) else None
                r.update({"price": round(float(c.iloc[-1]), 2), "chg_1d": pct(c.iloc[-1], c.iloc[-2]),
                          "off_52w_high": pct(c.iloc[-1], c.iloc[-252:].max()),
                          "since_etf_high": since,
                          "vs_etf": None if since is None or e["swing_high"]["since_pct"] is None
                          else round(since - e["swing_high"]["since_pct"], 2),
                          "above_50d": bool(c.iloc[-1] > c.rolling(50).mean().iloc[-1])})
            rows.append(r)
        rows.sort(key=lambda r: (r.get("vs_etf") is None, -(r.get("vs_etf") or 0)))
        e["holdings"] = rows
        out_etfs.append(e)

    out = {"updated_at": now.strftime("%Y-%m-%d %H:%M UTC"), "holdings_updated": h_when,
           "params": {"swing_win": SWING_WIN, "new_low_win": NEW_LOW_WIN, "new_low_ref": NEW_LOW_REF,
                      "holdings_max_age_days": HOLDINGS_MAX_AGE_DAYS},
           "etfs": out_etfs}
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"  Saved {OUTPUT_PATH}: " + ", ".join(
        f"{e['ticker']} {len(e['holdings'])}h" + (" LOW" if e.get("new_low_20") else "") for e in out_etfs))


if __name__ == "__main__":
    main()
