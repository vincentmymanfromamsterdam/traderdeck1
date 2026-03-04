"""
TRADERDECK — Political Alpha Fetcher
Source: capitoltrades.com (HTML scraping, server-side rendered)
No API key required.
"""

import json, os, sys, datetime, time, re
import requests
from bs4 import BeautifulSoup

OUTPUT_PATH = "data/political_alpha.json"
BASE_URL    = "https://www.capitoltrades.com/trades"
HEADERS     = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# ─────────────────────────────────────────────────────────────────────────────
#  DATE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def days_ago(n):
    return datetime.date.today() - datetime.timedelta(days=n)

def parse_date(s):
    """Parse dates like '24 Feb 2026', '4 Mar 2026'."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d %b %Y", "%b %d %Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

# ─────────────────────────────────────────────────────────────────────────────
#  SCRAPER
# ─────────────────────────────────────────────────────────────────────────────

def scrape_page(page_num):
    """Fetch one page of trades, return list of raw trade dicts."""
    url = f"{BASE_URL}?pageSize=96&page={page_num}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"    Page {page_num} fetch error: {e}")
        return [], False

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    if not table:
        print(f"    Page {page_num}: no table found")
        return [], False

    rows = table.find("tbody").find_all("tr") if table.find("tbody") else table.find_all("tr")[1:]
    trades = []
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 7:
            continue
        try:
            # Column order from site:
            # 0: Politician  1: Traded Issuer  2: Published  3: Traded  4: Filed After  5: Owner  6: Type  7: Size  8: Price
            politician_cell = cells[0]
            issuer_cell     = cells[1]
            traded_cell     = cells[3]
            type_cell       = cells[6]
            size_cell       = cells[7]

            # Politician name + party + chamber
            pol_name = politician_cell.get_text(separator=" ", strip=True)
            # Extract ticker from issuer cell
            ticker_span = issuer_cell.find(string=re.compile(r'^[A-Z]{1,5}(:[A-Z]+)?$'))
            if not ticker_span:
                # Try second line of issuer cell
                lines = [t.strip() for t in issuer_cell.stripped_strings]
                ticker = lines[1] if len(lines) > 1 else ""
            else:
                ticker = str(ticker_span).strip()

            # Clean ticker — remove exchange suffix like ":US"
            ticker = re.sub(r':[A-Z]+$', '', ticker).strip()
            if not ticker or len(ticker) > 6 or ticker == "N/A":
                continue

            # Issuer name (first line)
            issuer_lines = [t.strip() for t in issuer_cell.stripped_strings]
            issuer_name  = issuer_lines[0] if issuer_lines else ""

            # Trade date
            traded_str  = traded_cell.get_text(strip=True)
            trade_date  = parse_date(traded_str)

            # Type
            tx_type = type_cell.get_text(strip=True).lower()

            # Size
            size_raw = size_cell.get_text(strip=True)

            # Clean politician name — remove party/chamber info
            pol_clean = re.sub(r'\s+(Republican|Democrat|Independent)\s+.*', '', pol_name, flags=re.IGNORECASE).strip()

            trades.append({
                "politician": pol_clean,
                "ticker":     ticker,
                "issuer":     issuer_name,
                "date":       trade_date,
                "type":       "buy" if "buy" in tx_type or "purchase" in tx_type else "sell" if "sell" in tx_type else "other",
                "size_raw":   size_raw,
            })
        except Exception:
            continue

    return trades, len(trades) > 0


def estimate_capital(size_raw):
    """Convert size range string to estimated midpoint."""
    nums = re.findall(r'[\d,]+', size_raw)
    vals = [int(n.replace(",", "")) for n in nums if n.replace(",", "").isdigit()]
    if len(vals) >= 2:
        return (vals[0] + vals[1]) / 2
    if len(vals) == 1:
        return vals[0]
    if "over" in size_raw.lower() or ">1m" in size_raw.lower():
        return 2_500_000
    return 0


def fetch_all_trades(max_pages=30):
    """Paginate Capitol Trades until we've covered 90 days."""
    cutoff_90 = days_ago(90)
    cutoff_30 = days_ago(30)
    all_trades = []
    reached_cutoff = False

    print(f"  Fetching Capitol Trades (up to {max_pages} pages × 96 trades)...")

    for page in range(1, max_pages + 1):
        trades, ok = scrape_page(page)
        if not ok:
            break

        page_trades = []
        for t in trades:
            if t["date"] is None:
                continue
            if t["date"] < cutoff_90:
                reached_cutoff = True
                continue
            page_trades.append(t)

        all_trades.extend(page_trades)

        # Check oldest trade on this page
        valid_dates = [t["date"] for t in trades if t["date"]]
        if valid_dates and min(valid_dates) < cutoff_90:
            reached_cutoff = True

        sys.stdout.write(f"\r    Page {page}/{max_pages} — {len(all_trades)} trades collected")
        sys.stdout.flush()

        if reached_cutoff:
            break

        time.sleep(0.8)  # polite delay

    print(f"\n  Total trades in 90D window: {len(all_trades)}")
    return all_trades, cutoff_30

