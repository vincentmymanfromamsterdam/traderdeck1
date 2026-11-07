"""
TRADERDECK — Full Market Scan
==============================
Scans every US-listed common stock (NASDAQ + NYSE + AMEX) through the same
Stock DNA / confluence / behaviour analysis as fetch_swing.py, and keeps the
leaders that are NOT already in the Carnivore + watchlist universe.

Pipeline (to keep the nightly run under ~15 minutes):
  1. Symbol directory + sector + industry + market cap from the Nasdaq screener (one call)
  2. Drop ETFs, warrants, units, rights, preferreds, SPAC shells; market cap >= $1B
  3. Quick liquidity pass on 60 days of data: avg $ volume >= 20M, price >= 5
  4. Full 400-day analysis on the survivors (~1,000-1,500 names)
  5. Save the top 150 by setup score + everything that passes all DNA filters

Output: data/market_scan.json
Run:    python fetch_scan.py
"""

import datetime as dt
import json
import os
import sys
import time

import numpy as np
import pandas as pd
import yfinance as yf

from fetch_swing import (
    DNA, EXT_CAUTION, EXT_REDUCE, analyse, ema, load_universe, load_sp500, rs_raw,
)
from sector_map import load_directory, yf_fallback, SECTOR_ETF, exclude_dead, record_results, load_earnings

INDUSTRY_HISTORY_PATH = "data/industry_history.json"

MIN_MARKET_CAP = 1_000_000_000   # pre-filter before downloading (strategy hard filter is $2B; shown per row)

OUTPUT_PATH = "data/market_scan.json"
CACHE_DIR   = ".scan_cache"          # resumable download cache (git-ignored)
TIME_BUDGET = float(os.environ.get("SCAN_TIME_BUDGET", "0") or 0)   # seconds; 0 = unlimited
_T0 = time.time()


class OutOfTime(Exception):
    pass


def budget_check():
    if TIME_BUDGET and time.time() - _T0 > TIME_BUDGET:
        raise OutOfTime()

MIN_PRICE          = 5.0
PREFILTER_VOL_USD  = 20_000_000     # quick pass; the DNA filter itself is 50M
KEEP_TOP           = 150
BATCH              = 150

THEMES_PATH        = "data/themes.json"        # hand-curated theme baskets (edit freely)
THEME_DATA_PATH    = "data/themes_data.json"
THEME_HISTORY_PATH = "data/theme_history.json"
THEME_CHART_TOP    = 5                          # per theme: top-N by RS get chart data

HISTORY_DAYS   = 480      # calendar days of daily history to download
RS_LOOKBACK_1M = 21       # trading days back for the "RS a month ago" percentile
RS_TURN_MIN    = 15       # rs_chg_1m needed for the rs_turn signal
NOTABLE_PATH   = "data/notable.json"
NOTABLE_MIN_SIGNALS = 1   # of 6 — one real signal makes a name notable; the score ranks them
NOTABLE_MAX     = 20
NOTABLE_MIN_DOLLAR_VOL = 50_000_000
NOTABLE_MIN_ABOVE_LOW  = 1.05    # dead-cat guard only. 25% cut the wrong names:
                                 # a quiet consolidation sits close to BOTH its 52w low
                                 # and high, so distance-from-low says nothing about quality.

CHART_PATH  = "data/chart_data.json"
CHART_MAX   = 40          # max DNA passers with chart data
CHART_BARS  = 130         # ~6 months of daily bars
CHART_META  = ("ticker", "name", "industry", "rs_rating", "rs_chg_1m", "stage", "adr_pct", "atr_mult_50", "off_52w_high",
               "setup_score", "rvol", "earnings_date", "earnings_days", "plan_stop", "plan_stop_pct",
               "plan_stop_basis", "plan_entry_lo", "plan_entry_hi", "dna", "confluence", "pocket_pivot", "tight",
               "in_universe")

SA_NEWS_PATH    = "data/sa_news.json"
SA_FEED         = "https://seekingalpha.com/api/sa/combined/{}.xml"   # public per-symbol RSS, no login
SA_NEWS_PER_TK  = 8        # headlines kept per ticker
SA_NEWS_WORKERS = 8        # parallel feed requests
SA_NEWS_MAX_TK  = 250      # safety cap only — must stay above CHART_MAX + themes * THEME_CHART_TOP
SA_NEWS_TIMEOUT = 15


