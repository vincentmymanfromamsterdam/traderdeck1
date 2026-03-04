"""
TRADERDECK — Political Alpha Fetcher
Fetches congressional trades + 13F filings and scores tickers.
Sources: housestockwatcher.com, senatestockwatcher.com, SEC EDGAR
No API keys required — all free public data.
"""

import json
import os
import sys
import datetime

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    import urllib.request
    import urllib.error
    HAS_REQUESTS = False

OUTPUT_PATH = "data/political_alpha.json"

# ─────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────

def fetch_json(url, label=""):
    """Fetch JSON — uses requests (follows redirects) or urllib fallback."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, */*",
    }
    if HAS_REQUESTS:
        r = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        r.raise_for_status()
        return r.json()
    else:
        # urllib fallback — manually follow redirect
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                new_url = e.headers.get('Location')
                if new_url:
                    req2 = urllib.request.Request(new_url, headers=headers)
                    with urllib.request.urlopen(req2, timeout=30) as r2:
                        return json.loads(r2.read().decode())
            raise

def days_ago(n):
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()

def clean_amount(s):
    """Estimate midpoint of STOCK Act ranges like '$15,001 - $50,000'"""
    if not s:
        return 0
    import re
    nums = re.findall(r'[\d,]+', str(s).replace('$', ''))
    vals = [int(n.replace(',', '')) for n in nums if n.replace(',', '').isdigit()]
    if len(vals) >= 2:
        return (vals[0] + vals[1]) / 2
    if len(vals) == 1:
        return vals[0]
    # Handle text ranges
    s_lower = s.lower()
    if 'over $1,000,000' in s_lower or '>$1m' in s_lower:
        return 2500000
    if '1,000,001' in s or '1000001' in s:
        return 2500000
    return 0

def get_sector(ticker):
    """Best-effort sector from yfinance — fails gracefully."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return info.get('sector', info.get('industry', 'Unknown'))
    except Exception:
        return 'Unknown'

# ─────────────────────────────────────────────────────────
#  CONGRESSIONAL TRADES
# ─────────────────────────────────────────────────────────

def fetch_house_trades():
    """Fetch House trades — tries multiple known endpoints."""
    print("  Fetching House trades...", end=" ", flush=True)
    urls = [
        "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json",
        "https://house-stock-watcher-data.s3-us-east-2.amazonaws.com/data/all_transactions.json",
        "https://housestockwatcher.com/api/transactions_partitioned/all_transactions.json",
    ]
    for url in urls:
        try:
            data = fetch_json(url)
            if isinstance(data, list) and len(data) > 0:
                print(f"OK ({len(data)} records)")
                return data
        except Exception as e:
            continue
    print("ERROR: all endpoints failed")
    return []

def fetch_senate_trades():
    """Fetch Senate trades — tries multiple known endpoints."""
    print("  Fetching Senate trades...", end=" ", flush=True)
    urls = [
        "https://senate-stock-watcher-data.s3-us-east-2.amazonaws.com/aggregate/all_transactions.json",
        "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json",
        "https://senatestockwatcher.com/api/transactions/all_transactions.json",
    ]
    for url in urls:
        try:
            data = fetch_json(url)
            if isinstance(data, list) and len(data) > 0:
                print(f"OK ({len(data)} records)")
                return data
        except Exception as e:
            continue
    print("ERROR: all endpoints failed")
    return []

