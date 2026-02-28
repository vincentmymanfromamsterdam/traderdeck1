"""
TRADERDECK — Carnivore Trading Portfolio Scraper
Logs into carnivoretradedesk.com and extracts both portfolio tables.
Requires: CARNIVORE_EMAIL and CARNIVORE_PASSWORD environment variables.

Run locally:
    CARNIVORE_EMAIL=you@email.com CARNIVORE_PASSWORD=yourpass python scrape_carnivore.py

In GitHub Actions these come from repository secrets.
"""

import os
import json
import datetime
import sys

# ─────────────────────────────────────────────────────────
#  INSTALL PLAYWRIGHT IF NEEDED
# ─────────────────────────────────────────────────────────
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("Installing playwright...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright", "-q"])
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"])
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

CARNIVORE_URL   = "https://carnivoretradedesk.com/dashboard"
LOGIN_URL       = "https://carnivoretradedesk.com/login"
OUTPUT_PATH     = "data/carnivore_portfolios.json"

# ─────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────

def clean_num(s):
    """Convert '$1,234.56' or '12.3%' or '-5.4' to float, return None if unparseable."""
    if s is None:
        return None
    s = str(s).strip().replace("$", "").replace(",", "").replace("%", "").replace("(", "-").replace(")", "")
    try:
        return float(s)
    except ValueError:
        return None

def parse_table(page, table_locator):
    """
    Generic table parser. Returns list of dicts keyed by header names.
    Handles most standard HTML tables rendered by JS frameworks.
    """
    try:
        table = page.locator(table_locator).first
        table.wait_for(timeout=8000)
    except PWTimeout:
        print(f"    Table not found: {table_locator}")
        return []

    headers = []
    header_cells = table.locator("thead th, thead td").all()
    for cell in header_cells:
        headers.append(cell.inner_text().strip())

    if not headers:
        # Try first row as header
        first_row = table.locator("tr").first
        header_cells = first_row.locator("td, th").all()
        headers = [c.inner_text().strip() for c in header_cells]

    rows = []
    data_rows = table.locator("tbody tr").all()
    for row in data_rows:
        cells = row.locator("td, th").all()
        if not cells:
            continue
        row_dict = {}
        for i, cell in enumerate(cells):
            key = headers[i] if i < len(headers) else f"col_{i}"
            row_dict[key] = cell.inner_text().strip()
        if any(v for v in row_dict.values()):  # skip empty rows
            rows.append(row_dict)

    return rows


