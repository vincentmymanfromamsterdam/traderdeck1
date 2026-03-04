"""
TRADERDECK — Political Alpha Fetcher
Sources (in order of preference):
  1. efdsearch.senate.gov — official Senate EDGAR scraper (direct, always fresh)
  2. GitHub raw Senate (timothycarambat) — may be stale but fallback
"""

import json, os, sys, datetime, time, re, math
import requests
from xml.etree import ElementTree as ET

OUTPUT_PATH = "data/political_alpha.json"
TIMEOUT     = 30
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# ─── SOURCE 1: Official Senate EDGAR ─────────────────────────────────────────
# efdsearch.senate.gov is the PRIMARY source that senatestockwatcher.com uses.
# It has a search API that returns PTR filings as JSON.

def fetch_senate_efdsearch():
    """
    Scrape the official Senate financial disclosure search.
    Endpoint: https://efdsearch.senate.gov/search/report/data/
    This is the live source that timothycarambat's scraper reads from.
    """
    session = requests.Session()
    # First: get CSRF token
    print("  [Source 1] efdsearch.senate.gov...", end=" ", flush=True)
    try:
        base = session.get(
            "https://efdsearch.senate.gov/search/",
            headers={"User-Agent": UA}, timeout=TIMEOUT
        )
        csrf = session.cookies.get("csrftoken") or _extract_csrf(base.text)

        headers = {
            "User-Agent":   UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer":      "https://efdsearch.senate.gov/search/",
            "X-CSRFToken":  csrf or "",
        }

        # Agree to terms (required first time)
        session.post(
            "https://efdsearch.senate.gov/search/home/",
            data={"prohibition_agreement": "1"},
            headers=headers, timeout=TIMEOUT
        )

        # Search for PTR reports (Periodic Transaction Reports)
        cutoff_str = (datetime.date.today() - datetime.timedelta(days=90)).strftime("%m/%d/%Y")
        payload = {
            "start":           "0",
            "length":          "100",
            "report_types":    "[11]",  # 11 = PTR
            "submitted_start_date": cutoff_str,
        }
        r = session.post(
            "https://efdsearch.senate.gov/search/report/data/",
            data=payload, headers=headers, timeout=TIMEOUT
        )

        if r.status_code == 200:
            data = r.json()
            records = data.get("data", [])
            print(f"OK ({len(records)} PTR filings)")
            return records, session
        else:
            print(f"HTTP {r.status_code}")
            return None, session
    except Exception as e:
        print(f"FAILED: {e}")
        return None, None


def _extract_csrf(html):
    m = re.search(r"csrfmiddlewaretoken.*?value=['\"]([^'\"]+)['\"]", html)
    return m.group(1) if m else None


def fetch_ptr_transactions(filings, session):
    """
    For each PTR filing, fetch the individual transactions.
    Each filing has a link to a page with a transactions table.
    """
    trades = []
    cutoff = days_ago(90)

    for i, filing in enumerate(filings[:50]):  # limit to 50 filings
        # filing is typically [first_name, last_name, office, report_date, link, ...]
        try:
            if isinstance(filing, list) and len(filing) >= 5:
                first     = filing[0] or ""
                last      = filing[1] or ""
                name      = f"{first} {last}".strip()
                date_str  = filing[3] or ""
                link_html = filing[4] or ""
                # Extract URL from HTML link
                url_m = re.search(r'href="([^"]+)"', link_html)
                if not url_m:
                    continue
                url = "https://efdsearch.senate.gov" + url_m.group(1)
            else:
                continue

            d = parse_date(date_str)
            if not d or d < cutoff:
                continue

            # Fetch the PTR page
            time.sleep(0.3)
            r = session.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
            if r.status_code != 200:
                continue

            # Parse transactions table
            txs = _parse_ptr_page(r.text, name, d)
            trades.extend(txs)
            sys.stdout.write(f"\r  Parsed {i+1}/{min(len(filings),50)} filings — {len(trades)} trades")
            sys.stdout.flush()

        except Exception:
            continue

    print(f"\n  EFD trades (90D window): {len(trades)}")
    return trades


def _parse_ptr_page(html, senator_name, filing_date):
    """Parse the transactions table from a PTR filing page."""
    trades = []
    # Look for table rows with ticker/transaction data
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL | re.IGNORECASE)
    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
        cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
        if len(cells) < 4:
            continue
        # Typical columns: Asset Name | Asset Type | Transaction Date | Transaction Type | Amount
        # Try to find ticker in asset name (e.g. "Apple Inc. (AAPL)")
        ticker = None
        for cell in cells[:3]:
            m = re.search(r'\(([A-Z]{1,6})\)', cell)
            if m:
                ticker = m.group(1)
                break
        if not ticker:
            continue

        # Find transaction type
        tx_raw = ""
        for cell in cells:
            cl = cell.lower()
            if "purchase" in cl or "sale" in cl or "exchange" in cl:
                tx_raw = cl
                break

        # Find amount
        amount = ""
        for cell in cells:
            if "$" in cell or "," in cell:
                amount = cell
                break

        ty = tx_type(tx_raw)
        trades.append({
            "politician": senator_name,
            "chamber":    "Senate",
            "ticker":     ticker,
            "date":       filing_date,
            "type":       ty,
            "size_raw":   amount,
            "capital":    estimate_capital(amount),
        })
    return trades