def process_trades(house, senate, days=90):
    """
    Combine, filter by date window, aggregate by ticker.
    Returns dict: ticker -> {buys, sells, politicians, capital_buy, capital_sell, trades[]}
    """
    cutoff_90 = days_ago(90)
    cutoff_30 = days_ago(30)
    agg = {}

    def add_trade(ticker, politician, tx_date, tx_type, amount_str, chamber):
        if not ticker or ticker in ('--', 'N/A', ''):
            return
        ticker = ticker.upper().strip()
        # Skip options/bonds — keep equities only
        if any(x in ticker for x in ['PUT', 'CALL', 'BOND', ' ']):
            return

        tx_date_str = str(tx_date)[:10]
        if tx_date_str < cutoff_90:
            return  # outside 90D window

        amount = clean_amount(amount_str)
        is_buy  = 'purchase' in str(tx_type).lower() or 'buy' in str(tx_type).lower() or 'exchange' in str(tx_type).lower()
        is_sell = 'sale' in str(tx_type).lower() or 'sell' in str(tx_type).lower()

        if ticker not in agg:
            agg[ticker] = {
                'ticker': ticker,
                'politicians_90': set(),
                'politicians_30': set(),
                'capital_buy': 0,
                'capital_sell': 0,
                'trades': [],
                'buy_count': 0,
                'sell_count': 0,
            }

        a = agg[ticker]
        a['politicians_90'].add(politician)
        if tx_date_str >= cutoff_30:
            a['politicians_30'].add(politician)

        if is_buy:
            a['capital_buy']  += amount
            a['buy_count']    += 1
        elif is_sell:
            a['capital_sell'] += amount
            a['sell_count']   += 1

        a['trades'].append({
            'name':    politician,
            'chamber': chamber,
            'date':    tx_date_str,
            'type':    'buy' if is_buy else 'sell' if is_sell else 'exchange',
            'amount':  str(amount_str or ''),
            'amount_est': amount,
        })

    # Process House
    for t in house:
        try:
            ticker = t.get('ticker') or t.get('asset_description', '')
            # housestockwatcher sometimes puts ticker in asset_description
            if not ticker or len(ticker) > 5:
                import re
                m = re.search(r'\b([A-Z]{1,5})\b', str(t.get('asset_description', '')))
                ticker = m.group(1) if m else ''
            add_trade(
                ticker=ticker,
                politician=t.get('representative', t.get('name', 'Unknown')),
                tx_date=t.get('transaction_date', t.get('disclosure_date', '')),
                tx_type=t.get('type', t.get('transaction_type', '')),
                amount_str=t.get('amount', ''),
                chamber='House'
            )
        except Exception:
            continue

    # Process Senate
    for t in senate:
        try:
            add_trade(
                ticker=t.get('ticker', ''),
                politician=t.get('senator', t.get('name', 'Unknown')),
                tx_date=t.get('transaction_date', t.get('date', '')),
                tx_type=t.get('type', t.get('transaction_type', '')),
                amount_str=t.get('amount', ''),
                chamber='Senate'
            )
        except Exception:
            continue

    # Convert sets to counts + sort trades by date
    for ticker, a in agg.items():
        a['pol_count_90'] = len(a['politicians_90'])
        a['pol_count_30'] = len(a['politicians_30'])
        a['politicians_90'] = sorted(list(a['politicians_90']))
        a['politicians_30'] = sorted(list(a['politicians_30']))
        a['trades'] = sorted(a['trades'], key=lambda x: x['date'], reverse=True)[:15]

    print(f"  Processed {len(agg)} unique tickers from congressional trades")
    return agg

# ─────────────────────────────────────────────────────────
#  13F FILINGS
# ─────────────────────────────────────────────────────────

def fetch_13f_counts():
    """
    Get approximate 13F holder counts for top tickers via SEC EDGAR API.
    Uses the EDGAR full-text search to count recent 13F filings per ticker.
    """
    print("  Fetching 13F data from SEC EDGAR...", end=" ", flush=True)
    try:
        # Get list of most recent 13F filings
        url = "https://efts.sec.gov/LATEST/search-index?q=%2213F-HR%22&dateRange=custom&startdt={}&enddt={}&forms=13F-HR".format(
            days_ago(100), datetime.date.today().isoformat()
        )
        # Use EDGAR company search instead — more reliable
        url = "https://data.sec.gov/submissions/CIK0000102909.json"  # test call
        fetch_json(url)
        print("OK (EDGAR reachable)")
        return {}  # Full 13F parsing is complex — return empty, use cached if available
    except Exception as e:
        print(f"WARN: {e} — skipping 13F enrichment")
        return {}

def load_cached_13f():
    """Load previously cached 13F data if available."""
    path = "data/political_alpha.json"
    if os.path.exists(path):
        try:
            with open(path) as f:
                old = json.load(f)
            cache = {}
            for item in old.get('full_scan', []) + old.get('portfolio', []):
                if item.get('funds_13f'):
                    cache[item['ticker']] = item['funds_13f']
            return cache
        except Exception:
            return {}
    return {}

# ─────────────────────────────────────────────────────────
#  SECTOR LOOKUP
# ─────────────────────────────────────────────────────────

