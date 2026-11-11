"""
TRADERDECK — beslislog doorrekenen (v7.8)
=========================================
`data/decisions.json` wordt door het dashboard geschreven: per signaal NEEM of SKIP.
Dit script raakt dat bestand NOOIT aan. Het leest het, rekent elke beslissing door
met Vincents eigen regels, en schrijft het resultaat naar `data/decisions_result.json`.

Regels (gelijk aan de nulmeting, zodat beide vergelijkbaar zijn):
  entry  NEEM  = de entry die je zelf vastlegde, vanaf de sessie na de beslissing;
                 gevuld als de koers die entry raakt binnen ENTRY_VALID sessies.
         SKIP  = het plan uit het dashboard (plan_entry / plan_stop), zodat je ziet
                 wat de naam deed die je liet lopen.
  stop   onder de structuur, zoals vastgelegd.
  trim   TRIM_FRAC van de positie bij +TRIM_R, de rest loopt door.
  trail  slot onder de EMA21, actief zodra de trade +1R in de winst stond.
  cap    MAX_HOLD sessies.

Uitvoer per beslissing: status (wacht / open / closed), R, aantal bars, exit-reden,
MFE en MAE. Het dashboard zet jouw genomen trades naast de overgeslagen namen.

Run: python fetch_decisions.py
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

IN_PATH = "data/decisions.json"
OUT_PATH = "data/decisions_result.json"

TRIM_R, TRIM_FRAC = 3.0, 0.125
ARM_R = 1.0          # trail pas actief na +1R
MAX_HOLD = 120
ENTRY_VALID = 3
COST_BPS = 10


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def simulate(bars, d0, entry_ref, stop):
    """bars = DataFrame vanaf de beslissingsdag. d0 = index van de beslissingsdag."""
    o, h, l, c = (bars[k].values.astype(float) for k in ("Open", "High", "Low", "Close"))
    e21 = ema(pd.Series(c), 21).values
    n = len(c)
    if d0 + 1 >= n:
        return {"status": "wacht"}
    ent_i = None
    for j in range(d0 + 1, min(d0 + 1 + ENTRY_VALID, n)):
        if h[j] >= entry_ref:
            ent_i, entry = j, max(entry_ref, o[j])
            break
    if ent_i is None:
        return {"status": "wacht" if n - 1 - d0 < ENTRY_VALID else "geen entry"}
    if stop >= entry:
        return {"status": "ongeldig"}
    R = entry - stop
    trimmed, armed = False, ARM_R <= 0
    pos, realised, mfe, mae = 1.0, 0.0, 0.0, 0.0
    exit_i = exit_px = None
    reason = "open"
    for j in range(ent_i, min(ent_i + MAX_HOLD, n)):
        mfe = max(mfe, (h[j] - entry) / R)
        mae = min(mae, (l[j] - entry) / R)
        if l[j] <= stop:
            exit_i, exit_px, reason = j, (min(o[j], stop) if o[j] < stop else stop), "stop"
            break
        if not armed and h[j] >= entry + ARM_R * R:
            armed = True
        if not trimmed and h[j] >= entry + TRIM_R * R:
            realised += TRIM_FRAC * TRIM_R
            pos -= TRIM_FRAC
            trimmed = True
        if armed and j > ent_i and c[j] < e21[j]:
            if j + 1 < n:
                exit_i, exit_px, reason = j + 1, o[j + 1], "trail"
            else:
                exit_i, exit_px, reason = j, c[j], "trail"
            break
    closed = exit_i is not None
    if not closed:
        if ent_i + MAX_HOLD <= n - 1:
            exit_i, exit_px, reason, closed = ent_i + MAX_HOLD, c[ent_i + MAX_HOLD], "tijd", True
        else:
            exit_i, exit_px = n - 1, c[n - 1]
    cost = entry * COST_BPS / 10000 + exit_px * COST_BPS / 10000
    r = realised + pos * (exit_px - entry - cost) / R
    return {"status": "closed" if closed else "open", "entry": round(entry, 2),
            "entry_date": str(bars.index[ent_i].date()), "exit": round(float(exit_px), 2),
            "r": round(float(r), 2), "bars": int(exit_i - ent_i), "reason": reason,
            "mfe": round(float(mfe), 2), "mae": round(float(mae), 2), "trimmed": trimmed}


def main():
    print("TRADERDECK — beslislog")
    try:
        with open(IN_PATH) as f:
            rows = json.load(f).get("rows", [])
    except Exception as e:
        print(f"  geen beslislog ({e}) — niets te doen")
        return
    rows = [r for r in rows if r.get("ticker") and r.get("date")]
    if not rows:
        print("  beslislog is leeg")
        return
    tickers = sorted({r["ticker"] for r in rows})
    first = min(r["date"] for r in rows)
    start = (dt.date.fromisoformat(first) - dt.timedelta(days=60)).isoformat()
    print(f"  {len(rows)} beslissingen, {len(tickers)} namen vanaf {first}")
    raw = None
    for k in range(3):
        try:
            raw = yf.download(tickers, start=start, interval="1d", group_by="ticker",
                              auto_adjust=True, progress=False, threads=True)
            break
        except Exception as e:
            print(f"  download mislukt: {e} (poging {k + 1})")
            time.sleep(10 * (k + 1))
    if raw is None:
        print("  geen koersdata — vorige uitvoer blijft staan")
        return
    now = dt.datetime.now(dt.timezone.utc)

    def frame(t):
        try:
            x = raw[t].dropna(subset=["Close"]) if len(tickers) > 1 else raw.dropna(subset=["Close"])
        except Exception:
            return None
        if now.hour < 21:
            x = x[x.index < pd.Timestamp(now.date())]
        return x if len(x) > 25 else None

    out = []
    for r in rows:
        df = frame(r["ticker"])
        res = {"id": r["id"], "ticker": r["ticker"], "setup": r.get("setup"), "date": r["date"],
               "action": r.get("action"), "regime": r.get("regime"), "status": "geen data"}
        if df is not None:
            d0 = int(df.index.searchsorted(pd.Timestamp(r["date"])))
            if d0 < len(df):
                ent = r.get("entry") if r.get("action") == "take" else r.get("plan_entry")
                stp = r.get("stop") if r.get("action") == "take" else r.get("plan_stop")
                if ent and stp:
                    res.update(simulate(df, d0, float(ent), float(stp)))
        out.append(res)

    done = [x for x in out if x.get("status") == "closed"]
    takes = [x for x in done if x.get("action") == "take"]
    skips = [x for x in done if x.get("action") == "skip"]
    summary = {
        "n": len(out), "closed": len(done),
        "take_n": len(takes), "take_r": round(float(np.mean([x["r"] for x in takes])), 2) if takes else None,
        "take_win": round(sum(1 for x in takes if x["r"] > 0) / len(takes) * 100) if takes else None,
        "skip_n": len(skips), "skip_r": round(float(np.mean([x["r"] for x in skips])), 2) if skips else None,
        "skip_win": round(sum(1 for x in skips if x["r"] > 0) / len(skips) * 100) if skips else None,
    }
    os.makedirs("data", exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump({"updated_at": now.strftime("%Y-%m-%d %H:%M UTC"), "summary": summary, "rows": out},
                  f, separators=(",", ":"))
    print(f"  Saved {OUT_PATH}: {summary['closed']}/{summary['n']} afgerond · "
          f"genomen {summary['take_n']} ({summary['take_r']}R) · overgeslagen {summary['skip_n']} ({summary['skip_r']}R)")


if __name__ == "__main__":
    main()