def scrape_portfolios(email, password):
    """Main scraping function. Returns dict with sector_rotation and long_term."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # ── LOGIN ──────────────────────────────────────────
        print("  Navigating to login page...", end=" ", flush=True)
        page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
        print("OK")

        print("  Filling credentials...", end=" ", flush=True)

        # Try common email field selectors
        email_selectors = [
            'input[type="email"]',
            'input[name="email"]',
            'input[placeholder*="email" i]',
            'input[id*="email" i]',
        ]
        pw_selectors = [
            'input[type="password"]',
            'input[name="password"]',
            'input[placeholder*="password" i]',
            'input[id*="password" i]',
        ]

        email_filled = False
        for sel in email_selectors:
            try:
                page.fill(sel, email, timeout=3000)
                email_filled = True
                break
            except Exception:
                continue

        if not email_filled:
            raise RuntimeError("Could not find email input field. The login page may have changed.")

        for sel in pw_selectors:
            try:
                page.fill(sel, password, timeout=3000)
                break
            except Exception:
                continue

        print("OK")

        # Submit
        print("  Submitting login...", end=" ", flush=True)
        submit_selectors = [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Login")',
            'button:has-text("Sign in")',
            'button:has-text("Log in")',
        ]
        for sel in submit_selectors:
            try:
                page.click(sel, timeout=3000)
                break
            except Exception:
                continue

        # Wait for navigation post-login
        try:
            page.wait_for_url("**/dashboard**", timeout=15000)
        except PWTimeout:
            page.wait_for_load_state("networkidle", timeout=15000)

        print("OK")

        # ── NAVIGATE TO DASHBOARD ──────────────────────────
        if "dashboard" not in page.url:
            print("  Navigating to dashboard...", end=" ", flush=True)
            page.goto(CARNIVORE_URL, wait_until="networkidle", timeout=30000)
            print("OK")

        page.wait_for_timeout(3000)  # allow JS to render

        # ── DISCOVER PORTFOLIO SECTIONS ────────────────────
        # Take a snapshot of text to understand the page structure
        page_text = page.inner_text("body")

        print("  Page loaded. Scanning for portfolio tables...")

        # Strategy: find section headers containing "sector" or "long term"
        # then grab the table immediately following them.
        result = {
            "sector_rotation": [],
            "long_term": [],
            "last_updated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "source": "carnivoretradedesk.com",
        }

        # ── Try to find tables by common patterns ──────────
        # Approach 1: look for all tables on the page
        all_tables = page.locator("table").all()
        print(f"  Found {len(all_tables)} table(s) on page.")

        # Try to identify which table is which by nearby heading text
        for i, tbl in enumerate(all_tables):
            try:
                # Get surrounding context (parent heading)
                heading = ""
                try:
                    heading = page.evaluate("""
                        (tbl) => {
                            let el = tbl;
                            for (let i = 0; i < 6; i++) {
                                el = el.parentElement;
                                if (!el) break;
                                const h = el.querySelector('h1,h2,h3,h4,h5,h6,p,[class*="title"],[class*="header"]');
                                if (h) return h.innerText;
                            }
                            return '';
                        }
                    """, tbl.element_handle())
                except Exception:
                    pass

                rows = []
                headers = [th.inner_text().strip() for th in tbl.locator("thead th, thead td").all()]
                if not headers:
                    first_row_cells = tbl.locator("tr").first.locator("td,th").all()
                    headers = [c.inner_text().strip() for c in first_row_cells]

                data_rows = tbl.locator("tbody tr").all()
                for row in data_rows:
                    cells = [c.inner_text().strip() for c in row.locator("td,th").all()]
                    if cells and any(cells):
                        row_dict = {headers[j] if j < len(headers) else f"col_{j}": cells[j] for j in range(len(cells))}
                        rows.append(row_dict)

                heading_lower = heading.lower()
                label = f"table_{i}"

                if "sector" in heading_lower and ("rotation" in heading_lower or "sector" in heading_lower):
                    label = "sector_rotation"
                    result["sector_rotation"] = rows
                    print(f"  ✓ Sector Rotation table found ({len(rows)} rows)")
                elif "long" in heading_lower and "term" in heading_lower:
                    label = "long_term"
                    result["long_term"] = rows
                    print(f"  ✓ Long Term portfolio table found ({len(rows)} rows)")
                else:
                    print(f"  Table {i}: '{heading[:60]}' — {len(rows)} rows (headers: {headers[:4]})")

            except Exception as e:
                print(f"  Table {i}: error — {e}")

        # ── Fallback: try card/list based portfolio layouts ──
        if not result["sector_rotation"] and not result["long_term"]:
            print("  No standard tables found. Trying card/list layout...")
            result = _scrape_card_layout(page, result)

        browser.close()
        return result


def _scrape_card_layout(page, result):
    """
    Fallback for dashboards that use cards/lists instead of tables.
    Tries to extract position data from common card patterns.
    """
    # Common patterns: divs with ticker + price + change
    positions = page.evaluate("""
        () => {
            const results = { sector: [], longterm: [] };
            // Look for elements that look like position rows
            // (ticker symbol patterns: 2-5 uppercase letters)
            const allText = document.body.innerText;
            return { raw: allText.substring(0, 5000) };
        }
    """)

    print("  Raw page preview (first 1000 chars):")
    print("  " + positions.get("raw", "")[:1000].replace("\n", "\n  "))

    # Save the raw dump so you can inspect and we can refine the scraper
    os.makedirs("data", exist_ok=True)
    with open("data/carnivore_debug_dump.txt", "w") as f:
        f.write(positions.get("raw", ""))
    print("  Debug dump saved to data/carnivore_debug_dump.txt")
    print("  → Share this file so we can refine the scraper for Carnivore's layout.")

    return result


# ─────────────────────────────────────────────────────────
#  NORMALIZE PORTFOLIO DATA
# ─────────────────────────────────────────────────────────

def normalize_portfolio(rows):
    """
    Standardize column names to our expected format regardless of
    what headers Carnivore uses. Returns list of normalized position dicts.
    """
    normalized = []
    for row in rows:
        # Map common column name variants
        keys_lower = {k.lower().strip(): v for k, v in row.items()}

        def get(*candidates):
            for c in candidates:
                for k, v in keys_lower.items():
                    if c in k:
                        return v
            return None

        ticker = get("ticker", "symbol", "stock", "name")
        name   = get("company", "name", "description") or ticker
        shares = clean_num(get("shares", "qty", "quantity", "position"))
        avg_cost = clean_num(get("avg", "cost", "average", "entry", "basis"))
        curr_price = clean_num(get("price", "current", "last", "close"))
        market_val = clean_num(get("market", "value", "mkt val"))
        unrealized = clean_num(get("unrealized", "gain", "p&l", "pnl", "profit"))
        pct_chg    = clean_num(get("return", "change", "pct", "%", "gain%"))
        weight     = clean_num(get("weight", "alloc", "allocation", "%"))
        sector     = get("sector", "category", "group")

        if not ticker:
            continue  # skip rows without a ticker

        pos = {
            "ticker":      ticker.upper().strip() if ticker else None,
            "name":        name,
            "shares":      shares,
            "avg_cost":    avg_cost,
            "curr_price":  curr_price,
            "market_value":market_val,
            "unrealized_pnl": unrealized,
            "pct_return":  pct_chg,
            "weight":      weight,
            "sector":      sector,
            "_raw":        row,  # keep original for debugging
        }
        normalized.append(pos)

    return normalized


# ─────────────────────────────────────────────────────────
#  ENRICH WITH YAHOO FINANCE
# ─────────────────────────────────────────────────────────

def enrich_with_yfinance(portfolios):
    """
    For positions where we have shares + avg_cost but no live price,
    fetch current price and calculate P&L from Yahoo Finance.
    """
    try:
        import yfinance as yf
    except ImportError:
        return portfolios  # skip enrichment if not available

    for port_name, positions in portfolios.items():
        if not isinstance(positions, list) or not positions:
            continue

        tickers_needing_price = [
            p["ticker"] for p in positions
            if p.get("ticker") and not p.get("curr_price") and p.get("shares") and p.get("avg_cost")
        ]

        if not tickers_needing_price:
            continue

        print(f"  Enriching {port_name} prices from Yahoo Finance ({len(tickers_needing_price)} symbols)...")
        try:
            data = yf.download(tickers_needing_price, period="2d", interval="1d",
                               group_by="ticker", auto_adjust=True, progress=False, threads=True)

            for pos in positions:
                t = pos.get("ticker")
                if t not in tickers_needing_price:
                    continue
                try:
                    df = data[t] if len(tickers_needing_price) > 1 else data
                    curr = float(df["Close"].dropna().iloc[-1])
                    pos["curr_price"] = round(curr, 2)
                    if pos.get("shares") and pos.get("avg_cost"):
                        pos["market_value"]    = round(curr * pos["shares"], 2)
                        pos["unrealized_pnl"]  = round((curr - pos["avg_cost"]) * pos["shares"], 2)
                        pos["pct_return"]      = round((curr - pos["avg_cost"]) / pos["avg_cost"] * 100, 2)
                except Exception as e:
                    print(f"    Could not enrich {t}: {e}")
        except Exception as e:
            print(f"  Yahoo enrichment error: {e}")

    return portfolios


# ─────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────

def main():
    print("\n" + "─"*50)
    print("  TRADERDECK — Carnivore Portfolio Scraper")
    print("─"*50)

    email    = os.environ.get("CARNIVORE_EMAIL")
    password = os.environ.get("CARNIVORE_PASSWORD")

    if not email or not password:
        print("\n  ERROR: Missing credentials.")
        print("  Set CARNIVORE_EMAIL and CARNIVORE_PASSWORD environment variables.")
        print("\n  Local test:")
        print("  CARNIVORE_EMAIL=you@email.com CARNIVORE_PASSWORD=pass python scrape_carnivore.py")
        sys.exit(1)

    print(f"  Account: {email[:4]}***{email.split('@')[-1]}")
    print()

    raw = scrape_portfolios(email, password)

    # Normalize
    print("\n  Normalizing portfolio data...")
    portfolios = {
        "sector_rotation": normalize_portfolio(raw.get("sector_rotation", [])),
        "long_term":       normalize_portfolio(raw.get("long_term", [])),
        "last_updated":    raw.get("last_updated"),
        "source":          raw.get("source"),
    }

    # Enrich missing prices from Yahoo Finance
    print("  Enriching with Yahoo Finance prices...")
    portfolios = enrich_with_yfinance(portfolios)

    # Save
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(portfolios, f, indent=2)

    sr_count  = len(portfolios["sector_rotation"])
    lt_count  = len(portfolios["long_term"])
    print(f"\n  ✓ Sector Rotation: {sr_count} position(s)")
    print(f"  ✓ Long Term:       {lt_count} position(s)")
    print(f"  ✓ Saved to {OUTPUT_PATH}")
    print("─"*50 + "\n")

    if sr_count == 0 and lt_count == 0:
        print("  ⚠ No portfolio data extracted.")
        print("  Check data/carnivore_debug_dump.txt and share with Claude to refine the scraper.")
        sys.exit(1)


if __name__ == "__main__":
    main()
