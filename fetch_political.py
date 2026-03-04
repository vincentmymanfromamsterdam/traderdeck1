"""
TRADERDECK — Political Alpha Fetcher
Primary source: GitHub raw Senate data (confirmed working in GitHub Actions)
Secondary: House data (multiple URL variants tried)
"""

import json, os, sys, datetime, time, re, math
import requests

OUTPUT_PATH = "data/political_alpha.json"
TIMEOUT     = 30
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# ─── DATA SOURCES ────────────────────────────────────────────────────────────

SENATE_URLS = [
    # Primary — confirmed working in GitHub Actions
    "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json",
]

HOUSE_URLS = [
    # These returned 404 last check — keeping for retry with different branches
    "https://raw.githubusercontent.com/timothycarambat/house-stock-watcher-data/master/data/all_transactions.json",
    "https://raw.githubusercontent.com/timothycarambat/house-stock-watcher-data/main/data/all_transactions.json",
    # Alternative community mirror
    "https://raw.githubusercontent.com/ryanmio/congressional-stock-data/main/house/all_transactions.json",
]

# ─── FETCH ───────────────────────────────────────────────────────────────────

def fetch_json(urls, label):
    for url in urls:
        try:
            print(f"  Trying {label}: {url[:72]}...", end=" ", flush=True)
            r = requests.get(url, headers={"User-Agent": UA},
                             timeout=TIMEOUT, allow_redirects=True)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and len(data) > 10:
                    print(f"OK ({len(data):,} records)")
                    return data
                elif isinstance(data, dict):
                    # Some repos wrap in {"data": [...]}
                    inner = data.get("data") or data.get("transactions") or []
                    if inner:
                        print(f"OK ({len(inner):,} records, wrapped)")
                        return inner
                print(f"Unexpected format: {str(data)[:60]}")
            else:
                print(f"HTTP {r.status_code}")
        except Exception as e:
            print(f"FAILED: {str(e)[:80]}")
    return None

# ─── NORMALIZE ───────────────────────────────────────────────────────────────

def normalize_senate(data):
    """
    Senate record structure (timothycarambat):
    {
        "senator": "John Smith",
        "transaction_date": "2024-01-15",
        "ticker": "AAPL",
        "type": "Purchase",
        "amount": "$1,001 - $15,000"
    }
    """
    cutoff = days_ago(90)
    trades = []
    for row in (data or []):
        ticker = clean_ticker(row.get("ticker", ""))
        if not ticker:
            continue
        d = parse_date(row.get("transaction_date", row.get("date", "")))
        if not d or d < cutoff:
            continue
        ty_raw = str(row.get("type", row.get("transaction_type", ""))).lower()
        ty = tx_type(ty_raw)
        amount = str(row.get("amount", ""))
        name = str(row.get("senator", row.get("name", row.get("first_name", "Unknown"))))
        if row.get("last_name"):
            name = f"{name} {row['last_name']}".strip()
        trades.append({
            "politician": name,
            "chamber": "Senate",
            "ticker": ticker,
            "date": d,
            "type": ty,
            "size_raw": amount,
            "capital": estimate_capital(amount),
        })
    return trades


def normalize_house(data):
    """
    House record structure (timothycarambat):
    {
        "representative": "Nancy Pelosi",
        "transaction_date": "2024-01-15",
        "ticker": "NVDA",
        "type": "purchase",
        "amount": "$500,001 - $1,000,000"
    }
    """
    cutoff = days_ago(90)
    trades = []
    for row in (data or []):
        ticker = clean_ticker(row.get("ticker", ""))
        if not ticker:
            continue
        d = parse_date(row.get("transaction_date", row.get("date", "")))
        if not d or d < cutoff:
            continue
        ty_raw = str(row.get("type", row.get("transaction_type", ""))).lower()
        ty = tx_type(ty_raw)
        amount = str(row.get("amount", ""))
        name = str(row.get("representative", row.get("name", "Unknown")))
        trades.append({
            "politician": name,
            "chamber": "House",
            "ticker": ticker,
            "date": d,
            "type": ty,
            "size_raw": amount,
            "capital": estimate_capital(amount),
        })
    return trades

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def days_ago(n):
    return datetime.date.today() - datetime.timedelta(days=n)

