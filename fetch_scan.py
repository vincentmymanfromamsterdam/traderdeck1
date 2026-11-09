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
    DNA, EXT_CAUTION, EXT_REDUCE, analyse, ema, load_universe, load_sp500, rs_raw, set_tt_rs,
)
from sector_map import load_directory, yf_fallback, SECTOR_ETF, exclude_dead, record_results, load_earnings, apply_industry_map

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

HISTORY_DAYS   = 730      # calendar days of daily history (v7.1: 480 -> 730 so the breakout base count
                          # can look back ~13 months; it needs the SMA200 plus a 120-260 bar pivot window)
RS_LOOKBACK_1M = 21       # trading days back for the "RS a month ago" percentile
RS_TURN_MIN    = 15       # rs_chg_1m needed for the rs_turn signal
NOTABLE_PATH   = "data/notable.json"
NOTABLE_MIN_SIGNALS = 1   # of 6 — one real signal makes a name notable; the score ranks them
NOTABLE_MAX_BASE     = 15  # Stage 1 bases with early Stage 2 signs
NOTABLE_MAX_RECOVERY = 10  # corrected names (Stage 1/3) turning back up: >=2 signals incl. a turn signal
NOTABLE_MAX_UR       = 15  # undercut & reclaim, its own gate (see 6a). Measured on the
                           # 394-name sample: ~4 open U&R's in a reversal week (6 Nov 2026)
                           # against ~1 in an average week, so on the full pool this gate
                           # yields roughly 19 candidates now and 4 when markets are calm.
NOTABLE_RECOVERY_OFF_HIGH = -15   # "really corrected": below the 200 SMA or at least this far off the high
NOTABLE_FALLING_200 = -0.01       # 200 SMA slope over 20 bars below this = still falling -> recovery, not base
NOTABLE_MIN_DOLLAR_VOL = 50_000_000
NOTABLE_MIN_ABOVE_LOW  = 1.05    # dead-cat guard only. 25% cut the wrong names:
                                 # a quiet consolidation sits close to BOTH its 52w low
                                 # and high, so distance-from-low says nothing about quality.

# ── 8. Setup module (section 05b, v7) ──
SETUPS_PATH      = "data/setups.json"
SETUPS_LOG_PATH  = "data/setups_log.json"     # live measurement log: every new hit, no outcomes
SETUP_CAP        = 15
SETUP_MIN_PRICE  = 15.0                       # O'Neil: no stocks under $15 in the setup module
SETUP_MIN_DOLLAR_VOL = 50_000_000
SETUP_MIN_CAP    = 2_000_000_000
SETUP_PB_RS_MIN  = 85
SETUP_PB_MIN_LAYERS = 3
SETUP_GU_RS_MIN  = 70
SETUPS_LOG_MAX   = 5000
GRP_RS_MIN       = 80                          # group confirmation: peers in the same industry with RS >= 80
# v7.5 bottom checklist: signal 3 (fewer new lows on the retest) and 4 (RS-houders tab)
REGIME_PATH      = "data/regime.json"          # written by fetch_regime.py earlier in the same run
NL_SESSIONS      = 40                          # sessions of the new-52w-low count kept for the dashboard
RH_RS_MIN        = 85                          # RS-houders: leaders that hold up while the index corrects
RH_OFF_HIGH_MAX  = 8.0                         #   <= 8% under the 52w high
RH_PER_INDUSTRY  = 2                           #   in the tab: at most 2 per industry (counts cover all)
RH_HOLD_MIN      = 10                          # signal 4 is met when >= 10 leaders held their low
RH_REF_PAD       = 2                           # own reference low = min low within +-2 sessions of the prior heavy day

