"""
TRADERDECK — Political Alpha Fetcher
Source: bff.capitoltrades.com (unofficial JSON API)
No API key required.
"""

import json, os, sys, datetime, time, re, math
import requests

OUTPUT_PATH = "data/political_alpha.json"
API_BASE    = "https://bff.capitoltrades.com"

# ── Headers that mimic the frontend ──────────────────────────────────────────
HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Content-Type":    "application/json",
    "Origin":          "https://www.capitoltrades.com",
    "Referer":         "https://www.capitoltrades.com/",
    "DNT":             "1",
    "Connection":      "keep-alive",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-site",
    "Cache-Control":   "max-age=0",
}

# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def days_ago(n):
    return datetime.date.today() - datetime.timedelta(days=n)

def parse_date(s):
    if not s:
        return None
    s = str(s)[:10]
    try:
        return datetime.date.fromisoformat(s)
    except Exception:
        return None

def estimate_capital(size_str):
    """Convert STOCK Act range to midpoint estimate."""
    if not size_str:
        return 0
    nums = [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", str(size_str))]
    if len(nums) >= 2:
        return (nums[0] + nums[1]) / 2
    if len(nums) == 1:
        return nums[0]
    s = str(size_str).lower()
    if "1,000,000" in s or "1000000" in s:
        return 2_500_000
    return 0

# ─────────────────────────────────────────────────────────────────────────────
#  FETCH
# ─────────────────────────────────────────────────────────────────────────────

def fetch_page(session, page, page_size=100):
    """Fetch one page from bff.capitoltrades.com/trades."""
    params = {
        "page":     page,
        "pageSize": page_size,
    }
    url = f"{API_BASE}/trades"
    r = session.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()

def fetch_all_trades(max_pages=35):
    """Paginate until 90-day window is covered."""
    cutoff_90 = days_ago(90)
    cutoff_30 = days_ago(30)

    session = requests.Session()
    # Warm-up call (sets cookies)
    try:
        session.get(f"{API_BASE}/trades", headers=HEADERS, timeout=15)
    except Exception:
        pass

    all_trades = []
    total_items = None

    print(f"  Fetching bff.capitoltrades.com (≤{max_pages} pages × 100 trades)...")

    for page in range(1, max_pages + 1):
        try:
            data = fetch_page(session, page)
        except Exception as e:
            print(f"\n    Page {page} error: {e}")
            break

        items = data.get("data", [])
        if not items:
            print(f"\n    Page {page}: empty response — stopping")
            break

        if total_items is None:
            total_items = data.get("meta", {}).get("paging", {}).get("totalItems", 0)

        # Each item: pubDate, txDate, politician{}, issuer{ticker, name}, type, size
        reached_cutoff = False
        for item in items:
            tx_date = parse_date(item.get("txDate") or item.get("pubDate"))
            if tx_date is None:
                continue
            if tx_date < cutoff_90:
                reached_cutoff = True
                continue

            # Extract ticker — capitoltrades uses issuer.ticker
            ticker = ""
            issuer = item.get("issuer") or {}
            if isinstance(issuer, dict):
                ticker = issuer.get("ticker") or ""
            elif isinstance(issuer, str):
                ticker = issuer

            # Strip exchange suffix
            ticker = re.sub(r":[A-Z]+$", "", ticker).strip().upper()
            if not ticker or ticker in ("--", "N/A") or len(ticker) > 6:
                continue

            pol = item.get("politician") or {}
            pol_name = ""
            if isinstance(pol, dict):
                pol_name = pol.get("name") or (
                    (pol.get("firstName") or "") + " " + (pol.get("lastName") or "")
                ).strip()
            elif isinstance(pol, str):
                pol_name = pol

            tx_type_raw = str(item.get("type") or item.get("txType") or "").lower()
            tx_type = (
                "buy"  if any(w in tx_type_raw for w in ("buy","purchase","exchange")) else
                "sell" if any(w in tx_type_raw for w in ("sell","sale")) else
                "other"
            )

            size_raw = str(item.get("size") or item.get("amount") or "")
            cap = estimate_capital(size_raw)

            all_trades.append({
                "politician": pol_name or "Unknown",
                "ticker":     ticker,
                "issuer":     (issuer.get("name") if isinstance(issuer, dict) else str(issuer)) or ticker,
                "date":       tx_date,
                "type":       tx_type,
                "size_raw":   size_raw,
                "capital":    cap,
            })

        sys.stdout.write(f"\r    Page {page} — {len(all_trades)} trades in window")
        sys.stdout.flush()

        if reached_cutoff:
            break

        time.sleep(0.6)

    print(f"\n  Total trades (90D): {len(all_trades)}")
    return all_trades, cutoff_30

# ─────────────────────────────────────────────────────────────────────────────
#  AGGREGATE & SCORE
# ─────────────────────────────────────────────────────────────────────────────

def aggregate(trades, cutoff_30):
    agg = {}
    for t in trades:
        k = t["ticker"]
        if k not in agg:
            agg[k] = {
                "ticker": k, "issuer": t["issuer"],
                "politicians_90": set(), "politicians_30": set(),
                "capital_buy": 0, "capital_sell": 0,
                "buy_count": 0, "sell_count": 0, "trades": [],
            }
        a = agg[k]
        a["politicians_90"].add(t["politician"])
        if t["date"] >= cutoff_30:
            a["politicians_30"].add(t["politician"])
        if t["type"] == "buy":
            a["capital_buy"]  += t["capital"]
            a["buy_count"]    += 1
        elif t["type"] == "sell":
            a["capital_sell"] += t["capital"]
            a["sell_count"]   += 1
        a["trades"].append({
            "name":       t["politician"],
            "date":       t["date"].isoformat(),
            "type":       t["type"],
            "amount":     t["size_raw"],
            "amount_est": t["capital"],
        })

    for a in agg.values():
        a["pol_count_90"] = len(a["politicians_90"])
        a["pol_count_30"] = len(a["politicians_30"])
        a["politicians_90"] = sorted(a["politicians_90"])
        a["politicians_30"] = sorted(a["politicians_30"])
        a["trades"] = sorted(a["trades"], key=lambda x: x["date"], reverse=True)[:10]
    return agg

def score(a, funds_13f=0):
    pol = a["pol_count_90"]
    cap = a["capital_buy"] + a["capital_sell"]
    breadth = min(pol / 15 * 100, 100)
    cap_s   = min(math.log10(max(cap, 1000)) / math.log10(2_000_000) * 100, 100) if cap else 0
    f13_s   = min(funds_13f / 1000 * 100, 100) if funds_13f else 0
    total   = round(breadth * 0.40 + cap_s * 0.30 + f13_s * 0.30)
    return {"score": max(1,total), "score_breadth": round(breadth*0.40),
            "score_capital": round(cap_s*0.30), "score_13f": round(f13_s*0.30)}

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

# ─────────────────────────────────────────────────────────────────────────────
#  SECTORS
# ─────────────────────────────────────────────────────────────────────────────

def get_sectors(tickers):
    print(f"  Fetching sectors for {len(tickers)} tickers...", end=" ", flush=True)
    result = {}
    try:
        import yfinance as yf
        for t in tickers:
            try:
                info = yf.Ticker(t).info
                result[t] = info.get("sector") or info.get("industryDisp") or "Unknown"
            except Exception:
                result[t] = "Unknown"
        print("OK")
    except Exception as e:
        print(f"WARN: {e}")
        result = {t: "Unknown" for t in tickers}
    return result

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "-"*55)
    print("  TRADERDECK — Political Alpha (Capitol Trades API)")
    print("-"*55)
    print(f"  Date: {datetime.date.today()} | Window: 30D + 90D\n")

    # Carnivore tickers
    carnivore_sr, carnivore_lt = set(), set()
    try:
        with open("data/carnivore_portfolios.json") as f:
            cp = json.load(f)
        carnivore_sr = {p["ticker"] for p in cp.get("sector_rotation", [])}
        carnivore_lt = {p["ticker"] for p in cp.get("long_term", [])}
        print(f"  Carnivore: {len(carnivore_sr)} SR + {len(carnivore_lt)} LT tickers")
    except Exception as e:
        print(f"  Carnivore load failed: {e}")

    # Cached 13F
    cached_13f = {}
    try:
        with open(OUTPUT_PATH) as f:
            old = json.load(f)
        for item in old.get("full_scan", []) + old.get("portfolio", []):
            if item.get("funds_13f"):
                cached_13f[item["ticker"]] = item["funds_13f"]
        print(f"  Cached 13F entries: {len(cached_13f)}")
    except Exception:
        print("  Cached 13F entries: 0")

    # Fetch
    raw_trades, cutoff_30 = fetch_all_trades()

    if not raw_trades:
        print("  ERROR: No trades fetched — aborting")
        sys.exit(1)

    # Aggregate + score
    agg    = aggregate(raw_trades, cutoff_30)
    print(f"  Unique tickers: {len(agg)}")

    top50  = sorted(agg.keys(), key=lambda t: agg[t]["pol_count_90"], reverse=True)[:40]
    sectors = get_sectors(top50)

    items = []
    for ticker, a in agg.items():
        funds = cached_13f.get(ticker, 0)
        sc    = score(a, funds)
        carn  = "SR" if ticker in carnivore_sr else ("LT" if ticker in carnivore_lt else None)
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
            "politicians_90": a["politicians_90"][:10],
            "capital_buy":    round(a["capital_buy"]),
            "capital_sell":   round(a["capital_sell"]),
            "net_flow":       round(a["capital_buy"] - a["capital_sell"]),
            "funds_13f":      funds,
            "has_13f":        funds > 0,
            "carnivore":      carn,
            "trend":          trend(a["pol_count_30"], a["pol_count_90"]),
            "trades":         a["trades"][:8],
            "buy_count":      a["buy_count"],
            "sell_count":     a["sell_count"],
        })

    items.sort(key=lambda x: x["score"], reverse=True)
    portfolio_items = [i for i in items if i["carnivore"]]
    full_scan_items = items[:50]

    active_pols = len({p for a in agg.values() for p in a["politicians_90"]})
    payload = {
        "last_updated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "source":       "bff.capitoltrades.com",
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

    print(f"\n  Portfolio overlap: {len(portfolio_items)} tickers")
    print(f"  Full scan top 3:   {', '.join(i['ticker'] for i in full_scan_items[:3])}")
    print(f"  Top score:         {payload['summary']['top_ticker']} ({payload['summary']['top_score']})")
    print(f"  Saved → {OUTPUT_PATH}")
    print("-"*55)

if __name__ == "__main__":
    main()