# ─────────────────────────────────────────────────────────────────────────────
#  AGGREGATE & SCORE
# ─────────────────────────────────────────────────────────────────────────────

def aggregate(trades, cutoff_30):
    agg = {}
    for t in trades:
        ticker = t["ticker"]
        if ticker not in agg:
            agg[ticker] = {
                "ticker":       ticker,
                "issuer":       t["issuer"],
                "politicians_90": set(),
                "politicians_30": set(),
                "capital_buy":  0,
                "capital_sell": 0,
                "buy_count":    0,
                "sell_count":   0,
                "trades":       [],
            }
        a = agg[ticker]
        a["politicians_90"].add(t["politician"])
        if t["date"] >= cutoff_30:
            a["politicians_30"].add(t["politician"])

        cap = estimate_capital(t["size_raw"])
        if t["type"] == "buy":
            a["capital_buy"]  += cap
            a["buy_count"]    += 1
        elif t["type"] == "sell":
            a["capital_sell"] += cap
            a["sell_count"]   += 1

        a["trades"].append({
            "name":   t["politician"],
            "date":   t["date"].isoformat(),
            "type":   t["type"],
            "amount": t["size_raw"],
            "amount_est": cap,
        })

    # Finalize
    for a in agg.values():
        a["pol_count_90"] = len(a["politicians_90"])
        a["pol_count_30"] = len(a["politicians_30"])
        a["politicians_90"] = sorted(list(a["politicians_90"]))
        a["politicians_30"] = sorted(list(a["politicians_30"]))
        a["trades"] = sorted(a["trades"], key=lambda x: x["date"], reverse=True)[:10]

    return agg


def score_ticker(a, funds_13f=0):
    import math
    pol   = a["pol_count_90"]
    cap   = a["capital_buy"] + a["capital_sell"]
    breadth = min(pol / 15 * 100, 100)
    cap_s   = min(math.log10(max(cap, 1000)) / math.log10(2_000_000) * 100, 100) if cap > 0 else 0
    f13_s   = min(funds_13f / 1000 * 100, 100) if funds_13f else 0
    total   = round(breadth * 0.40 + cap_s * 0.30 + f13_s * 0.30)
    return {
        "score":         max(1, total),
        "score_breadth": round(breadth * 0.40),
        "score_capital": round(cap_s * 0.30),
        "score_13f":     round(f13_s * 0.30),
    }


def direction(a):
    b, s = a["buy_count"], a["sell_count"]
    if b == 0 and s == 0: return "unknown"
    if s == 0: return "buy"
    if b == 0: return "sell"
    r = b / (b + s)
    return "buy" if r >= 0.7 else "sell" if r <= 0.3 else "mixed"


def trend(p30, p90):
    if p90 == 0: return "flat"
    r_new = p30 / 30
    r_old = (p90 - p30) / 60 if p90 > p30 else 0
    if r_new > r_old * 1.3: return "up"
    if r_new < r_old * 0.7: return "down"
    return "flat"