def chart_bars(df):
    """OHLCV + EMA9/21/50, 10-week MA, AVWAP from the 120-day swing low, RVOL — for the chart cards."""
    x = df.dropna(subset=["Close"])
    c = x["Close"]
    e9, e21, e50 = (c.ewm(span=n, adjust=False).mean() for n in (9, 21, 50))
    ma10w = c.rolling(50).mean()
    vol20 = x["Volume"].rolling(20).mean()
    seg = x.loc[x["Low"].tail(120).idxmin():]
    tp = (seg["High"] + seg["Low"] + seg["Close"]) / 3
    av = pd.Series(np.nan, index=x.index)
    av.loc[seg.index] = (tp * seg["Volume"]).cumsum() / seg["Volume"].cumsum()
    x = x.tail(CHART_BARS)

    def f(s, i):
        v = s.loc[i]
        return None if pd.isna(v) else round(float(v), 2)

    bars = []
    for i, row in x.iterrows():
        v20 = vol20.loc[i]
        bars.append({
            "d": i.strftime("%Y-%m-%d"),
            "o": round(float(row["Open"]), 2), "h": round(float(row["High"]), 2),
            "l": round(float(row["Low"]), 2), "c": round(float(row["Close"]), 2),
            "v": int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
            "e9": f(e9, i), "e21": f(e21, i), "e50": f(e50, i), "m10w": f(ma10w, i), "av": f(av, i),
            "rv": None if pd.isna(v20) or v20 == 0 else round(float(row["Volume"] / v20), 2),
        })
    return bars

# ─────────────────────────────────────────────────────────
#  DOWNLOAD IN BATCHES
# ─────────────────────────────────────────────────────────
def download(tickers, start, label):
    """Batch download with on-disk resume: each finished batch is pickled to CACHE_DIR."""
    import pickle
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(CACHE_DIR, f"{label}_{dt.date.today().isoformat()}.pkl")
    frames = {}
    done_batches = set()
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                saved = pickle.load(f)
            frames, done_batches = saved["frames"], set(saved["done"])
            print(f"    {label}: resuming, {len(frames)} frames cached", flush=True)
        except Exception:
            frames, done_batches = {}, set()

    for i in range(0, len(tickers), BATCH):
        if i in done_batches:
            continue
        budget_check()
        chunk = tickers[i:i + BATCH]
        data = None
        for attempt in range(2):
            try:
                data = yf.download(chunk, start=start, interval="1d", group_by="ticker",
                                   auto_adjust=True, progress=False, threads=True, timeout=25)
                break
            except Exception as e:
                if attempt == 1:
                    print(f"\n    batch {i//BATCH} failed: {e}")
                time.sleep(5)
        if data is not None and not data.empty:
            lvl0 = set(data.columns.get_level_values(0)) if isinstance(data.columns, pd.MultiIndex) else set()
            for t in chunk:
                try:
                    df = data[t] if t in lvl0 else (data if len(chunk) == 1 else None)
                    if df is None:
                        continue
                    df = df.dropna(subset=["Close"])
                    if not df.empty:
                        frames[t] = df
                except Exception:
                    pass
        done_batches.add(i)
        with open(cache_file, "wb") as f:
            pickle.dump({"frames": frames, "done": sorted(done_batches)}, f)
        print(f"    {label}: {min(i+BATCH, len(tickers))}/{len(tickers)}", flush=True)
    return frames


# ─────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────
#  THEMATICS
# ─────────────────────────────────────────────────────────
def load_themes():
    if not os.path.exists(THEMES_PATH):
        return []
    with open(THEMES_PATH) as f:
        d = json.load(f)
    out = []
    for th in d.get("themes", []):
        tk = [t.strip().upper() for t in th.get("tickers", []) if t and t.strip()]
        if th.get("id") and tk:
            out.append({"id": th["id"], "name": th.get("name") or th["id"], "etf": th.get("etf"),
                        "src": th.get("src"), "tickers": list(dict.fromkeys(tk))})
    return out


def _ret(c, n):
    return round(float(c.iloc[-1] / c.iloc[-1 - n] - 1) * 100, 2) if len(c) > n else None


def _ytd(c):
    s = c[c.index.year == c.index[-1].year]
    return round(float(s.iloc[-1] / s.iloc[0] - 1) * 100, 1) if len(s) > 1 else None


def spy_returns(spy):
    c = spy["Close"].dropna()
    return {"chg_1d": _ret(c, 1), "chg_1w": _ret(c, 5), "chg_1m": _ret(c, 21), "ytd": _ytd(c)}


def theme_label(t):
    """Same rules as the dashboard legend."""
    if t["rel_1w"] is None:
        return None
    if t["rel_1w"] > 2 and (t.get("ps_chg_5d") or 0) >= 10:
        return "ON THE MOVE"
    if t["rel_1w"] > 0 and (t["pct_strong"] or 0) >= 50:
        return "LEADING"
    if t["rel_1w"] > 0:
        return "REL. STRONG"
    if (t.get("ps_chg_5d") or 0) <= -20:
        return "FADING"
    return None


