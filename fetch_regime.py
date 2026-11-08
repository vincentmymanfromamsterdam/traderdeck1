"""
TRADERDECK — Market regime (v7)
================================
Distribution days and follow-through days on the Nasdaq Composite, after
Morales & Kacher ("Trade Like an O'Neil Disciple", ch. 7) and O'Neil ("How to
Make Money in Stocks", ch. 9). Measured in MEETRESULTATEN-T1-T2.md (T1,
variant B). Reference implementation: boeken-distillatie/meet/regime.py.

The IBD status (data/ibd.json, scraped locally) stays the PRIMARY regime label
in the dashboard; this model is the supporting count, an early warning, and an
archive to calibrate against IBD later (`history`).

States: CORR (correction), UP (confirmed uptrend), PRES (under pressure / standby).
Model regime: A = UP and <= A_DAYS sessions after the FTD and <= 2 DDs; B = other UP;
C = PRES; D = CORR.

Output: data/regime.json
Run:    python fetch_regime.py
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

OUTPUT_PATH = "data/regime.json"
IBD_PATH    = "data/ibd.json"
START       = "2015-01-01"          # history the state machine needs to settle

DD_DOWN       = -0.001   # close <= -0.1% on higher volume
DD_FLAT       = 0.001    # ...or close < +0.1% in the lower quarter of the range
DD_FLAT_POS   = 0.25
DD_WIN        = 20       # sessions
DD_EXPIRE_UP  = 0.05     # a DD expires once the index closes 5% above it (IBD practice; assumption)
DD_STANDBY    = 5
CORR_NEEDS_50 = True     # PRES -> CORR needs a break of the standby low AND a close below the 50d
FTD_MIN_DAY   = 4
FTD_K         = 1.6      # threshold = 1.6 x mean |daily move| of the Nasdaq over 50 sessions
FTD_CLIP      = (0.01, 0.03)
FTD_LOW_DAYS  = 40       # a low under the rally low within 40 sessions = failed FTD
A_DAYS        = 25
CHOP_WIN, CHOP_FLAG = 60, 3
HISTORY_MAX   = 750


def dd_flags(x):
    r = x["Close"].pct_change()
    v_up = x["Volume"] > x["Volume"].shift(1)
    rng = (x["High"] - x["Low"]).replace(0, np.nan)
    pos = (x["Close"] - x["Low"]) / rng
    return (v_up & ((r <= DD_DOWN) | ((r < DD_FLAT) & (pos <= DD_FLAT_POS)))).fillna(False)


def dd_live(x, dd):
    """Per session: which DDs of the last DD_WIN sessions are still counting."""
    C = x["Close"].values
    idx = np.where(dd.values)[0]
    live = []
    for i in range(len(x)):
        js = idx[(idx > i - DD_WIN) & (idx <= i)]
        live.append([int(j) for j in js
                     if C[j + 1:i + 1].max(initial=0) < C[j] * (1 + DD_EXPIRE_UP)])
    return live


def run_model(q, s):
    q = q[q["Volume"] > 0].copy()
    s = s.reindex(q.index)
    rq, rs = q["Close"].pct_change(), s["Close"].pct_change()
    vq = q["Volume"] > q["Volume"].shift(1)
    vs = s["Volume"] > s["Volume"].shift(1)
    thr = (rq.abs().rolling(50).mean() * FTD_K).clip(lower=FTD_CLIP[0], upper=FTD_CLIP[1])
    dd = dd_flags(q)
    live = dd_live(q, dd)
    dd20 = np.array([len(x) for x in live], dtype=float)
    sma50 = q["Close"].rolling(50).mean().values
    H, L, C = q["High"].values, q["Low"].values, q["Close"].values

    state, ftd_i, ftd_low = "CORR", None, None
    low_c, rally_d, rally_low, rally_start = np.inf, 0, None, None
    stby_low = stby_high = None
    rows, ftds = [], []
    for i in range(len(q)):
        if i < 60:
            rows.append(("CORR", 0, None, 0, None)); continue
        if state == "CORR":
            if L[i] < low_c:
                low_c = L[i]
                pos = (C[i] - L[i]) / (H[i] - L[i]) if H[i] > L[i] else 0
                if pos >= 0.5:
                    rally_d, rally_low, rally_start = 1, L[i], i
                else:
                    rally_d, rally_low, rally_start = 0, None, None
            elif rally_d == 0 and C[i] > C[i - 1]:
                rally_d, rally_low, rally_start = 1, low_c, i
            elif rally_d >= 1:
                if L[i] < rally_low:
                    rally_d, rally_low, rally_start = 0, None, None
                else:
                    rally_d += 1
            if rally_d >= FTD_MIN_DAY and ((rq.iloc[i] >= thr.iloc[i] and vq.iloc[i]) or
                                           (rs.iloc[i] >= thr.iloc[i] and vs.iloc[i])):
                state, ftd_i, ftd_low = "UP", i, rally_low
                ftds.append({"i": i, "pct": float(max(rq.iloc[i], rs.iloc[i]) * 100),
                             "thr": float(thr.iloc[i] * 100), "failed_i": None})
        elif state == "UP":
            if ftd_low is not None and L[i] < ftd_low:
                state, low_c, rally_d, rally_low, rally_start = "CORR", L[i], 0, None, None
                if ftds and ftds[-1]["i"] == ftd_i:
                    ftds[-1]["failed_i"] = i
            elif dd20[i] >= DD_STANDBY:
                state, stby_low, stby_high = "PRES", L[i], H[i]
            if state == "UP" and i - ftd_i > FTD_LOW_DAYS:
                ftd_low = None
        elif state == "PRES":
            if L[i] < stby_low and (not CORR_NEEDS_50 or C[i] < sma50[i]):
                state, low_c, rally_d, rally_low, rally_start, ftd_low = "CORR", L[i], 0, None, None, None
            elif H[i] > stby_high and dd20[i] < DD_STANDBY:
                state = "UP"
        days = (i - ftd_i) if (state == "UP" and ftd_i is not None) else None
        rows.append((state, int(dd20[i]), days, rally_d if state == "CORR" else 0,
                     rally_low if state == "CORR" else None))

    df = pd.DataFrame(rows, index=q.index, columns=["state", "dd20", "days_ftd", "rally_day", "rally_low"])

    def reg(r):
        if r.state == "CORR":
            return "D"
        if r.state == "PRES":
            return "C"
        if r.days_ftd is not None and r.days_ftd <= A_DAYS and r.dd20 <= 2:
            return "A"
        return "B"
    df["regime"] = df.apply(reg, axis=1)
    return df, q, dd, live, thr, ftds, (stby_low, stby_high)


def vs_ma(close, n):
    ma = close.rolling(n).mean().iloc[-1]
    return round((float(close.iloc[-1]) / float(ma) - 1) * 100, 1) if ma and not np.isnan(ma) else None


def next_sessions(d, n):
    """The n-th weekday after date d (holidays not handled)."""
    out = d
    while n > 0:
        out += dt.timedelta(days=1)
        if out.weekday() < 5:
            n -= 1
    return out


def main():
    print("TRADERDECK — Market regime")
    raw = yf.download(["^IXIC", "^GSPC", "^RUT"], start=START, interval="1d",
                      group_by="ticker", auto_adjust=True, progress=False, threads=True)
    q = raw["^IXIC"].dropna(subset=["Close"])
    s = raw["^GSPC"].dropna(subset=["Close"])
    r = raw["^RUT"].dropna(subset=["Close"])
    # A session still in progress (Yahoo returns today's partial bar) is dropped
    # when the run happens before the US close (21:00 UTC is safe all year).
    now = dt.datetime.now(dt.timezone.utc)
    if now.hour < 21:
        today = pd.Timestamp(now.date())
        q, s, r = [x[x.index < today] for x in (q, s, r)]
    df, qv, dd, live, thr, ftds, stby = run_model(q, s)
    last = df.iloc[-1]
    idx = df.index
    asof = idx[-1].date()

    since = idx[-1]
    for j in range(len(df) - 1, -1, -1):
        if df["regime"].iloc[j] != last.regime:
            break
        since = idx[j]

    ddq_live = live[-1]
    dd_s = dd_flags(s[s["Volume"] > 0])
    s_live = dd_live(s[s["Volume"] > 0], dd_s)[-1]
    s_idx = s[s["Volume"] > 0].index

    last20 = [{"d": str(idx[k].date()), "dd": bool(k in ddq_live)} for k in range(len(df) - 20, len(df))]

    ftd_out = None
    if ftds:
        f = ftds[-1]
        ftd_out = {"last": str(idx[f["i"]].date()), "pct": round(f["pct"], 2), "thr": round(f["thr"], 2),
                   "status": "failed" if f["failed_i"] is not None else ("active" if last.state != "CORR" else "ended"),
                   "failed_on": str(idx[f["failed_i"]].date()) if f["failed_i"] is not None else None,
                   "days_since": int(last.days_ftd) if last.days_ftd is not None and not pd.isna(last.days_ftd) else None}

    rally = None
    if last.state == "CORR" and last.rally_day:
        start_i = len(df) - int(last.rally_day)
        rally = {"day": int(last.rally_day), "start": str(idx[start_i].date()),
                 "low": round(float(last.rally_low), 2),
                 "ftd_from": str(next_sessions(asof, max(0, FTD_MIN_DAY - int(last.rally_day)))) if last.rally_day < FTD_MIN_DAY else str(next_sessions(asof, 1)),
                 "ftd_thr": round(float(thr.iloc[-1]) * 100, 2)}

    flips = int((df["state"] != df["state"].shift()).iloc[-CHOP_WIN:].sum())

    ratio = r["Close"] / s["Close"].reindex(r.index)
    ratio = ratio.dropna()
    russell = {"close": round(float(r["Close"].iloc[-1]), 2), "vs50": vs_ma(r["Close"], 50),
               "vs200": vs_ma(r["Close"], 200),
               "ratio_vs50": bool(ratio.iloc[-1] > ratio.rolling(50).mean().iloc[-1]),
               "ratio_chg20": round((float(ratio.iloc[-1]) / float(ratio.iloc[-21]) - 1) * 100, 1)}

    out = {
        "updated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "asof": str(asof),
        "model": {"state": last.state, "regime": last.regime, "since": str(since.date())},
        "nasdaq": {"close": round(float(q["Close"].iloc[-1]), 2), "chg": round(float(q["Close"].pct_change().iloc[-1]) * 100, 2),
                   "vs50": vs_ma(q["Close"], 50), "vs200": vs_ma(q["Close"], 200),
                   "dd20": int(last.dd20), "dd_dates": [str(idx[k].date()) for k in ddq_live], "last20": last20},
        "sp500": {"close": round(float(s["Close"].iloc[-1]), 2), "chg": round(float(s["Close"].pct_change().iloc[-1]) * 100, 2),
                  "vs50": vs_ma(s["Close"], 50), "vs200": vs_ma(s["Close"], 200),
                  "dd20": len(s_live), "dd_dates": [str(s_idx[k].date()) for k in s_live]},
        "russell": russell,
        "rally": rally,
        "ftd": ftd_out,
        "standby": {"low": round(float(stby[0]), 2), "high": round(float(stby[1]), 2)} if last.state == "PRES" else None,
        "chop": {"flips60": flips, "flag": flips >= CHOP_FLAG},
        "params": {"dd_win": DD_WIN, "dd_standby": DD_STANDBY, "dd_expire_up": DD_EXPIRE_UP,
                   "ftd_min_day": FTD_MIN_DAY, "a_days": A_DAYS},
    }

    # Calibration archive: own model next to the IBD status, one row per date.
    hist = []
    try:
        with open(OUTPUT_PATH) as f:
            hist = json.load(f).get("history", [])
    except Exception:
        pass
    ibd_status = None
    try:
        with open(IBD_PATH) as f:
            ibd = json.load(f)
        ibd_status = ibd.get("outlook") if ibd.get("ok", True) else None
    except Exception:
        pass
    hist = [h for h in hist if h.get("date") != str(asof)]
    hist.append({"date": str(asof), "model": last.regime, "state": last.state,
                 "ibd": ibd_status, "dd20": int(last.dd20)})
    out["history"] = sorted(hist, key=lambda h: h["date"])[-HISTORY_MAX:]

    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(out, f, indent=1)
    print(f"  {out['asof']}: model {last.regime} ({last.state}) since {out['model']['since']} · "
          f"DD {int(last.dd20)}/{DD_STANDBY} · chop {flips} · IBD {ibd_status}")


if __name__ == "__main__":
    main()