CHART_PATH  = "data/chart_data.json"
CHART_MAX   = 40          # max DNA passers with chart data
CHART_SHOW  = 45          # v7.1: cards in the default view (TOP), ranked on "potential"; the rest go to chart_more.json
CHART_MORE_PATH = "data/chart_more.json"   # lazy-loaded by the dashboard: other candidates + scan leaders + universe + themes
CHART_BARS  = 130         # ~6 months of daily bars
CHART_META  = ("ticker", "name", "industry", "rs_rating", "rs_chg_1m", "stage", "adr_pct", "atr_mult_50", "off_52w_high",
               "setup_score", "rvol", "earnings_date", "earnings_days", "plan_stop", "plan_stop_pct",
               "plan_stop_basis", "plan_entry_lo", "plan_entry_hi", "dna", "confluence", "pocket_pivot", "tight",
               "ur", "bo", "gu", "tt", "grp", "in_universe")

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
    # v7.2: Yahoo industry over the SIC-based screener label (solar out of "Semiconductors")
    try:
        apply_industry_map(smap, liquid, fetch_new=not TIME_BUDGET)
    except Exception as e:
        print(f"  (industry map skipped: {e})")

    # ── 4. Full analysis ─────────────────────────────────
    # 480 days ~= 334 trading days: enough for a 252-bar RS window PLUS the
    # 21-bar offset used for rs_rating_1m, with ~60 bars to spare.
    start400 = (dt.date.today() - dt.timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    themes_def = load_themes()
    theme_tickers = sorted({t for th in themes_def for t in th["tickers"]})
    need = sorted(set(liquid) | set(sp500) | set(theme_tickers) | set(universe) | {"SPY"})
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
        set_tt_rs(m)
        early = m.get("early") or {}
        # One definition everywhere: dRS >= +15 with RS still under 85. The old floor of 50
        # ("turning into strength") blocked deep turns by construction — TMO sat at RS 33
        # on its 29 May 2026 reversal day with dRS +15.
        early["rs_turn"] = bool(m["rs_chg_1m"] is not None and m["rs_chg_1m"] >= RS_TURN_MIN
                                and m["rs_rating"] is not None and m["rs_rating"] < 85)
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

    # Group confirmation (O'Neil p388): how many OTHER names in the same industry
    # are leaders (RS >= 80). Counted over the liquid pool only.
    grp_n = {}
    for r in rows:
        if r.get("industry") and (r.get("rs_rating") or 0) >= GRP_RS_MIN:
            grp_n[r["industry"]] = grp_n.get(r["industry"], 0) + 1
    in_pool = {id(r) for r in rows}
    for r in list(rows) + list(extra.values()):
        own = 1 if (r.get("rs_rating") or 0) >= GRP_RS_MIN and id(r) in in_pool else 0
        r["grp"] = (grp_n.get(r.get("industry"), 0) - own) if r.get("industry") else None

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
               "confluence_n", "dist_atr", "pocket_pivot", "tight", "ur", "bo", "gu", "tt", "grp", "vol_ratio_5_20", "wis_count_20d",
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

    # ── 6a. Notable: two routes, NOT a buy list ──
    #   BASE     — Stage 1 base with early Stage 2 signs. Seeds the watchlist for the
    #              first pullback after the 1 -> 2 transition.
    #   RECOVERY — a really corrected name (Stage 1/3, below the 200 or >= 15% off its
    #              high) turning back up: >= 2 signals of which at least one is a turn
    #              signal. Catches a V-recovery inside a larger uptrend, which is never
    #              Stage 1 and so invisible to the base route (case study: TMO, whose
    #              27-29 May 2026 reversal this gate flags on 29 May at $492).
    #   U&R      — Iain Murphy's undercut & reclaim: a support that held >= 5 bars is
    #              undercut intraday and reclaimed on the close on >= 2x volume, with
    #              price above the 200 SMA. No stage and no RS cap: a U&R on a leader
    #              is the best version of the setup, and the DNA table cannot show it.
    #              Takes priority over base/recovery -- it is a dated event with a
    #              five-session shelf life, after which the name falls back to its
    #              structural route (case study: TYL, 29 Oct 2026, +12.5% in 10 days).
    notable = []
    TURN_SIGNALS = ("ma200_turn", "reclaim50", "rs_turn")
    try:
        for r in rows:
            if (r.get("avg_dollar_vol") or 0) < NOTABLE_MIN_DOLLAR_VOL:
                continue
            cap = r.get("market_cap")
            if cap is not None and cap < 2_000_000_000:
                continue
            df = full.get(r["ticker"])
            if df is None or len(df) < 60:
                continue
            lo252 = float(df["Low"].iloc[-252:].min())
            if not lo252 or (r.get("price") or 0) < NOTABLE_MIN_ABOVE_LOW * lo252:
                continue            # still making lows
            e = r.get("early") or {}
            sc = r.get("early_score") or 0
            strong = (r.get("rs_rating") or 0) >= DNA["rs_rating_min"]
            turn = any(e.get(k) for k in TURN_SIGNALS)
            corrected = (r.get("above_s200") is False
                         or (r.get("off_52w_high") or 0) <= NOTABLE_RECOVERY_OFF_HIGH)
            falling200 = (r.get("s200_slope_20") or 0) < NOTABLE_FALLING_200
            # Since v6.4 Stage 1 holds two different animals, split by the 200's slope:
            #   flat/rising 200  -> a quiet Weinstein base, the classic 1->2 candidate
            #   still falling    -> a fresh reclaim of a falling 200 (RKT, 4 Nov 2026)
            # Without the split the reclaims crowd the bases out of the cap.
            route = None
            ur = r.get("ur")
            if ur and r.get("above_s200"):
                continue            # v7: lives in the setup module (05b) while the U&R is fresh
            elif strong:
                continue            # already strong enough for the screener
            elif r.get("stage") == 1 and not falling200 and sc >= NOTABLE_MIN_SIGNALS:
                route = "base"
            elif sc >= 2 and turn and (
                    (r.get("stage") == 1 and falling200)
                    or (r.get("stage") == 3 and corrected)):
                route = "recovery"
            if route is None:
                continue
            notable.append({k: r.get(k) for k in (
                "ticker", "name", "industry", "etf", "price", "chg_1d", "stage", "rs_rating",
                "rs_chg_1m", "avg_dollar_vol", "off_52w_high", "early", "early_score",
                "setup_score", "plan_stop", "plan_stop_pct", "plan_stop_basis",
                "plan_entry_lo", "plan_entry_hi", "confluence_n", "rvol",
                "s200_slope_20", "above_s200", "above_ema50", "ur")} | {"route": route})
        # Rank: score, then the number of actual TURN signals over merely constructive
        # ones (accumulation, higher lows, a pocket pivot), then dRS.
        notable.sort(key=lambda r: ((r["early_score"] or 0),
                                    sum(1 for k in TURN_SIGNALS if (r.get("early") or {}).get(k)),
                                    (r["rs_chg_1m"] if r["rs_chg_1m"] is not None else -999)), reverse=True)
        notable = ([r for r in notable if r["route"] == "base"][:NOTABLE_MAX_BASE]
                   + [r for r in notable if r["route"] == "recovery"][:NOTABLE_MAX_RECOVERY])
        with open(NOTABLE_PATH, "w") as f:
            json.dump({"updated_at": payload["updated_at"],
                       "thresholds": {"min_signals": NOTABLE_MIN_SIGNALS, "min_dollar_vol": NOTABLE_MIN_DOLLAR_VOL,
                                      "min_above_low": NOTABLE_MIN_ABOVE_LOW, "rs_max": DNA["rs_rating_min"],
                                      "recovery_off_high": NOTABLE_RECOVERY_OFF_HIGH,
                                      "ur": {"min_held": 5, "min_vol_x": 2.0, "max_under_pct": 7.0,
                                             "max_age_days": 5, "gate": "close > SMA200"}},
                       "rows": notable}, f, separators=(",", ":"))
        nb = sum(1 for r in notable if r["route"] == "base")
        nr = sum(1 for r in notable if r["route"] == "recovery")
        print(f"  Saved {NOTABLE_PATH} ({nb} base + {nr} recovery; U&R moved to {SETUPS_PATH})")
        if notable:
            print("    " + ", ".join(f"{r['ticker']}:{r['route'][:3]}{r['early_score']}" for r in notable[:12]))
    except Exception as e:
        notable = []
        print(f"  (notable scan skipped: {e})")

    # ── 8. Setup module (section 05b): four watchlists, one per setup ──
    #   pullback  — Stage 2 leader in a confluence zone (Mijn swing strategie, step 3)
    #   ur        — undercut & reclaim, the former Notable route, unchanged rules
    #   breakout  — base breakout (fetch_swing bo), measured in T2
    #   gapup     — buyable gap-up after Morales & Kacher (fetch_swing gu), measured live
    # All lists share: price >= $15, $vol >= 50M, cap >= $2B. The dashboard labels
    # each tab with the regime warning; nothing is hidden by regime.
    setups = {"pullback": [], "ur": [], "breakout": [], "gapup": []}
    setup_of = {}
    try:
        SU_KEYS = ("ticker", "name", "industry", "etf", "price", "chg_1d", "stage", "rs_rating", "rs_chg_1m",
                   "tt", "grp", "adr_pct", "lod_dist", "avg_dollar_vol", "above_s200", "earnings_days",
                   "plan_stop", "plan_stop_pct", "plan_stop_basis", "plan_entry_lo", "plan_entry_hi",
                   "above_ema50", "confluence", "confluence_n", "dist_atr", "ur", "bo", "gu")
        cand = {"pullback": [], "ur": [], "breakout": [], "gapup": []}
        gu_rejected = []
        for r in rows:
            if (r.get("price") or 0) < SETUP_MIN_PRICE or (r.get("avg_dollar_vol") or 0) < SETUP_MIN_DOLLAR_VOL:
                continue
            cap = r.get("market_cap")
            if cap is not None and cap < SETUP_MIN_CAP:
                continue
            rs = r.get("rs_rating") or 0
            if r.get("stage") == 2 and rs >= SETUP_PB_RS_MIN and (r.get("confluence_n") or 0) >= SETUP_PB_MIN_LAYERS:
                cand["pullback"].append(r)
            if r.get("ur") and r.get("above_s200"):
                cand["ur"].append(r)
            if r.get("bo"):
                cand["breakout"].append(r)
            if r.get("gu") and r.get("stage") == 2 and rs >= SETUP_GU_RS_MIN:
                if r["gu"].get("valid", True):
                    cand["gapup"].append(r)
                else:
                    gu_rejected.append(r)          # v7.3: gap closed (or close in the lower half) -> log only

        def near(r):
            d = [abs(v) for v in (r.get("dist_atr") or {}).values() if v is not None]
            return min(d) if d else 99
        cand["pullback"].sort(key=lambda r: (-(r.get("confluence_n") or 0), near(r)))
        cand["ur"].sort(key=lambda r: (r["ur"].get("age", 99), -(r["ur"].get("vol_x") or 0)))
        cand["breakout"].sort(key=lambda r: (r["bo"].get("age", 99), -(r.get("rs_rating") or 0)))
        cand["gapup"].sort(key=lambda r: (not r["gu"].get("held"), r["gu"].get("age", 99), -(r.get("rs_rating") or 0)))

        for k, lst in cand.items():
            setups[k] = [{f: r.get(f) for f in SU_KEYS} for r in lst[:SETUP_CAP]]
            for r in lst[:SETUP_CAP]:
                setup_of.setdefault(r["ticker"], []).append({"pullback": "pb", "ur": "ur", "breakout": "bo", "gapup": "gu"}[k])
        # ── 8b. Bottom checklist (v7.5): new-52w-low count + RS-houders ──
        bottom = None
        holders_all = []
        try:
            try:
                with open(REGIME_PATH) as f:
                    rgb = (json.load(f) or {}).get("bottom")
            except Exception:
                rgb = None
            # new 52w lows per session across the analysed (liquid) names
            nl_cnt, nl_n = {}, 0
            for r in rows:
                df = full.get(r["ticker"])
                if df is None or len(df) < 260:
                    continue
                lo = df["Low"]
                flag = (lo <= lo.rolling(252, min_periods=252).min()).iloc[-NL_SESSIONS:]
                nl_n += 1
                for ts, v in flag.items():
                    if v:
                        k = str(ts.date()); nl_cnt[k] = nl_cnt.get(k, 0) + 1
            ref_df = full.get("SPY")
            if ref_df is None:
                ref_df = max((x for x in full.values() if x is not None), key=len)
            spy_idx = ref_df.index[-NL_SESSIONS:]
            nl_series = [{"d": str(ts.date()), "n": nl_cnt.get(str(ts.date()), 0)} for ts in spy_idx]
            nl_of = {x["d"]: x["n"] for x in nl_series}
            s3 = None
            if rgb and rgb.get("test"):
                ref_n = max([nl_of.get(x, 0) for x in rgb["ref"]["dates"]] + [nl_of.get(rgb["anchor"]["date"], 0)])
                t_n = nl_of.get(rgb["test"]["date"])
                s3 = {"ref_n": ref_n, "test_n": t_n, "fewer": t_n is not None and t_n < ref_n}
            # RS-houders: leaders near their highs; with an index undercut, did they hold their own low?
            lt = (rgb or {}).get("leader_test")
            for r in rows:
                if (r.get("price") or 0) < SETUP_MIN_PRICE or (r.get("avg_dollar_vol") or 0) < SETUP_MIN_DOLLAR_VOL:
                    continue
                cap = r.get("market_cap")
                if cap is not None and cap < SETUP_MIN_CAP:
                    continue
                if (r.get("rs_rating") or 0) < RH_RS_MIN or (r.get("off_52w_high") is None) \
                        or r["off_52w_high"] < -RH_OFF_HIGH_MAX or not r.get("above_ema50"):
                    continue
                h = {"held": None, "ref_low": None, "test_low": None, "vs_ref_pct": None}
                df = full.get(r["ticker"])
                if lt and df is not None and len(df) > 30:
                    ix = df.index
                    p = ix.searchsorted(pd.Timestamp(lt["prior"]))
                    a = ix.searchsorted(pd.Timestamp(lt["undercut"]))
                    if p < len(ix) and a < len(ix) and a > p:
                        ref = float(df["Low"].iloc[max(0, p - RH_REF_PAD):min(a, p + RH_REF_PAD + 1)].min())
                        tst = float(df["Low"].iloc[a:].min())
                        h = {"held": bool(tst > ref), "ref_low": round(ref, 2), "test_low": round(tst, 2),
                             "vs_ref_pct": round((float(df["Close"].iloc[-1]) / ref - 1) * 100, 1)}
                holders_all.append(dict(r, rh=h))
            holders_all.sort(key=lambda r: (r["rh"]["held"] is not True, -(r.get("rs_rating") or 0),
                                            r.get("off_52w_high") is None, -(r.get("off_52w_high") or -99)))
            n_held = sum(1 for r in holders_all if r["rh"]["held"] is True)
            n_broke = sum(1 for r in holders_all if r["rh"]["held"] is False)
            per_ind, shown = {}, []
            for r in holders_all:
                k = r.get("industry") or "?"
                if per_ind.get(k, 0) >= RH_PER_INDUSTRY:
                    continue
                per_ind[k] = per_ind.get(k, 0) + 1
                shown.append(r)
                if len(shown) >= SETUP_CAP:
                    break
            setups["holders"] = [dict({f: r.get(f) for f in SU_KEYS}, off_52w_high=r.get("off_52w_high"), rh=r["rh"])
                                 for r in shown]
            cand["holders"] = holders_all
            bottom = {"newlows": {"universe": nl_n, "series": nl_series}, "s3": s3,
                      "holders": {"n": len(holders_all), "n_held": n_held, "n_broke": n_broke,
                                  "test": lt, "ok": bool(lt) and n_held >= RH_HOLD_MIN, "hold_min": RH_HOLD_MIN}}
            print(f"  Bottom: new lows today {nl_series[-1]['n'] if nl_series else '?'} / {nl_n} · "
                  f"RS-houders {len(holders_all)} ({n_held} held, {n_broke} broke)" + (f" · test {lt['undercut']}" if lt else ""))
        except Exception as e:
            print(f"  (bottom checklist skipped: {e})")

        su_payload = {"updated_at": payload["updated_at"],
                      "bottom": bottom,
                      "n_total": {k: len(v) for k, v in cand.items()},
                      "n_gapup_held": sum(1 for r in cand["gapup"] if r["gu"].get("held")),
                      "n_gapup_rejected": len(gu_rejected),
                      "thresholds": {"cap": SETUP_CAP, "min_price": SETUP_MIN_PRICE,
                                     "min_dollar_vol": SETUP_MIN_DOLLAR_VOL, "min_cap": SETUP_MIN_CAP,
                                     "pullback": {"stage": 2, "rs_min": SETUP_PB_RS_MIN, "min_layers": SETUP_PB_MIN_LAYERS},
                                     "ur": {"min_held": 5, "min_vol_x": 2.0, "max_under_pct": 7.0, "max_age_days": 5,
                                            "gate": "close > SMA200"},
                                     "breakout": {"pivot": "260-session high, >= 25 sessions old", "max_depth_pct": 35,
                                                  "max_ext_pct": 5, "min_vol_x": 1.5, "max_age_days": 5},
                                     "gapup": {"gap": "open - prior high >= 0.75 x ATR(40)", "min_vol_x": 1.5,
                                               "close": "close > prior high and in the upper half of the range",
                                               "stage": 2, "rs_min": SETUP_GU_RS_MIN, "max_age_days": 5},
                                     "holders": {"rs_min": RH_RS_MIN, "off_high_max": RH_OFF_HIGH_MAX, "above": "EMA50",
                                                 "per_industry": RH_PER_INDUSTRY, "hold_min": RH_HOLD_MIN}},
                      "lists": setups}
        with open(SETUPS_PATH, "w") as f:
            json.dump(su_payload, f, separators=(",", ":"))
        print(f"  Saved {SETUPS_PATH} (" + ", ".join(f"{k} {len(setups[k])}/{len(cand[k])}" for k in setups) + ")")

        # Live measurement log: one row per NEW hit (age 0), deduplicated on (date, ticker, setup).
        try:
            log = []
            if os.path.exists(SETUPS_LOG_PATH):
                with open(SETUPS_LOG_PATH) as f:
                    log = json.load(f).get("rows", [])
            model_regime = None
            try:
                with open("data/regime.json") as f:
                    model_regime = json.load(f).get("model", {}).get("regime")
            except Exception:
                pass
            seen_keys = {(x["date"], x["ticker"], x["setup"]) for x in log}
            shown_pb = {id(r) for r in cand["pullback"][:SETUP_CAP]}
            added = 0
            for k, lst in cand.items():
                if k == "holders":
                    continue                             # a watch list, not a setup with an event date
                for r in lst:
                    obj = {"pullback": None, "ur": r.get("ur"), "breakout": r.get("bo"), "gapup": r.get("gu")}[k]
                    if k == "pullback":
                        d = today_s                      # a pullback has no event date: log once per day
                    else:
                        if not obj or obj.get("age") != 0:
                            continue
                        d = obj["date"]
                    key = (d, r["ticker"], k)
                    if key in seen_keys:
                        continue
                    if k == "pullback" and id(r) not in shown_pb:
                        continue                         # only log the pullbacks that were shown
                    ref = {"ur": "low", "breakout": "pivot", "gapup": "low"}.get(k)
                    row_log = {"date": d, "ticker": r["ticker"], "setup": k, "regime_model": model_regime,
                               "price": r.get("price"), "ref": obj.get(ref) if (obj and ref) else None,
                               "rs": r.get("rs_rating"), "stage": r.get("stage")}
                    if k == "gapup":
                        row_log["valid"] = True
                    log.append(row_log)
                    seen_keys.add(key); added += 1
            for r in gu_rejected:
                g = r.get("gu") or {}
                if g.get("age") != 0:
                    continue
                key = (g["date"], r["ticker"], "gapup")
                if key in seen_keys:
                    continue
                log.append({"date": g["date"], "ticker": r["ticker"], "setup": "gapup", "regime_model": model_regime,
                            "price": r.get("price"), "ref": g.get("low"), "rs": r.get("rs_rating"),
                            "stage": r.get("stage"), "valid": False})
                seen_keys.add(key); added += 1
            log = log[-SETUPS_LOG_MAX:]
            with open(SETUPS_LOG_PATH, "w") as f:
                json.dump({"updated_at": payload["updated_at"], "rows": log}, f, separators=(",", ":"))
            print(f"  Saved {SETUPS_LOG_PATH} (+{added}, {len(log)} total)")
        except Exception as e:
            print(f"  (setups log skipped: {e})")
    except Exception as e:
        print(f"  (setup module skipped: {e})")

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

        nt_route = {r["ticker"]: r["route"] for r in notable}
        for r in notable:
            tk = r["ticker"]
            if tk not in seen and tk in by_t:
                chart_rows.append(by_t[tk]); seen.add(tk); why[tk] = "notable"
        for tk in setup_of:
            if tk not in seen and tk in by_t:
                chart_rows.append(by_t[tk]); seen.add(tk); why[tk] = "setup"

        def picks_for(tk):
            return [{"id": th["id"], "name": th["name"], "rank": th["chart_tickers"].index(tk) + 1,
                     "label": th.get("label"), "rel_1w": th.get("rel_1w")}
                    for th in themes_out if tk in th["chart_tickers"]]

        # v7.1 "potential": which cards earn a place in the default view (TOP 45).
        # setup score + 3 per active setup in 05b (gap-up only while its low holds)
        # + 2 for a DNA pass + RS/25 - 3 when extended (ATR x 50 > 15), -1 above 10.
        def potential(r):
            act = sum(1 for k in setup_of.get(r["ticker"], [])
                      if k != "gu" or (r.get("gu") or {}).get("held"))
            ext = r.get("extension")
            return round((r.get("setup_score") or 0) + 3 * act + (2 if r.get("dna_pass") else 0)
                         + (r.get("rs_rating") or 0) / 25
                         - (3 if ext == "reduce" else 1 if ext == "caution" else 0), 1)

        def card(r, why_tk):
            tk = r["ticker"]
            return {"meta": {k: r.get(k) for k in CHART_META} | {
                        "themes": [th["id"] for th in themes_out if tk in th["chart_tickers"]],
                        "theme_picks": picks_for(tk),
                        "dna_rank": dna_rank.get(tk),
                        "early": r.get("early"), "early_score": r.get("early_score"),
                        "why": why_tk, "route": nt_route.get(tk),
                        "setups": setup_of.get(tk, []),
                        "dna_pass": r.get("dna_pass"), "potential": potential(r)},
                    "bars": chart_bars(full[tk])}

        charts = {}
        for r in chart_rows:
            if r["ticker"] in full:
                charts[r["ticker"]] = card(r, why.get(r["ticker"], "dna"))
        ranked = sorted(charts, key=lambda t: -charts[t]["meta"]["potential"])
        top = set(ranked[:CHART_SHOW])
        for i, t in enumerate(ranked):
            charts[t]["meta"]["top"] = t in top
            charts[t]["meta"]["pot_rank"] = i + 1
        with open(CHART_PATH, "w") as f:
            json.dump({"updated": payload["updated_at"], "n_core": len(charts), "n_top": len(top),
                       "charts": {t: charts[t] for t in ranked if t in top}}, f, separators=(",", ":"))
        # Everything else a table can point at: the other candidates, the scan leaders
        # (05 Market Scan), my universe (05 screener) and every theme basket name (03b).
        more = {t: charts[t] for t in ranked if t not in top}
        extra_rows = list(out_rows) + [by_t[t] for t in universe if t in by_t] \
                     + [by_t[t] for th in themes_def for t in th["tickers"] if t in by_t]
        for r in extra_rows:
            tk = r["ticker"]
            if tk in charts or tk in more or tk not in full:
                continue
            c = card(r, "extra"); c["meta"]["top"] = False
            more[tk] = c
        with open(CHART_MORE_PATH, "w") as f:
            json.dump({"updated": payload["updated_at"], "charts": more}, f, separators=(",", ":"))
        n_by = {w: sum(1 for t in charts if charts[t]["meta"]["why"] == w) for w in ("dna", "theme", "notable", "setup")}
        print(f"  Saved {CHART_PATH} ({len(top)} of {len(charts)} candidates by potential: {n_by['dna']} DNA, "
              f"{n_by['theme']} theme, {n_by['notable']} notable, {n_by['setup']} setup) + "
              f"{CHART_MORE_PATH} ({len(more)} tickers)")
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