def build_themes(themes_def, by_t, full, spy, today_s, dates_hint=None):
    if not themes_def or spy is None:
        return [], []
    spy_c = spy["Close"].dropna()
    idx = spy_c.tail(130).index
    spy_n = spy_c.reindex(idx).ffill(); spy_n = spy_n / spy_n.iloc[0]

    # history of % strong per theme → 5-day change (same mechanism as industries)
    hist = {}
    if os.path.exists(THEME_HISTORY_PATH):
        try:
            with open(THEME_HISTORY_PATH) as f:
                hist = json.load(f)
        except Exception:
            hist = {}
    prev_labels = {}
    if os.path.exists(THEME_DATA_PATH):
        try:
            with open(THEME_DATA_PATH) as f:
                prev_labels = {t["id"]: t.get("label") for t in json.load(f).get("themes", [])}
        except Exception:
            prev_labels = {}
    past = sorted(d for d in hist if d < today_s)

    TK = ("ticker", "name", "price", "chg_1d", "rs_rating", "rs_chg_1m", "stage", "off_52w_high", "dna", "dna_pass", "dna_score",
          "setup_score", "confluence_n", "pocket_pivot", "tight", "rvol", "earnings_days", "plan_stop", "plan_stop_pct", "plan_entry_hi",
          "in_universe", "industry", "etf", "above_ema9", "above_ema21", "ema9_rising", "ema21_rising")
    out, moves, snap = [], [], {}
    for th in themes_def:
        rows = []
        for t in th["tickers"]:
            r = by_t.get(t)
            if not r or t not in full:
                continue
            c = full[t]["Close"].dropna()
            if len(c) < 30:
                continue
            row = {k: r.get(k) for k in TK}
            row.update({"chg_1w": _ret(c, 5), "chg_1m": _ret(c, 21), "ytd": _ytd(c),
                        "near_high": (r.get("off_52w_high") is not None and r["off_52w_high"] >= -5),
                        "strong": bool(r["above_ema9"] and r["above_ema21"] and r["ema9_rising"] and r["ema21_rising"])})
            rows.append(row)
        if not rows:
            continue
        n = len(rows)
        basket = pd.concat([full[r["ticker"]]["Close"].reindex(idx).ffill() for r in rows], axis=1)
        basket = (basket / basket.iloc[0]).mean(axis=1)
        rel = (basket / spy_n).dropna()
        mean = lambda k: round(float(np.mean([r[k] for r in rows if r[k] is not None])), 2) if any(r[k] is not None for r in rows) else None
        rows.sort(key=lambda r: ((r["rs_rating"] or 0), r["setup_score"] or 0), reverse=True)
        pct_strong = round(sum(r["strong"] for r in rows) / n * 100)
        rs_vals = [r["rs_rating"] for r in rows if r["rs_rating"] is not None]
        t = {"id": th["id"], "name": th["name"], "etf": th["etf"], "src": th.get("src"), "n": n,
             "missing": [x for x in th["tickers"] if x not in {r["ticker"] for r in rows}],
             "chg_1d": mean("chg_1d"), "chg_1w": mean("chg_1w"), "chg_1m": mean("chg_1m"), "ytd": mean("ytd"),
             "rel_1w": round(float(rel.iloc[-1] / rel.iloc[-6] - 1) * 100, 2) if len(rel) > 6 else None,
             "rel_1m": round(float(rel.iloc[-1] / rel.iloc[-22] - 1) * 100, 2) if len(rel) > 22 else None,
             "pct_strong": pct_strong,
             "pct_stage2": round(sum(r["stage"] == 2 for r in rows) / n * 100),
             "near_high": sum(r["near_high"] for r in rows),
             "dna_pass": sum(bool(r["dna_pass"]) for r in rows),
             "avg_rs": round(sum(rs_vals) / len(rs_vals)) if rs_vals else None,
             "leaders": [r["ticker"] for r in rows[:3]],
             "chart_tickers": [r["ticker"] for r in rows[:THEME_CHART_TOP]],
             "spark": [round(float(v), 4) for v in rel.iloc[::5]],
             "tickers": rows}
        snap[th["id"]] = {"ps": pct_strong, "rs": t["avg_rs"]}
        # 5-day / 1-day change in % strong
        ref = hist[past[-5]] if len(past) >= 5 else (hist[past[0]] if past else {})
        prev = hist[past[-1]] if past else {}
        r5, r1 = ref.get(th["id"]) or {}, prev.get(th["id"]) or {}
        t["ps_chg_5d"] = round(pct_strong - r5["ps"]) if r5.get("ps") is not None else None
        t["ps_chg_1d"] = round(pct_strong - r1["ps"]) if r1.get("ps") is not None else None
        t["label"] = theme_label(t)
        t["label_prev"] = prev_labels.get(th["id"])
        if th["id"] in prev_labels and prev_labels[th["id"]] != t["label"]:
            moves.append({"id": th["id"], "name": th["name"], "prev": prev_labels[th["id"]], "now": t["label"],
                          "rel_1w": t["rel_1w"], "pct_strong": pct_strong, "ps_chg_5d": t["ps_chg_5d"]})
        out.append(t)

    hist[today_s] = snap
    hist = {d: hist[d] for d in sorted(hist)[-30:]}
    with open(THEME_HISTORY_PATH, "w") as f:
        json.dump(hist, f, separators=(",", ":"))
    out.sort(key=lambda t: -(t["rel_1w"] if t["rel_1w"] is not None else -999))
    return out, moves


def _sa_item_url(guid, link, title):
    """Seeking Alpha RSS: articles carry a real slug URL, news items only link to the
    symbol's news page — rebuild the direct /news/<id> link from the guid."""
    gid = guid or ""
    if "Article:" in gid:
        kind = "transcript" if "transcript" in ((link or "") + " " + (title or "")).lower() else "article"
        return kind, link
    if "MarketCurrent:" in gid:
        nid = gid.rsplit(":", 1)[-1]
        if nid.isdigit():
            return "news", f"https://seekingalpha.com/news/{nid}"
    return "news", link


