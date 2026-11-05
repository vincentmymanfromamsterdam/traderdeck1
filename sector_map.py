"""
TRADERDECK — Symbol directory + sector / industry / market cap
===============================================================
Primary source: Nasdaq stock screener (one request, all US-listed names, with
sector, industry and market cap). Cached per day in data/sector_map.json.
Fallback: the previous day's cache, then yfinance per-ticker lookup for misses.

  from sector_map import load_directory, SECTOR_ETF
  d = load_directory()
  d["NVDA"] -> {"name": "NVIDIA Corporation", "sector": "Technology",
                "industry": "Semiconductors", "etf": "XLK", "market_cap": 4.98e12,
                "country": "United States", "last": 206.69}
"""

import datetime as dt
import json
import os
import re
import time

import requests

CACHE_PATH = "data/sector_map.json"
SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Nasdaq sector name -> SPDR sector ETF used in TRADERDECK section 03
SECTOR_ETF = {
    "Technology":             "XLK",
    "Health Care":            "XLV",
    "Finance":                "XLF",
    "Consumer Discretionary": "XLY",
    "Consumer Staples":       "XLP",
    "Energy":                 "XLE",
    "Industrials":            "XLI",
    "Basic Materials":        "XLB",
    "Real Estate":            "XLRE",
    "Utilities":              "XLU",
    "Telecommunications":     "XLC",
    # yfinance names (fallback path)
    "Healthcare":             "XLV",
    "Financial Services":     "XLF",
    "Consumer Cyclical":      "XLY",
    "Consumer Defensive":     "XLP",
    "Communication Services": "XLC",
}

BAD_NAME_WORDS = (
    " etf", " fund", " trust", " warrant", " unit", " right", " preferred",
    " depositary", " notes", " debenture", " acquisition corp", " acquisition co",
    " capital corp", " bond", " etn", "ishares", "proshares", "vanguard",
    "direxion", "invesco", "spdr", "wisdomtree", "global x", "first trust",
)


def _num(s):
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(s))) if s not in (None, "", "NA") else None
    except Exception:
        return None


def _clean_name(n):
    n = re.sub(r"\s+(Common Stock|Ordinary Shares|Class [A-C] Common Stock|American Depositary Shares.*|Common Shares)$", "", n, flags=re.I)
    return n.strip()[:40]


def fetch_screener(timeout=40):
    r = requests.get(SCREENER_URL, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    rows = r.json()["data"]["rows"]
    out = {}
    for x in rows:
        sym = (x.get("symbol") or "").strip()
        name = x.get("name") or ""
        nl = " " + name.lower()
        if not sym or not sym.isalpha() or len(sym) > 5:
            continue                      # drops warrants/units/rights/preferreds (suffix symbols)
        if any(w in nl for w in BAD_NAME_WORDS):
            continue
        sector = x.get("sector") or None
        out[sym] = {
            "name": _clean_name(name),
            "sector": sector,
            "industry": (x.get("industry") or "Other") if sector else None,
            "etf": SECTOR_ETF.get(sector) if sector else None,
            "market_cap": _num(x.get("marketCap")),
            "country": x.get("country") or None,
            "last": _num(x.get("lastsale")),
        }
    return out


def load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, separators=(",", ":"), sort_keys=True)


def load_directory(verbose=True):
    """Return {ticker: {...}} for all US-listed common stocks. Refreshes once per day."""
    today = dt.date.today().isoformat()
    cache = load_cache()
    if cache.get("_date") == today and cache.get("symbols"):
        if verbose:
            print(f"  Directory: {len(cache['symbols'])} symbols (cached today)")
        return cache["symbols"]
    for attempt in range(3):
        try:
            syms = fetch_screener()
            if len(syms) > 2000:
                save_cache({"_date": today, "_source": "nasdaq screener", "symbols": syms})
                if verbose:
                    classified = sum(1 for v in syms.values() if v["etf"])
                    print(f"  Directory: {len(syms)} symbols from Nasdaq screener, {classified} with sector")
                return syms
        except Exception as e:
            if verbose:
                print(f"  Nasdaq screener attempt {attempt+1} failed: {e}")
            time.sleep(5)
    if cache.get("symbols"):
        if verbose:
            print(f"  Directory: using cached list from {cache.get('_date')} ({len(cache['symbols'])} symbols)")
        return cache["symbols"]
    raise RuntimeError("No symbol directory available (Nasdaq screener down and no cache)")


def yf_fallback(tickers, max_new=200, verbose=True):
    """Per-ticker yfinance lookup for names the screener left unclassified. Slow; rate-limited."""
    import yfinance as yf
    out = {}
    for i, t in enumerate(tickers[:max_new]):
        try:
            info = yf.Ticker(t).get_info() or {}
            sec = info.get("sector")
            if sec:
                out[t] = {"sector": sec, "industry": info.get("industry") or "Other", "etf": SECTOR_ETF.get(sec)}
        except Exception:
            time.sleep(1)
        if verbose and (i + 1) % 50 == 0:
            print(f"    yf fallback {i+1}/{min(len(tickers), max_new)}", flush=True)
    return out