# ─────────────────────────────────────────────────────────────────────────────
#  SECTOR LOOKUP
# ─────────────────────────────────────────────────────────────────────────────

def get_sectors(tickers):
    print(f"  Getting sectors for top {len(tickers)} tickers...", end=" ", flush=True)
    result = {}
    try:
        import yfinance as yf
        for t in tickers:
            try:
                info = yf.Ticker(t).info
                result[t] = info.get("sector", info.get("industryDisp", "Unknown"))
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
    print("  TRADERDECK — Political Alpha (Capitol Trades)")
    print("-"*55)
    print(f"  Date: {datetime.date.today()} | Window: 30D + 90D\n")

    # Load Carnivore tickers
    carnivore = {"sr": [], "lt": []}
    try:
        with open("data/carnivore_portfolios.json") as f:
            cp = json.load(f)
        carnivore["sr"] = [p["ticker"] for p in cp.get("sector_rotation", [])]
        carnivore["lt"] = [p["ticker"] for p in cp.get("long_term", [])]
        print(f"  Carnivore: {len(carnivore['sr'])} SR + {len(carnivore['lt'])} LT tickers")
    except Exception as e:
        print(f"  Carnivore load failed: {e}")

    all_carnivore_sr = set(carnivore["sr"])
    all_carnivore_lt = set(carnivore["lt"])

    # Load cached 13F data
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

    # Fetch trades
    raw_trades, cutoff_30 = fetch_all_trades(max_pages=30)

    if not raw_trades:
        print("  ERROR: No trades fetched — aborting")
        sys.exit(1)

    # Aggregate
    agg = aggregate(raw_trades, cutoff_30)
    print(f"  Unique tickers: {len(agg)}")

    # Get sectors for top 40
    top_tickers = sorted(agg.keys(), key=lambda t: agg[t]["pol_count_90"], reverse=True)[:40]
    sectors = get_sectors(top_tickers)

    # Build output items
    items = []
    for ticker, a in agg.items():
        funds = cached_13f.get(ticker, 0)
        sc    = score_ticker(a, funds)
        carn  = "SR" if ticker in all_carnivore_sr else ("LT" if ticker in all_carnivore_lt else None)

        items.append({
            "ticker":          ticker,
            "issuer":          a["issuer"],
            "sector":          sectors.get(ticker, "Unknown"),
            "score":           sc["score"],
            "score_breadth":   sc["score_breadth"],
            "score_capital":   sc["score_capital"],
            "score_13f":       sc["score_13f"],
            "direction":       direction(a),
            "pol_count_30":    a["pol_count_30"],
            "pol_count_90":    a["pol_count_90"],
            "politicians_90":  a["politicians_90"][:10],
            "capital_buy":     round(a["capital_buy"]),
            "capital_sell":    round(a["capital_sell"]),
            "net_flow":        round(a["capital_buy"] - a["capital_sell"]),
            "funds_13f":       funds,
            "has_13f":         funds > 0,
            "carnivore":       carn,
            "trend":           trend(a["pol_count_30"], a["pol_count_90"]),
            "trades":          a["trades"][:8],
            "buy_count":       a["buy_count"],
            "sell_count":      a["sell_count"],
        })

    items.sort(key=lambda x: x["score"], reverse=True)

    portfolio_items = [i for i in items if i["carnivore"]]
    full_scan_items = items[:50]

    total_buy  = sum(i["capital_buy"]  for i in items)
    total_sell = sum(i["capital_sell"] for i in items)
    active_pols = len(set(
        p for a in agg.values() for p in a["politicians_90"]
    ))

    payload = {
        "last_updated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "source":       "capitoltrades.com",
        "window_days":  90,
        "summary": {
            "active_politicians": active_pols,
            "total_buy_capital":  round(total_buy),
            "total_sell_capital": round(total_sell),
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
    print(f"  Full scan:         {len(full_scan_items)} tickers")
    print(f"  Top conviction:    {payload['summary']['top_ticker']} (score {payload['summary']['top_score']})")
    print(f"  Saved → {OUTPUT_PATH}")
    print("-"*55)


if __name__ == "__main__":
    main()