def fetch_sa_news(tickers, per_ticker=SA_NEWS_PER_TK, workers=SA_NEWS_WORKERS):
    """Headlines per ticker from Seeking Alpha's public per-symbol RSS feed.
    Returns {ticker: [{t,u,d,a,k}, ...]} — never raises, a dead feed just yields nothing."""
    import xml.etree.ElementTree as ET
    from concurrent.futures import ThreadPoolExecutor
    from email.utils import parsedate_to_datetime
    import requests

    NS = {"sa": "https://seekingalpha.com/api/1.0"}
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; traderdeck/1.0; personal dashboard)",
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
    })

    def candidates(tk):
        # SA uses dots for class shares (BRK.B); yfinance/Nasdaq use hyphens or slashes.
        out = [tk, tk.replace("-", ".").replace("/", "."), tk.replace(".", "-")]
        seen, uniq = set(), []
        for c in out:
            if c and c not in seen:
                seen.add(c); uniq.append(c)
        return uniq

    def one(tk):
        for sym in candidates(tk):
            try:
                r = sess.get(SA_FEED.format(sym), timeout=SA_NEWS_TIMEOUT)
            except Exception:
                continue
            if r.status_code != 200 or not r.content.lstrip()[:1] == b"<":
                continue
            try:
                root = ET.fromstring(r.content)
            except Exception:
                continue
            items = []
            for it in root.iterfind("./channel/item"):
                title = (it.findtext("title") or "").strip()
                if not title:
                    continue
                kind, url = _sa_item_url((it.findtext("guid") or "").strip(),
                                         (it.findtext("link") or "").strip(), title)
                if not url:
                    continue
                try:
                    d = parsedate_to_datetime(it.findtext("pubDate") or "")
                    d = d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
                except Exception:
                    d = None
                author = (it.findtext("sa:author_name", namespaces=NS) or "").strip()
                items.append({"t": title, "u": url, "d": d, "a": author or None, "k": kind})
            if items:
                items.sort(key=lambda x: x["d"] or "", reverse=True)
                keep = items[:per_ticker]
                # always carry the newest earnings-call transcript, even if it fell off the list
                tr = next((i for i in items if i["k"] == "transcript"), None)
                if tr is not None and tr not in keep:
                    keep.append(tr)
                return tk, keep
        return tk, []

    todo = list(tickers)
    if len(todo) > SA_NEWS_MAX_TK:
        print(f"  ! SA news capped at {SA_NEWS_MAX_TK} of {len(todo)} tickers "
              f"— raise SA_NEWS_MAX_TK, the rest gets no headlines")
        todo = todo[:SA_NEWS_MAX_TK]
    res = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for tk, items in ex.map(one, todo):
            if items:
                res[tk] = items
    return res