if __name__ == "__main__":
    d = load_directory()
    for t in ("NVDA", "LLY", "XOM", "RKLB", "SNDK"):
        print(t, d.get(t))


# ─────────────────────────────────────────────────────────
#  TICKER HEALTH — skip names that failed on consecutive nights
# ─────────────────────────────────────────────────────────
HEALTH_PATH = "data/ticker_health.json"
MAX_MISSES = 2          # excluded after this many consecutive nights without data
RETRY_AFTER_DAYS = 30   # ...but re-tried once a month in case it was a temporary outage


def load_health():
    if os.path.exists(HEALTH_PATH):
        try:
            with open(HEALTH_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_health(h):
    os.makedirs(os.path.dirname(HEALTH_PATH), exist_ok=True)
    with open(HEALTH_PATH, "w") as f:
        json.dump(h, f, indent=0, sort_keys=True)


def exclude_dead(tickers, verbose=True, label=""):
    """Drop tickers that missed MAX_MISSES nights in a row (unless due for a monthly retry)."""
    h = load_health()
    today = dt.date.today()
    keep, dead = [], []
    for t in tickers:
        rec = h.get(t)
        if rec and rec.get("misses", 0) >= MAX_MISSES:
            last = rec.get("last_miss")
            try:
                days = (today - dt.date.fromisoformat(last)).days if last else 999
            except Exception:
                days = 999
            if days < RETRY_AFTER_DAYS:
                dead.append(t)
                continue
        keep.append(t)
    if dead and verbose:
        print(f"  Skipping {len(dead)} dead tickers{(' for ' + label) if label else ''}: {', '.join(dead[:12])}{'…' if len(dead) > 12 else ''}")
    return keep


def record_results(requested, found):
    """After a download: reset misses for found tickers, increment for the rest."""
    h = load_health()
    today = dt.date.today().isoformat()
    found = set(found)
    for t in requested:
        if t in found:
            if t in h:
                del h[t]
        else:
            rec = h.get(t, {"misses": 0})
            if rec.get("last_miss") != today:
                rec["misses"] = rec.get("misses", 0) + 1
                rec["last_miss"] = today
            h[t] = rec
    save_health(h)
    return h


def get_benchmark_tickers(n=500, fallback=None):
    """Top-n US-listed stocks by market cap from the directory (a live S&P 500 proxy)."""
    try:
        d = load_directory(verbose=False)
        rows = [(t, v["market_cap"]) for t, v in d.items()
                if v.get("market_cap") and (v.get("country") in (None, "", "United States"))]
        rows.sort(key=lambda x: x[1], reverse=True)
        tickers = [t for t, _ in rows[:n]]
        if len(tickers) >= n * 0.8:
            return exclude_dead(tickers, label="benchmark")
    except Exception as e:
        print(f"  (benchmark from directory failed: {e})")
    return exclude_dead(list(fallback or []), label="benchmark")


# ─────────────────────────────────────────────────────────
#  EARNINGS CALENDAR — Nasdaq, one request per day ahead
# ─────────────────────────────────────────────────────────
EARNINGS_PATH = "data/earnings.json"
EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings?date={d}"
EARNINGS_DAYS_AHEAD = 21


def load_earnings(verbose=True):
    """Return {ticker: 'YYYY-MM-DD'} for the next ~3 weeks. Cached per day."""
    today = dt.date.today()
    cache = {}
    if os.path.exists(EARNINGS_PATH):
        try:
            with open(EARNINGS_PATH) as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    if cache.get("_date") == today.isoformat() and cache.get("by_ticker"):
        return cache["by_ticker"]
    by_ticker, days_ok = {}, 0
    for i in range(EARNINGS_DAYS_AHEAD):
        d = today + dt.timedelta(days=i)
        if d.weekday() >= 5:
            continue
        try:
            r = requests.get(EARNINGS_URL.format(d=d.isoformat()), headers=HEADERS, timeout=20)
            r.raise_for_status()
            rows = ((r.json() or {}).get("data") or {}).get("rows") or []
            for x in rows:
                sym = (x.get("symbol") or "").strip()
                if sym and sym not in by_ticker:
                    by_ticker[sym] = d.isoformat()
            days_ok += 1
        except Exception as e:
            if verbose:
                print(f"    earnings {d}: {e}")
        time.sleep(0.4)
    if days_ok >= 5:
        os.makedirs("data", exist_ok=True)
        with open(EARNINGS_PATH, "w") as f:
            json.dump({"_date": today.isoformat(), "days_fetched": days_ok, "by_ticker": by_ticker}, f, separators=(",", ":"))
        if verbose:
            print(f"  Earnings calendar: {len(by_ticker)} tickers over {days_ok} trading days")
        return by_ticker
    if verbose:
        print("  Earnings calendar: fetch failed, using cache" if cache.get("by_ticker") else "  Earnings calendar: unavailable")
    return cache.get("by_ticker", {})