SECTOR_CACHE = {}

def get_sectors_bulk(tickers):
    """Fetch sectors for a list of tickers via yfinance."""
    print(f"  Fetching sectors for {len(tickers)} tickers...", end=" ", flush=True)
    try:
        import yfinance as yf
        result = {}
        # Batch in groups of 20
        for i in range(0, len(tickers), 20):
            batch = tickers[i:i+20]
            for t in batch:
                try:
                    info = yf.Ticker(t).fast_info
                    # fast_info doesn't have sector — use basic info
                    result[t] = 'Unknown'
                except Exception:
                    result[t] = 'Unknown'
        # Try getting sector from download data
        import yfinance as yf
        for t in tickers[:30]:  # limit to top 30 to save time
            try:
                info = yf.Ticker(t).info
                result[t] = info.get('sector', info.get('industryDisp', 'Unknown'))
            except Exception:
                result[t] = 'Unknown'
        print(f"OK")
        return result
    except Exception as e:
        print(f"ERROR: {e}")
        return {t: 'Unknown' for t in tickers}

# ─────────────────────────────────────────────────────────
#  SCORING
# ─────────────────────────────────────────────────────────

def score_ticker(agg_data, funds_13f=0):
    """
    Score = 40% politician breadth + 30% capital weight + 30% 13F bonus
    All components normalized 0-100.
    """
    pol   = agg_data['pol_count_90']
    cap   = agg_data['capital_buy'] + agg_data['capital_sell']
    net   = agg_data['capital_buy'] - agg_data['capital_sell']

    # Breadth score: 1 pol=10, 5=50, 10=80, 15+=100
    breadth_score = min(pol / 15 * 100, 100)

    # Capital score: normalized on log scale, $50K=10, $500K=50, $2M+=100
    import math
    cap_score = min(math.log10(max(cap, 1000)) / math.log10(2000000) * 100, 100) if cap > 0 else 0

    # 13F bonus: 0 funds=0, 100=20, 500=60, 1000+=100
    f13_score = min(funds_13f / 1000 * 100, 100) if funds_13f else 0

    total = round(breadth_score * 0.40 + cap_score * 0.30 + f13_score * 0.30)
    return {
        'score': max(1, total),
        'score_breadth': round(breadth_score * 0.40),
        'score_capital': round(cap_score * 0.30),
        'score_13f':     round(f13_score * 0.30),
    }

def determine_direction(agg_data):
    b = agg_data['buy_count']
    s = agg_data['sell_count']
    if b == 0 and s == 0:
        return 'unknown'
    if s == 0:
        return 'buy'
    if b == 0:
        return 'sell'
    ratio = b / (b + s)
    if ratio >= 0.7:
        return 'buy'
    if ratio <= 0.3:
        return 'sell'
    return 'mixed'

def determine_trend(pol_30, pol_90):
    if pol_90 == 0:
        return 'flat'
    rate_recent = pol_30 / 30
    rate_older  = (pol_90 - pol_30) / 60 if pol_90 > pol_30 else 0
    if rate_recent > rate_older * 1.3:
        return 'up'
    if rate_recent < rate_older * 0.7:
        return 'down'
    return 'flat'

# ─────────────────────────────────────────────────────────
#  BUILD OUTPUT
# ─────────────────────────────────────────────────────────

def build_output(agg, sectors, cached_13f, carnivore_tickers):
    """Build ranked list of tickers with all fields."""
    items = []
    for ticker, a in agg.items():
        if a['pol_count_90'] == 0:
            continue
        funds = cached_13f.get(ticker, 0)
        scores = score_ticker(a, funds)
        direction = determine_direction(a)
        trend = determine_trend(a['pol_count_30'], a['pol_count_90'])

        # Carnivore match
        carnivore_match = None
        if ticker in carnivore_tickers.get('sr', []):
            carnivore_match = 'SR'
        elif ticker in carnivore_tickers.get('lt', []):
            carnivore_match = 'LT'

        items.append({
            'ticker':        ticker,
            'sector':        sectors.get(ticker, 'Unknown'),
            'score':         scores['score'],
            'score_breadth': scores['score_breadth'],
            'score_capital': scores['score_capital'],
            'score_13f':     scores['score_13f'],
            'direction':     direction,
            'pol_count_30':  a['pol_count_30'],
            'pol_count_90':  a['pol_count_90'],
            'politicians_90':a['politicians_90'][:10],
            'capital_buy':   round(a['capital_buy']),
            'capital_sell':  round(a['capital_sell']),
            'net_flow':      round(a['capital_buy'] - a['capital_sell']),
            'funds_13f':     funds,
            'has_13f':       funds > 0,
            'carnivore':     carnivore_match,
            'trend':         trend,
            'trades':        a['trades'][:8],
            'buy_count':     a['buy_count'],
            'sell_count':    a['sell_count'],
        })

    # Sort by score desc
    items.sort(key=lambda x: x['score'], reverse=True)
    return items