def main():
    t0 = time.time()
    print("TRADERDECK — Full Market Scan")
    universe, _ = load_universe()
    sp500 = load_sp500()

    directory = load_directory()
    syms = {t: v for t, v in directory.items()
            if (v.get("market_cap") or 0) >= MIN_MARKET_CAP and (v.get("last") or 0) >= MIN_PRICE}
    print(f"  {len(syms)} names with market cap >= ${MIN_MARKET_CAP/1e9:.0f}B and price >= ${MIN_PRICE:.0f}")
    alive = exclude_dead(sorted(syms), label="scan")

    # ── 3. Quick liquidity pass ──────────────────────────
    start60 = (dt.date.today() - dt.timedelta(days=90)).strftime("%Y-%m-%d")
    print("  Liquidity pre-filter (60d)...")
    quick = download(alive, start60, "prefilter")
    record_results(alive, [t for t, df in quick.items() if len(df) >= 5])
    liquid = []
    for t, df in quick.items():
        if len(df) < 40:
            continue
        close = float(df["Close"].iloc[-1])
        dvol = float((df["Close"] * df["Volume"]).iloc[-20:].mean())
        if close >= MIN_PRICE and dvol >= PREFILTER_VOL_USD:
            liquid.append(t)
    print(f"  {len(liquid)} names pass liquidity (price >= ${MIN_PRICE:.0f}, $vol >= {PREFILTER_VOL_USD/1e6:.0f}M)")

    # ── 3b. Sector / industry: from the directory; yfinance only for the unclassified few ──
    smap = {t: dict(directory.get(t, {})) for t in liquid}
    unclassified_t = [t for t in liquid if not smap[t].get("etf")]
    if unclassified_t and not TIME_BUDGET:
        print(f"  {len(unclassified_t)} liquid names without sector — yfinance fallback (max 200)")
        for t, v in yf_fallback(unclassified_t, max_new=200).items():
            smap[t].update(v)

    # ── 4. Full analysis ─────────────────────────────────
    # 480 days ~= 334 trading days: enough for a 252-bar RS window PLUS the
    # 21-bar offset used for rs_rating_1m, with ~60 bars to spare.
    start400 = (dt.date.today() - dt.timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    themes_def = load_themes()
    theme_tickers = sorted({t for th in themes_def for t in th["tickers"]})
    need = sorted(set(liquid) | set(sp500) | set(theme_tickers) | {"SPY"})
    print(f"  Full history for {len(need)} names...")
    full = download(need, start400, "full")

    spy = full.get("SPY")
    spy_chg = spy["Close"].pct_change() * 100 if spy is not None else None

    # RS pool = S&P 500 + all liquid names (one consistent percentile for both scripts' output)
    rs_pool, rs_pool_1m = {}, {}
    for t in set(sp500) | set(liquid):
        df = full.get(t)
        if df is None or len(df) < 70:
            continue
        cl = df["Close"].dropna()
        r = rs_raw(cl)
        if r is not None:
            rs_pool[t] = r
        # Same formula on the series as it stood 21 trading days ago. RS is a pure
        # function of price, so the whole history is recomputable — no stored
        # rs_history and no external source needed.
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

    def percentile_1m(x):
        return _pct(x, pool_vals_1m)

    def rs_1m_of(df):
        cl = df["Close"].dropna()
        if len(cl) <= RS_LOOKBACK_1M + 70:
            return None
        return percentile_1m(rs_raw(cl.iloc[:-RS_LOOKBACK_1M]))

    try:
        earnings = load_earnings()
    except Exception as e:
        print(f"  (earnings calendar unavailable: {e})"); earnings = {}

    def enrich(t):
        df = full.get(t)
        if df is None:
            return None
        try:
            m = analyse(t, df, spy_chg)
        except Exception:
            return None
        if not m:
            return None
        m["rs_rating"] = percentile(m.pop("rs_raw"))
        m["rs_rating_1m"] = rs_1m_of(df)
        m["rs_chg_1m"] = (m["rs_rating"] - m["rs_rating_1m"]) \
            if (m["rs_rating"] is not None and m["rs_rating_1m"] is not None) else None
        early = m.get("early") or {}
        early["rs_turn"] = bool(m["rs_chg_1m"] is not None and m["rs_chg_1m"] >= RS_TURN_MIN
                                and m["rs_rating"] is not None and 50 <= m["rs_rating"] < 85)
        m["early"] = early
        m["early_score"] = sum(1 for v in early.values() if v)
        sm = smap.get(t) or {}
        m["name"] = (sm.get("name") or "")[:40]
        m["sector"] = sm.get("sector")
        m["industry"] = sm.get("industry")
        m["etf"] = sm.get("etf")
        m["market_cap"] = sm.get("market_cap")
        er = earnings.get(t)
        m["earnings_date"] = er
        m["earnings_days"] = (dt.date.fromisoformat(er) - dt.date.today()).days if er else None
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
        am = m["atr_mult_50"] or 0
        m["extension"] = "reduce" if am > EXT_REDUCE else "caution" if am > EXT_CAUTION else "ok"
        m["in_universe"] = t in universe
        m["in_sp500"] = t in sp500
        m["setup_score"] = (
            m["dna_score"] + m["confluence_n"]
            + (1 if (m["vol_ratio_5_20"] or 1) < 0.8 else 0)
            + (1 if m["tight"] else 0) + (1 if m["pocket_pivot"] else 0)
            + min(m["wis_count_20d"], 3)
        )
        return m

    rows = []
    for t in liquid:
        m = enrich(t)
        if m:
            rows.append(m)
    n_analysed = len(rows)
    # theme tickers that did not pass the liquidity pre-filter: analysed too, but kept out of market breadth / sectors
    extra = {}
    for t in theme_tickers:
        if t in full and t not in {r["ticker"] for r in rows}:
            m = enrich(t)
            if m:
                extra[t] = m

    rows.sort(key=lambda x: (x["dna_pass"], x["setup_score"], x["rs_rating"] or 0), reverse=True)
    passed = [r for r in rows if r["dna_pass"]]
    keep = {r["ticker"]: r for r in passed}
    for r in rows:
        if len(keep) >= max(KEEP_TOP, len(passed)):
            break
        keep.setdefault(r["ticker"], r)
    out_rows = list(keep.values())

    # Market-wide breadth with the same regime definition as the universe meter
    strong = sum(1 for r in rows if r["above_ema9"] and r["above_ema21"] and r["ema9_rising"] and r["ema21_rising"])
    stage2 = sum(1 for r in rows if r["stage"] == 2)

    COMPACT = ("ticker", "name", "sector", "industry", "etf", "market_cap", "price", "chg_1d", "rs_rating", "stage",
               "rs_rating_1m", "rs_chg_1m",
               "adr_pct", "atr_mult_50", "off_52w_high", "avg_dollar_vol", "lod_dist", "confluence",
               "confluence_n", "dist_atr", "pocket_pivot", "tight", "vol_ratio_5_20", "wis_count_20d",
               "wis_today", "dna", "dna_pass", "dna_score", "extension", "setup_score", "in_universe",
               "above_ema9", "above_ema21", "above_ema50", "ema9_rising", "ema21_rising",
               "rvol", "earnings_date", "earnings_days",
               "plan_stop", "plan_stop_pct", "plan_stop_basis", "plan_entry_lo", "plan_entry_hi")
    all_rows = [{k: r.get(k) for k in COMPACT} for r in rows]

    def is_strong(r):
        return bool(r["above_ema9"] and r["above_ema21"] and r["ema9_rising"] and r["ema21_rising"])

    def agg(group):
        n = len(group)
        rs_vals = [g["rs_rating"] for g in group if g["rs_rating"] is not None]
        top = sorted(group, key=lambda g: (g["rs_rating"] or 0, g["setup_score"]), reverse=True)
        return {
            "n": n,
            "pct_strong": round(sum(is_strong(g) for g in group) / n * 100, 1) if n else None,
            "pct_stage2": round(sum(g["stage"] == 2 for g in group) / n * 100, 1) if n else None,
            "avg_rs": round(sum(rs_vals) / len(rs_vals), 1) if rs_vals else None,
            "dna_pass": sum(g["dna_pass"] for g in group),
            "chg_1d_median": round(float(np.median([g["chg_1d"] for g in group if g["chg_1d"] is not None])), 2) if n else None,
            "top": [g["ticker"] for g in top[:5]],
        }

    sectors = {}
    for etf in SECTOR_ETF.values():
        group = [r for r in all_rows if r["etf"] == etf]
        if not group:
            continue
        sec = agg(group)
        sec["sector"] = next((k for k, v in SECTOR_ETF.items() if v == etf), etf)
        inds = {}
        for name in sorted({g["industry"] or "Other" for g in group}):
            ig = [g for g in group if (g["industry"] or "Other") == name]
            inds[name] = agg(ig)
        # industries ranked by avg RS, then % strong
        sec["industries"] = dict(sorted(inds.items(), key=lambda kv: (kv[1]["avg_rs"] or 0, kv[1]["pct_strong"] or 0), reverse=True))
        sectors[etf] = sec
    unclassified = sum(1 for r in all_rows if not r["etf"])
    today_s = dt.date.today().isoformat()

    # ── Industry / sector history (30 days) → 5-day change in % strong ──
    hist = {}
    if os.path.exists(INDUSTRY_HISTORY_PATH):
        try:
            with open(INDUSTRY_HISTORY_PATH) as f:
                hist = json.load(f)
        except Exception:
            hist = {}
    snap = {}
    for etf, sec in sectors.items():
        snap[etf] = {"ps": sec["pct_strong"], "rs": sec["avg_rs"]}
        for name, g in sec["industries"].items():
            snap[f"{etf}|{name}"] = {"ps": g["pct_strong"], "rs": g["avg_rs"]}
    hist[today_s] = snap
    dates = sorted(hist)[-30:]
    hist = {d: hist[d] for d in dates}
    with open(INDUSTRY_HISTORY_PATH, "w") as f:
        json.dump(hist, f, separators=(",", ":"))

    def change(key, n=5):
        past = [d for d in dates if d < today_s]
        if len(past) < 1:
            return None, None
        ref = hist[past[-n]] if len(past) >= n else hist[past[0]]
        prev = hist[past[-1]]
        now = snap.get(key) or {}
        r5 = ref.get(key) or {}
        r1 = prev.get(key) or {}
        d5 = round(now["ps"] - r5["ps"], 1) if now.get("ps") is not None and r5.get("ps") is not None else None
        d1 = round(now["ps"] - r1["ps"], 1) if now.get("ps") is not None and r1.get("ps") is not None else None
        return d5, d1
    for etf, sec in sectors.items():
        sec["ps_chg_5d"], sec["ps_chg_1d"] = change(etf)
        for name, g in sec["industries"].items():
            g["ps_chg_5d"], g["ps_chg_1d"] = change(f"{etf}|{name}")

    # ── Market regime history (60 days) ──
    pct_strong_mkt = round(strong / n_analysed * 100, 1) if n_analysed else None
    regime_mkt = (("green" if pct_strong_mkt >= 60 else "yellow" if pct_strong_mkt >= 40 else "red") if pct_strong_mkt is not None else None)
    prev_payload = {}
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH) as f:
                prev_payload = json.load(f)
        except Exception:
            prev_payload = {}
    mkt_hist = [h for h in (prev_payload.get("regime_history") or []) if h.get("date") != today_s]
    mkt_hist.append({"date": today_s, "pct_strong": pct_strong_mkt, "regime": regime_mkt,
                     "dna_pass": len(passed), "pct_stage2": round(stage2 / n_analysed * 100, 1) if n_analysed else None})
    mkt_hist = mkt_hist[-60:]

    # ── What changed since the previous run ──
    prev_rows = {r["ticker"]: r for r in (prev_payload.get("all") or [])}
    prev_pass = {t for t, r in prev_rows.items() if r.get("dna_pass")}
    now_pass = {r["ticker"] for r in passed}
    prev_date = prev_payload.get("updated_date")
    prev_regime = (prev_payload.get("stats") or {}).get("regime")
    def brief(t):
        r = next((x for x in all_rows if x["ticker"] == t), None) or prev_rows.get(t) or {}
        return {"ticker": t, "name": r.get("name"), "etf": r.get("etf"), "industry": r.get("industry"),
                "rs_rating": r.get("rs_rating"), "setup_score": r.get("setup_score"), "chg_1d": r.get("chg_1d")}
    ind_moves = []
    for etf, sec in sectors.items():
        for name, g in sec["industries"].items():
            if g.get("ps_chg_5d") is not None and abs(g["ps_chg_5d"]) >= 15 and g["n"] >= 4:
                ind_moves.append({"etf": etf, "industry": name, "n": g["n"], "pct_strong": g["pct_strong"], "chg_5d": g["ps_chg_5d"], "avg_rs": g["avg_rs"]})
    ind_moves.sort(key=lambda x: -abs(x["chg_5d"]))
    changes = {
        "since": prev_date,
        "regime_prev": prev_regime, "regime_now": regime_mkt,
        "new_dna_pass": sorted([brief(t) for t in now_pass - prev_pass], key=lambda x: -(x["rs_rating"] or 0)),
        "dropped_dna_pass": sorted([brief(t) for t in prev_pass - now_pass], key=lambda x: -(x["rs_rating"] or 0)),
        "industry_moves": ind_moves[:12],
        "earnings_soon": sorted([brief(r["ticker"]) | {"earnings_days": r["earnings_days"], "earnings_date": r["earnings_date"]}
                                 for r in passed if r.get("earnings_days") is not None and r["earnings_days"] <= 7],
                                key=lambda x: x["earnings_days"]),
    }

    # ── Thematics: equal-weight baskets, relative to SPY ──
    by_t = {r["ticker"]: r for r in rows} | extra
    themes_out, theme_moves = build_themes(themes_def, by_t, full, spy, today_s, dates_hint=None)
    changes["theme_moves"] = theme_moves

    payload = {
        "updated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "updated_date": dt.date.today().isoformat(),
        "stats": {
            "symbols_listed": len(syms), "liquid": len(liquid), "analysed": n_analysed,
            "dna_pass": len(passed), "kept": len(out_rows), "unclassified": unclassified,
            "pct_strong": pct_strong_mkt, "regime": regime_mkt,
            "pct_stage2": round(stage2 / n_analysed * 100, 1) if n_analysed else None,
            "new_vs_universe": sum(1 for r in passed if not r["in_universe"]),
            "earnings_tickers": len(earnings),
            "runtime_s": int(time.time() - t0),
        },
        "thresholds": {**DNA, "min_price": MIN_PRICE, "prefilter_vol_usd": PREFILTER_VOL_USD},
        "leaders": [{k: r.get(k) for k in COMPACT} for r in out_rows],
        "sectors": sectors,
        "regime_history": mkt_hist,
        "changes": changes,
        "all": all_rows,
    }
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    # ── 6a. Notable: Stage 1 names showing early signs of a Stage 2 turn ──
    # Watchlist seeding for the first pullback after the transition, NOT a buy list.
    notable = []
    try:
        for r in rows:
            if r.get("stage") != 1 or (r.get("early_score") or 0) < NOTABLE_MIN_SIGNALS:
                continue
            if (r.get("avg_dollar_vol") or 0) < NOTABLE_MIN_DOLLAR_VOL:
                continue
            if (r.get("rs_rating") or 0) >= DNA["rs_rating_min"]:
                continue            # already strong enough for the screener
            cap = r.get("market_cap")
            if cap is not None and cap < 2_000_000_000:
                continue
            df = full.get(r["ticker"])
            if df is None or len(df) < 60:
                continue
            lo252 = float(df["Low"].iloc[-252:].min())
            if not lo252 or (r.get("price") or 0) < NOTABLE_MIN_ABOVE_LOW * lo252:
                continue            # still making lows
            notable.append({k: r.get(k) for k in (
                "ticker", "name", "industry", "etf", "price", "chg_1d", "stage", "rs_rating",
                "rs_chg_1m", "avg_dollar_vol", "off_52w_high", "early", "early_score",
                "setup_score", "plan_stop", "plan_stop_pct", "plan_stop_basis",
                "plan_entry_lo", "plan_entry_hi", "confluence_n", "rvol")})
        # Scores cluster at 1, so the cap would otherwise truncate arbitrarily.
        # Rank by: score, then how many of the signals are actual TURN signals
        # (the 200 turning, a 50 reclaim, RS turning) rather than merely
        # constructive ones (accumulation, higher lows, a pocket pivot), then dRS.
        TURN_SIGNALS = ("ma200_turn", "reclaim50", "rs_turn")
        notable.sort(key=lambda r: ((r["early_score"] or 0),
                                    sum(1 for k in TURN_SIGNALS if (r.get("early") or {}).get(k)),
                                    (r["rs_chg_1m"] if r["rs_chg_1m"] is not None else -999)), reverse=True)
        notable = notable[:NOTABLE_MAX]
        with open(NOTABLE_PATH, "w") as f:
            json.dump({"updated_at": payload["updated_at"],
                       "thresholds": {"min_signals": NOTABLE_MIN_SIGNALS, "min_dollar_vol": NOTABLE_MIN_DOLLAR_VOL,
                                      "min_above_low": NOTABLE_MIN_ABOVE_LOW, "rs_max": DNA["rs_rating_min"]},
                       "rows": notable}, f, separators=(",", ":"))
        print(f"  Saved {NOTABLE_PATH} ({len(notable)} Stage 1 names with >= {NOTABLE_MIN_SIGNALS} early signals)")
    except Exception as e:
        notable = []
        print(f"  (notable scan skipped: {e})")

    # ── 6b. Chart data for section 06 ──
    #   DNA passers (top CHART_MAX by setup score)
    # + theme leaders, but only where the theme beats SPY AND the name is Stage 2
    # + every Notable row, so section 07 can click through to a chart
    charts = {}
    try:
        chart_rows = list(passed[:CHART_MAX])
        dna_rank = {r["ticker"]: i + 1 for i, r in enumerate(chart_rows)}
        seen = set(dna_rank)
        why = {t: "dna" for t in seen}

        for th in themes_out:
            if (th.get("rel_1w") or -99) <= 0:
                continue                                   # theme is not beating SPY
            for tk in th["chart_tickers"]:
                r = by_t.get(tk)
                if tk in seen or r is None or r.get("stage") != 2:
                    continue
                chart_rows.append(r); seen.add(tk); why[tk] = "theme"

        for r in notable:
            tk = r["ticker"]
            if tk not in seen and tk in by_t:
                chart_rows.append(by_t[tk]); seen.add(tk); why[tk] = "notable"

        def picks_for(tk):
            return [{"id": th["id"], "name": th["name"], "rank": th["chart_tickers"].index(tk) + 1,
                     "label": th.get("label"), "rel_1w": th.get("rel_1w")}
                    for th in themes_out if tk in th["chart_tickers"]]

        charts = {}
        for r in chart_rows:
            tk = r["ticker"]
            if tk not in full:
                continue
            charts[tk] = {"meta": {k: r.get(k) for k in CHART_META} | {
                              "themes": [th["id"] for th in themes_out if tk in th["chart_tickers"]],
                              "theme_picks": picks_for(tk),
                              "dna_rank": dna_rank.get(tk),
                              "early": r.get("early"), "early_score": r.get("early_score"),
                              "why": why.get(tk, "dna")},
                          "bars": chart_bars(full[tk])}
        with open(CHART_PATH, "w") as f:
            json.dump({"updated": payload["updated_at"], "charts": charts}, f, separators=(",", ":"))
        n_by = {w: sum(1 for t in charts if charts[t]["meta"]["why"] == w) for w in ("dna", "theme", "notable")}
        print(f"  Saved {CHART_PATH} ({len(charts)} tickers: {n_by['dna']} DNA, "
              f"{n_by['theme']} theme, {n_by['notable']} notable)")
    except Exception as e:
        print(f"  (chart data skipped: {e})")

    with open(THEME_DATA_PATH, "w") as f:
        json.dump({"updated_at": payload["updated_at"], "updated_date": today_s,
                   "spy": spy_returns(spy), "themes": themes_out}, f, separators=(",", ":"))
    print(f"  Saved {THEME_DATA_PATH} ({len(themes_out)} themes, {len(theme_moves)} label changes)")

    # ── 7. Seeking Alpha headlines for the charted tickers (section 06) ──
    try:
        sa_tickers = sorted(charts.keys())
        if not sa_tickers:
            raise RuntimeError("no chart tickers")
        sa_t0 = time.time()
        sa_news = fetch_sa_news(sa_tickers)
        n_items = sum(len(v) for v in sa_news.values())
        n_tr = sum(1 for v in sa_news.values() for i in v if i["k"] == "transcript")
        with open(SA_NEWS_PATH, "w") as f:
            json.dump({"updated_at": payload["updated_at"],
                       "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
                       "source": "Seeking Alpha per-symbol RSS",
                       "news": sa_news}, f, separators=(",", ":"), ensure_ascii=False)
        missing = [t for t in sa_tickers if t not in sa_news]
        print(f"  Saved {SA_NEWS_PATH} ({len(sa_news)}/{len(sa_tickers)} tickers, "
              f"{n_items} headlines, {n_tr} transcripts, {int(time.time() - sa_t0)}s)")
        if missing:
            print(f"    no feed for: {', '.join(missing[:20])}{' ...' if len(missing) > 20 else ''}")
    except Exception as e:
        print(f"  (Seeking Alpha news skipped: {e})")

    print(f"  Analysed {n_analysed} · DNA pass {len(passed)} "
          f"({payload['stats']['new_vs_universe']} not in my universe) · "
          f"market strong {payload['stats']['pct_strong']}% · {payload['stats']['runtime_s']}s")
    print(f"  Sectors: " + ", ".join(f"{k} {v['pct_strong']}%/{v['dna_pass']}" for k, v in sectors.items()) + f" · unclassified {unclassified}")
    print(f"  Top DNA pass: {', '.join(r['ticker'] for r in passed[:25])}")
    print(f"  Saved {OUTPUT_PATH}")


if __name__ == "__main__":
    try:
        main()
    except OutOfTime:
        print("  Time budget reached — progress cached, run again to resume.")
        sys.exit(3)
