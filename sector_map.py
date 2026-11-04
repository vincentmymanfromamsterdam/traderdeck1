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