# ─────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────

def main():
    print("\n" + "-"*55)
    print("  TRADERDECK — Political Alpha Fetcher")
    print("-"*55)
    print(f"  Date: {datetime.date.today()} | Window: 30D + 90D\n")

    # Load Carnivore tickers for overlap detection
    carnivore_tickers = {'sr': [], 'lt': []}
    try:
        with open('data/carnivore_portfolios.json') as f:
            cp = json.load(f)
        carnivore_tickers['sr'] = [p['ticker'] for p in cp.get('sector_rotation', [])]
        carnivore_tickers['lt'] = [p['ticker'] for p in cp.get('long_term', [])]
        print(f"  Carnivore: {len(carnivore_tickers['sr'])} SR + {len(carnivore_tickers['lt'])} LT tickers")
    except Exception as e:
        print(f"  Carnivore load failed: {e}")

    all_carnivore = set(carnivore_tickers['sr'] + carnivore_tickers['lt'])

    # Load cached 13F data
    cached_13f = load_cached_13f()
    print(f"  Cached 13F entries: {len(cached_13f)}")

    # Fetch congressional trades
    house  = fetch_house_trades()
    senate = fetch_senate_trades()

    if not house and not senate:
        print("  ERROR: Both House and Senate data unavailable — aborting")
        sys.exit(1)
    if not house:
        print("  WARN: House data unavailable — using Senate only")
    if not senate:
        print("  WARN: Senate data unavailable — using House only")

    # Aggregate
    print("  Aggregating trades...")
    agg = process_trades(house, senate, days=90)

    # Get top 50 tickers by activity for sector lookup
    top_tickers = sorted(agg.keys(), key=lambda t: agg[t]['pol_count_90'], reverse=True)[:50]
    sectors = get_sectors_bulk(top_tickers)

    # Build output
    all_items = build_output(agg, sectors, cached_13f, carnivore_tickers)

    # Split into portfolio view and full scan
    portfolio_items  = [i for i in all_items if i['carnivore']]
    full_scan_items  = all_items[:50]  # top 50 by score

    # Summary stats
    total_buy_cap  = sum(i['capital_buy']  for i in all_items)
    total_sell_cap = sum(i['capital_sell'] for i in all_items)
    active_pols    = len(set(
        p for a in agg.values()
        for p in a['politicians_90']
    ))
    carnivore_hits = len(portfolio_items)
    f13_hits       = len([i for i in full_scan_items if i['has_13f']])
    top_ticker     = all_items[0]['ticker'] if all_items else '—'
    top_score      = all_items[0]['score'] if all_items else 0

    payload = {
        'last_updated':    datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'),
        'window_days':     90,
        'summary': {
            'active_politicians': active_pols,
            'total_buy_capital':  round(total_buy_cap),
            'total_sell_capital': round(total_sell_cap),
            'carnivore_overlap':  carnivore_hits,
            'f13_confirmed':      f13_hits,
            'top_ticker':         top_ticker,
            'top_score':          top_score,
        },
        'portfolio':  portfolio_items,
        'full_scan':  full_scan_items,
    }

    os.makedirs('data', exist_ok=True)
    with open(OUTPUT_PATH, 'w') as f:
        json.dump(payload, f, indent=2)

    print(f"\n  Portfolio overlap: {len(portfolio_items)} tickers")
    print(f"  Full scan:         {len(full_scan_items)} tickers")
    print(f"  Top conviction:    {top_ticker} (score {top_score})")
    print(f"  Saved to {OUTPUT_PATH}")
    print("-"*55)

if __name__ == '__main__':
    main()