# ─── SOURCE 2: GitHub raw (stale fallback, shows recent dates) ───────────────

def fetch_github_senate():
    url = "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json"
    print(f"  [Source 2] GitHub Senate raw...", end=" ", flush=True)
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                # Show freshness
                dates = []
                for row in data:
                    d = parse_date(row.get("transaction_date", ""))
                    if d:
                        dates.append(d)
                if dates:
                    most_recent = max(dates)
                    cutoff = days_ago(90)
                    fresh = [d for d in dates if d >= cutoff]
                    print(f"OK ({len(data):,} records, most recent: {most_recent}, in 90D: {len(fresh)})")
                    if fresh:
                        return data
                    else:
                        print(f"  ⚠️  Repo appears stale — most recent trade: {most_recent}")
                        return None
                else:
                    print(f"OK but no parseable dates")
                    return None
            print(f"Unexpected format")
            return None
        print(f"HTTP {r.status_code}")
        return None
    except Exception as e:
        print(f"FAILED: {e}")
        return None


def normalize_senate_github(data):
    cutoff = days_ago(90)
    trades = []
    for row in (data or []):
        ticker = clean_ticker(row.get("ticker", ""))
        if not ticker:
            continue
        d = parse_date(row.get("transaction_date", ""))
        if not d or d < cutoff:
            continue
        ty_raw = str(row.get("type", "")).lower()
        name   = str(row.get("senator", row.get("name", "Unknown")))
        trades.append({
            "politician": name, "chamber": "Senate",
            "ticker": ticker, "date": d,
            "type": tx_type(ty_raw),
            "size_raw": str(row.get("amount", "")),
            "capital":  estimate_capital(str(row.get("amount", ""))),
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
    s = str(s).strip()
    # Handle M/D/YYYY (no leading zeros)
    parts = re.split(r'[-/]', s)
    if len(parts) == 3:
        # Try to detect format
        p0, p1, p2 = parts
        if len(p2) == 4:  # MM/DD/YYYY or DD/MM/YYYY
            try: return datetime.date(int(p2), int(p0), int(p1))  # MM/DD/YYYY
            except Exception: pass
        if len(p0) == 4:  # YYYY-MM-DD
            try: return datetime.date(int(p0), int(p1), int(p2))
            except Exception: pass
    return None

def tx_type(raw):
    if any(w in raw for w in ("purchase", "buy", "exchange_false")):
        return "buy"
    if any(w in raw for w in ("sale", "sell")):
        return "sell"
    return "other"

def estimate_capital(s):
    if not s: return 0
    nums = [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", str(s))]
    if len(nums) >= 2: return (nums[0] + nums[1]) / 2
    if len(nums) == 1: return nums[0]
    return 0

# ─── AGGREGATE & SCORE ───────────────────────────────────────────────────────

def aggregate(trades):
    cutoff_30 = days_ago(30)
    agg = {}
    for t in trades:
        k = t["ticker"]
        if k not in agg:
            agg[k] = {"ticker": k, "issuer": k,
                      "politicians_90": set(), "politicians_30": set(),
                      "capital_buy": 0, "capital_sell": 0,
                      "buy_count": 0, "sell_count": 0, "trades": []}
        a = agg[k]
        a["politicians_90"].add(t["politician"])
        if t["date"] >= cutoff_30:
            a["politicians_30"].add(t["politician"])
        if t["type"] == "buy":
            a["capital_buy"]  += t["capital"]; a["buy_count"]  += 1
        elif t["type"] == "sell":
            a["capital_sell"] += t["capital"]; a["sell_count"] += 1
        a["trades"].append({"name": t["politician"], "chamber": t.get("chamber",""),
                            "date": t["date"].isoformat(), "type": t["type"],
                            "amount": t["size_raw"], "amount_est": t["capital"]})
    for a in agg.values():
        a["pol_count_90"] = len(a["politicians_90"])
        a["pol_count_30"] = len(a["politicians_30"])
        a["politicians_90"] = sorted(a["politicians_90"])
        a["trades"] = sorted(a["trades"], key=lambda x: x["date"], reverse=True)[:10]
    return agg

def score_item(a, f13=0):
    p = a["pol_count_90"]; c = a["capital_buy"] + a["capital_sell"]
    br = min(p / 15 * 100, 100)
    cs = min(math.log10(max(c, 1000)) / math.log10(2_000_000) * 100, 100) if c else 0
    fs = min(f13 / 1000 * 100, 100) if f13 else 0
    tot = round(br * 0.40 + cs * 0.30 + fs * 0.30)
    return {"score": max(1, tot), "score_breadth": round(br*0.40),
            "score_capital": round(cs*0.30), "score_13f": round(fs*0.30)}

def direction(a):
    b, s = a["buy_count"], a["sell_count"]
    if not b and not s: return "unknown"
    if not s: return "buy"
    if not b: return "sell"
    r = b / (b + s)
    return "buy" if r >= 0.7 else "sell" if r <= 0.3 else "mixed"

def trend(p30, p90):
    if not p90: return "flat"
    rn = p30 / 30; ro = (p90 - p30) / 60 if p90 > p30 else 0
    return "up" if rn > ro * 1.3 else "down" if rn < ro * 0.7 else "flat"

def get_sectors(tickers):
    print(f"  Sectors ({len(tickers)} tickers)...", end=" ", flush=True)
    result = {}
    try:
        import yfinance as yf
        for t in tickers:
            try: result[t] = yf.Ticker(t).info.get("sector", "Unknown") or "Unknown"
            except Exception: result[t] = "Unknown"
        print("OK")
    except Exception as e:
        print(f"skipped ({e})"); result = {t: "Unknown" for t in tickers}
    return result

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 60)
    print("  TRADERDECK — Political Alpha")
    print("=" * 60)
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
        print(f"  Carnivore: {e}\n")

    # Load cached 13F
    cached_13f = {}
    try:
        with open(OUTPUT_PATH) as f:
            old = json.load(f)
        for item in old.get("full_scan", []) + old.get("portfolio", []):
            if item.get("funds_13f"): cached_13f[item["ticker"]] = item["funds_13f"]
    except Exception: pass

    # ── Try sources ─────────────────────────────────────────────────────────
    trades = []

    # Source 1: Official Senate EFD search
    filings, session = fetch_senate_efdsearch()
    if filings and session:
        efd_trades = fetch_ptr_transactions(filings, session)
        if efd_trades:
            trades.extend(efd_trades)

    # Source 2: GitHub raw (with freshness check)
    if not trades:
        gh_data = fetch_github_senate()
        if gh_data:
            trades = normalize_senate_github(gh_data)

    if not trades:
        print("\n  ERROR: All sources failed or stale — aborting")
        print("  NOTE: The timothycarambat GitHub repo may no longer be updated.")
        print("  Consider: Unusual Whales API ($150/mo) for reliable fresh data.")
        sys.exit(1)

    print(f"\n  Total trades in window : {len(trades)}")

    # ── Aggregate & score ───────────────────────────────────────────────────
    agg = aggregate(trades)
    print(f"  Unique tickers         : {len(agg)}")

    top40   = sorted(agg.keys(), key=lambda t: agg[t]["pol_count_90"], reverse=True)[:40]
    sectors = get_sectors(top40)

    items = []
    for ticker, a in agg.items():
        f13  = cached_13f.get(ticker, 0)
        sc   = score_item(a, f13)
        carn = ("SR" if ticker in carnivore_sr else
                "LT" if ticker in carnivore_lt else None)
        items.append({
            "ticker": ticker, "issuer": a["issuer"],
            "sector": sectors.get(ticker, "Unknown"),
            "score": sc["score"], "score_breadth": sc["score_breadth"],
            "score_capital": sc["score_capital"], "score_13f": sc["score_13f"],
            "direction": direction(a),
            "pol_count_30": a["pol_count_30"], "pol_count_90": a["pol_count_90"],
            "politicians_90": a["politicians_90"][:12],
            "capital_buy": round(a["capital_buy"]), "capital_sell": round(a["capital_sell"]),
            "net_flow": round(a["capital_buy"] - a["capital_sell"]),
            "funds_13f": f13, "has_13f": f13 > 0, "carnivore": carn,
            "trend": trend(a["pol_count_30"], a["pol_count_90"]),
            "trades": a["trades"][:8],
            "buy_count": a["buy_count"], "sell_count": a["sell_count"],
        })

    items.sort(key=lambda x: x["score"], reverse=True)
    portfolio_items = [i for i in items if i["carnivore"]]
    full_scan_items = items[:50]
    active_pols     = len({p for a in agg.values() for p in a["politicians_90"]})

    payload = {
        "last_updated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "window_days": 90,
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

    print(f"\n  Carnivore overlap : {len(portfolio_items)}")
    print(f"  Top 5 tickers     : {', '.join(i['ticker'] for i in full_scan_items[:5])}")
    print(f"  Top conviction    : {payload['summary']['top_ticker']} (score {payload['summary']['top_score']})")
    print(f"\n  Saved → {OUTPUT_PATH}")
    print("=" * 60 + "\n")

if __name__ == "__main__":
    main()