def clean_ticker(raw):
    t = re.sub(r':[A-Z]+$', '', str(raw or "")).upper().strip()
    if not t or t in ('--', 'N/A', 'NONE', '') or len(t) > 6:
        return None
    if any(c in t for c in (' ', '/', '\\')):
        return None
    return t

def parse_date(s):
    if not s:
        return None
    s = str(s).strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None

def tx_type(raw):
    if any(w in raw for w in ("purchase", "buy", "exchange_false")):
        return "buy"
    if any(w in raw for w in ("sale", "sell")):
        return "sell"
    return "other"

def estimate_capital(s):
    if not s:
        return 0
    nums = [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", str(s))]
    if len(nums) >= 2:
        return (nums[0] + nums[1]) / 2
    if len(nums) == 1:
        return nums[0]
    return 0

# ─── AGGREGATE ───────────────────────────────────────────────────────────────

def aggregate(trades):
    cutoff_30 = days_ago(30)
    agg = {}
    for t in trades:
        k = t["ticker"]
        if k not in agg:
            agg[k] = {
                "ticker": k, "issuer": k,
                "politicians_90": set(), "politicians_30": set(),
                "capital_buy": 0, "capital_sell": 0,
                "buy_count": 0, "sell_count": 0,
                "trades": [],
            }
        a = agg[k]
        a["politicians_90"].add(t["politician"])
        if t["date"] >= cutoff_30:
            a["politicians_30"].add(t["politician"])
        if t["type"] == "buy":
            a["capital_buy"]  += t["capital"]; a["buy_count"]  += 1
        elif t["type"] == "sell":
            a["capital_sell"] += t["capital"]; a["sell_count"] += 1
        a["trades"].append({
            "name": t["politician"], "chamber": t["chamber"],
            "date": t["date"].isoformat(), "type": t["type"],
            "amount": t["size_raw"], "amount_est": t["capital"],
        })

    for a in agg.values():
        a["pol_count_90"] = len(a["politicians_90"])
        a["pol_count_30"] = len(a["politicians_30"])
        a["politicians_90"] = sorted(a["politicians_90"])
        a["trades"] = sorted(a["trades"], key=lambda x: x["date"], reverse=True)[:10]
    return agg


def score_item(a, f13=0):
    p = a["pol_count_90"]
    c = a["capital_buy"] + a["capital_sell"]
    br = min(p / 15 * 100, 100)
    cs = min(math.log10(max(c, 1000)) / math.log10(2_000_000) * 100, 100) if c else 0
    fs = min(f13 / 1000 * 100, 100) if f13 else 0
    tot = round(br * 0.40 + cs * 0.30 + fs * 0.30)
    return {
        "score": max(1, tot),
        "score_breadth":  round(br * 0.40),
        "score_capital":  round(cs * 0.30),
        "score_13f":      round(fs * 0.30),
    }


def direction(a):
    b, s = a["buy_count"], a["sell_count"]
    if not b and not s: return "unknown"
    if not s: return "buy"
    if not b: return "sell"
    r = b / (b + s)
    return "buy" if r >= 0.7 else "sell" if r <= 0.3 else "mixed"


def trend(p30, p90):
    if not p90: return "flat"
    rn = p30 / 30
    ro = (p90 - p30) / 60 if p90 > p30 else 0
    return "up" if rn > ro * 1.3 else "down" if rn < ro * 0.7 else "flat"


def get_sectors(tickers):
    print(f"  Fetching sectors for {len(tickers)} tickers...", end=" ", flush=True)
    result = {}
    try:
        import yfinance as yf
        for t in tickers:
            try:
                result[t] = yf.Ticker(t).info.get("sector", "Unknown") or "Unknown"
            except Exception:
                result[t] = "Unknown"
        print("OK")
    except Exception as e:
        print(f"skipped ({e})")
        result = {t: "Unknown" for t in tickers}
    return result

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 58)
    print("  TRADERDECK — Political Alpha")
    print("=" * 58)
    print(f"  {datetime.date.today()} | Window: 30D + 90D\n")

    # Load Carnivore tickers
    carnivore_sr, carnivore_lt = set(), set()
    try:
        with open("data/carnivore_portfolios.json") as f:
            cp = json.load(f)
        carnivore_sr = {p["ticker"] for p in cp.get("sector_rotation", [])}
        carnivore_lt = {p["ticker"] for p in cp.get("long_term", [])}
        print(f"  Carnivore: {len(carnivore_sr)} SR + {len(carnivore_lt)} LT\n")
    except Exception as e:
        print(f"  Carnivore: not loaded ({e})\n")

    # Load cached 13F data from previous run
    cached_13f = {}
    try:
        with open(OUTPUT_PATH) as f:
            old = json.load(f)
        for item in old.get("full_scan", []) + old.get("portfolio", []):
            if item.get("funds_13f"):
                cached_13f[item["ticker"]] = item["funds_13f"]
        print(f"  Cached 13F entries: {len(cached_13f)}\n")
    except Exception:
        pass

    # ── Fetch ───────────────────────────────────────────────────────────────
    senate_raw = fetch_json(SENATE_URLS, "Senate")
    house_raw  = fetch_json(HOUSE_URLS,  "House")

    trades = []
    if senate_raw:
        trades += normalize_senate(senate_raw)
        print(f"  Senate trades (90D window): {len(trades)}")
    if house_raw:
        house_trades = normalize_house(house_raw)
        trades += house_trades
        print(f"  House  trades (90D window): {len(house_trades)}")

    if not trades:
        print("\n  ERROR: No trades fetched from any source — aborting")
        sys.exit(1)

    print(f"\n  Total trades in window: {len(trades)}")

    # ── Aggregate & score ───────────────────────────────────────────────────
    agg = aggregate(trades)
    print(f"  Unique tickers: {len(agg)}")

    top40   = sorted(agg.keys(), key=lambda t: agg[t]["pol_count_90"], reverse=True)[:40]
    sectors = get_sectors(top40)

    items = []
    for ticker, a in agg.items():
        f13 = cached_13f.get(ticker, 0)
        sc  = score_item(a, f13)
        carn = ("SR" if ticker in carnivore_sr else
                "LT" if ticker in carnivore_lt else None)
        items.append({
            "ticker":         ticker,
            "issuer":         a["issuer"],
            "sector":         sectors.get(ticker, "Unknown"),
            "score":          sc["score"],
            "score_breadth":  sc["score_breadth"],
            "score_capital":  sc["score_capital"],
            "score_13f":      sc["score_13f"],
            "direction":      direction(a),
            "pol_count_30":   a["pol_count_30"],
            "pol_count_90":   a["pol_count_90"],
            "politicians_90": a["politicians_90"][:12],
            "capital_buy":    round(a["capital_buy"]),
            "capital_sell":   round(a["capital_sell"]),
            "net_flow":       round(a["capital_buy"] - a["capital_sell"]),
            "funds_13f":      f13,
            "has_13f":        f13 > 0,
            "carnivore":      carn,
            "trend":          trend(a["pol_count_30"], a["pol_count_90"]),
            "trades":         a["trades"][:8],
            "buy_count":      a["buy_count"],
            "sell_count":     a["sell_count"],
        })

    items.sort(key=lambda x: x["score"], reverse=True)
    portfolio_items = [i for i in items if i["carnivore"]]
    full_scan_items = items[:50]
    active_pols     = len({p for a in agg.values() for p in a["politicians_90"]})

    payload = {
        "last_updated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "source":       "Senate: github/timothycarambat | House: github/timothycarambat",
        "window_days":  90,
        "summary": {
            "active_politicians": active_pols,
            "total_buy_capital":  round(sum(i["capital_buy"]  for i in items)),
            "total_sell_capital": round(sum(i["capital_sell"] for i in items)),
            "carnivore_overlap":  len(portfolio_items),
            "f13_confirmed":      len([i for i in full_scan_items if i["has_13f"]]),
            "top_ticker":         items[0]["ticker"] if items else "—",
            "top_score":          items[0]["score"]  if items else 0,
        },
        "portfolio":  portfolio_items,
        "full_scan":  full_scan_items,
    }

    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"\n  Active politicians : {active_pols}")
    print(f"  Carnivore overlap  : {len(portfolio_items)}")
    print(f"  Top 5 tickers      : {', '.join(i['ticker'] for i in full_scan_items[:5])}")
    print(f"  Top conviction     : {payload['summary']['top_ticker']} (score {payload['summary']['top_score']})")
    print(f"\n  Saved → {OUTPUT_PATH}")
    print("=" * 58 + "\n")


if __name__ == "__main__":
    main()
