"""
TRADERDECK — Carnivore Trading Portfolio Scraper
Logs into carnivoretradedesk.com and extracts both portfolio tables.
Requires: CARNIVORE_EMAIL and CARNIVORE_PASSWORD environment variables.
"""

import os
import json
import datetime
import sys
import time

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("Installing playwright...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright", "-q"])
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"])
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

LOGIN_URL       = "https://carnivoretradedesk.com/login"
SECTOR_URL      = "https://carnivoretradedesk.com/sector-heaters"
LONGTERM_URL    = "https://carnivoretradedesk.com/long-term-portfolio"
OUTPUT_PATH     = "data/carnivore_portfolios.json"

# ─────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────

def clean_num(s):
    if s is None:
        return None
    s = str(s).strip().replace("$", "").replace(",", "").replace("%", "").replace("(", "-").replace(")", "")
    try:
        return float(s)
    except ValueError:
        return None

def save_debug(page, label="debug"):
    """Save page text and screenshot for debugging."""
    os.makedirs("data", exist_ok=True)
    try:
        text = page.inner_text("body")
        with open(f"data/carnivore_{label}_dump.txt", "w") as f:
            f.write(f"URL: {page.url}\n\n{text}")
        print(f"  Debug dump saved: data/carnivore_{label}_dump.txt")
    except Exception as e:
        print(f"  Could not save debug dump: {e}")

# ─────────────────────────────────────────────────────────
#  LOGIN
# ─────────────────────────────────────────────────────────

def do_login(page, email, password):
    """
    Robust login: fill credentials, submit, verify session established.
    Returns True if login succeeded.
    """
    print("  Navigating to login page...", end=" ", flush=True)
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    print("OK")

    print(f"  Current URL: {page.url}")

    # If already logged in, skip
    if "login" not in page.url.lower():
        print("  Already logged in — skipping login step")
        return True

    print("  Filling email...", end=" ", flush=True)
    email_selectors = [
        'input[type="email"]',
        'input[name="email"]',
        'input[placeholder*="email" i]',
        'input[id*="email" i]',
        'input[autocomplete*="email" i]',
    ]
    for sel in email_selectors:
        try:
            page.wait_for_selector(sel, timeout=3000)
            page.click(sel)
            page.fill(sel, email)
            print("OK")
            break
        except Exception:
            continue
    else:
        print("FAILED — could not find email field")
        save_debug(page, "login_fail")
        return False

    print("  Filling password...", end=" ", flush=True)
    pw_selectors = [
        'input[type="password"]',
        'input[name="password"]',
        'input[placeholder*="password" i]',
        'input[id*="password" i]',
    ]
    for sel in pw_selectors:
        try:
            page.wait_for_selector(sel, timeout=3000)
            page.click(sel)
            page.fill(sel, password)
            print("OK")
            break
        except Exception:
            continue
    else:
        print("FAILED — could not find password field")
        return False

    # Small pause to let React/Vue state update
    page.wait_for_timeout(500)

    print("  Clicking submit...", end=" ", flush=True)
    submit_selectors = [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("LOGIN")',
        'button:has-text("Login")',
        'button:has-text("Sign in")',
        'button:has-text("Log in")',
        'button:has-text("SIGN IN")',
    ]

    clicked = False
    for sel in submit_selectors:
        try:
            page.wait_for_selector(sel, timeout=2000)
            # Use expect_navigation for React apps
            with page.expect_navigation(wait_until="networkidle", timeout=20000):
                page.click(sel)
            clicked = True
            print("OK")
            break
        except Exception:
            continue

    if not clicked:
        # Fallback: press Enter in the password field
        print("trying Enter key...", end=" ", flush=True)
        try:
            page.keyboard.press("Enter")
            page.wait_for_load_state("networkidle", timeout=20000)
            print("OK")
        except Exception as e:
            print(f"FAILED: {e}")
            save_debug(page, "submit_fail")
            return False

    # Wait a moment for auth to settle
    page.wait_for_timeout(3000)

    current_url = page.url
    print(f"  Post-login URL: {current_url}")

    # Verify login succeeded — should no longer be on login page
    if "login" in current_url.lower():
        print("  ⚠ Still on login page — login may have failed")
        print("  Checking page content...")
        body_text = page.inner_text("body")
        print(f"  Page preview: {body_text[:300]}")
        save_debug(page, "login_still_on_login")
        return False

    print("  ✓ Login successful")
    return True


# ─────────────────────────────────────────────────────────
#  SCRAPE A PORTFOLIO PAGE
# ─────────────────────────────────────────────────────────

def scrape_page(page, url, label):
    """Navigate to a portfolio page and extract all meaningful data."""
    print(f"\n  Navigating to {label} ({url})...", end=" ", flush=True)
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(4000)  # wait for JS rendering
    print("OK")

    current_url = page.url
    print(f"  URL after navigation: {current_url}")

    # Redirected back to login?
    if "login" in current_url.lower():
        print(f"  ⚠ Redirected to login — session not maintained")
        save_debug(page, f"{label}_redirect")
        return []

    # Get full page text for debugging
    save_debug(page, label)

    # ── Strategy 1: Standard HTML tables ────────────────
    all_tables = page.locator("table").all()
    print(f"  Found {len(all_tables)} HTML table(s)")

    if all_tables:
        best_table = None
        best_row_count = 0
        for tbl in all_tables:
            try:
                rows = tbl.locator("tbody tr").all()
                if len(rows) > best_row_count:
                    best_row_count = len(rows)
                    best_table = tbl
            except Exception:
                continue

        if best_table and best_row_count > 0:
            print(f"  Parsing table with {best_row_count} rows...")
            return parse_table(best_table)

    # ── Strategy 2: Look for list/card rows ─────────────
    print("  No tables found — trying list/card layout...")

    # Common patterns for JS portfolio dashboards
    row_selectors = [
        '[class*="row"]',
        '[class*="position"]',
        '[class*="holding"]',
        '[class*="stock"]',
        '[class*="portfolio-item"]',
        'li[class*="item"]',
        'tr',
    ]

    for sel in row_selectors:
        try:
            items = page.locator(sel).all()
            if len(items) > 3:
                print(f"  Found {len(items)} items matching '{sel}'")
                break
        except Exception:
            continue

    # ── Strategy 3: Extract structured data via JS ───────
    print("  Trying JavaScript data extraction...")
    try:
        # Look for React/Vue component data in the DOM
        data = page.evaluate("""
            () => {
                const results = [];
                // Look for elements containing ticker-like text (2-5 uppercase letters)
                const tickerRegex = /^[A-Z]{1,5}$/;
                const allEls = document.querySelectorAll('*');
                const candidates = [];

                for (const el of allEls) {
                    if (el.children.length > 0) continue; // skip parent elements
                    const txt = el.innerText ? el.innerText.trim() : '';
                    if (tickerRegex.test(txt) && txt.length >= 1 && txt.length <= 5) {
                        // Found a potential ticker — grab its row/parent context
                        const parent = el.closest('tr, [class*="row"], [class*="item"], li') || el.parentElement;
                        if (parent && !candidates.includes(parent)) {
                            candidates.push(parent);
                            results.push({
                                ticker: txt,
                                context: parent.innerText.replace(/\\n/g, ' | ').substring(0, 200)
                            });
                        }
                    }
                }
                return results.slice(0, 50); // max 50 rows
            }
        """)

        if data and len(data) > 2:
            print(f"  Found {len(data)} potential position rows via JS")
            for row in data[:5]:
                print(f"    {row['ticker']}: {row['context'][:100]}")
            return parse_js_data(data)

    except Exception as e:
        print(f"  JS extraction error: {e}")

    print(f"  ⚠ Could not extract data from {label}")
    return []


def parse_table(table):
    """Parse a standard HTML table into list of dicts."""
    headers = []
    for cell in table.locator("thead th, thead td").all():
        headers.append(cell.inner_text().strip())

    if not headers:
        first_row = table.locator("tr").first
        headers = [c.inner_text().strip() for c in first_row.locator("td,th").all()]

    rows = []
    for row in table.locator("tbody tr").all():
        cells = [c.inner_text().strip() for c in row.locator("td,th").all()]
        if not cells or not any(cells):
            continue
        row_dict = {headers[i] if i < len(headers) else f"col_{i}": cells[i]
                    for i in range(len(cells))}
        rows.append(row_dict)
    return rows


def parse_js_data(data):
    """Parse JS-extracted ticker+context rows into normalized dicts."""
    results = []
    for item in data:
        ticker = item.get("ticker", "").strip()
        context = item.get("context", "")
        if not ticker:
            continue

        # Try to extract numbers from context
        import re
        numbers = re.findall(r'\$?[\d,]+\.?\d*', context)
        prices = []
        for n in numbers:
            val = clean_num(n)
            if val and 0.01 < val < 100000:
                prices.append(val)

        row = {"ticker": ticker, "name": ticker, "_raw_context": context}
        if len(prices) >= 1:
            row["avg_cost"] = prices[0]
        if len(prices) >= 2:
            row["curr_price"] = prices[1]
        results.append(row)

    return results


# ─────────────────────────────────────────────────────────
#  NORMALIZE
# ─────────────────────────────────────────────────────────

def normalize(rows):
    normalized = []
    for row in rows:
        kl = {k.lower().strip(): v for k, v in row.items()}

        def get(*candidates):
            for c in candidates:
                for k, v in kl.items():
                    if c in k:
                        return v
            return None

        ticker    = get("ticker", "symbol", "stock")
        name      = get("company", "name", "description") or ticker
        shares    = clean_num(get("shares", "qty", "quantity"))
        avg_cost  = clean_num(get("avg", "cost", "average", "entry", "basis"))
        curr      = clean_num(get("current", "price", "last", "close"))
        mkt_val   = clean_num(get("market", "value", "mkt"))
        unrealized= clean_num(get("unrealized", "gain", "p&l", "pnl", "profit", "loss"))
        pct_chg   = clean_num(get("return", "change", "pct", "%", "gain%"))
        weight    = clean_num(get("weight", "alloc", "allocation"))
        stop      = clean_num(get("stop"))
        buy_up    = clean_num(get("buy", "target", "limit"))
        entry_date= get("date", "entry date", "entered")

        if not ticker:
            continue

        pos = {
            "ticker":          ticker.upper().strip(),
            "name":            name if name != ticker else ticker,
            "shares":          shares,
            "avg_cost":        avg_cost,
            "curr_price":      curr,
            "market_value":    mkt_val,
            "unrealized_pnl":  unrealized,
            "pct_return":      pct_chg,
            "weight":          weight,
            "stop_loss":       stop,
            "buy_up_to":       buy_up,
            "entry_date":      entry_date,
        }
        normalized.append(pos)
    return normalized


# ─────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────

def main():
    print("\n" + "─"*55)
    print("  TRADERDECK — Carnivore Portfolio Scraper v2")
    print("─"*55)

    email    = os.environ.get("CARNIVORE_EMAIL")
    password = os.environ.get("CARNIVORE_PASSWORD")

    if not email or not password:
        print("\n  ERROR: Missing credentials.")
        print("  Set CARNIVORE_EMAIL and CARNIVORE_PASSWORD env vars.")
        sys.exit(1)

    print(f"  Account: {email[:4]}***{email.split('@')[-1]}")

    # Load existing file so we don't wipe good data on failure
    existing = {"sector_rotation": [], "long_term": []}
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH) as f:
                existing = json.load(f)
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ]
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            locale="en-US",
        )
        page = context.new_page()

        # Login
        login_ok = do_login(page, email, password)

        if not login_ok:
            print("\n  ✗ Login failed — keeping existing portfolio data")
            browser.close()
            sys.exit(1)

        # Scrape sector rotation
        sr_raw = scrape_page(page, SECTOR_URL, "sector_rotation")
        sr = normalize(sr_raw) if sr_raw else existing.get("sector_rotation", [])
        print(f"\n  Sector Rotation: {len(sr)} position(s) extracted")

        # Scrape long term — try common URL patterns
        lt_raw = scrape_page(page, LONGTERM_URL, "long_term")
        if not lt_raw:
            # Try alternate URL
            lt_raw = scrape_page(page, "https://carnivoretradedesk.com/long-term", "long_term_alt")
        lt = normalize(lt_raw) if lt_raw else existing.get("long_term", [])
        print(f"  Long Term:       {len(lt)} position(s) extracted")

        browser.close()

    # Save results
    os.makedirs("data", exist_ok=True)
    payload = {
        "last_updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source": "carnivoretradedesk.com",
        "sector_rotation": sr,
        "long_term": lt,
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"\n  ✓ Saved to {OUTPUT_PATH}")
    print("─"*55 + "\n")

    if len(sr) == 0 and len(lt) == 0:
        print("  ⚠ No data extracted from either portfolio.")
        print("  Check data/carnivore_*_dump.txt files for page content.")
        sys.exit(1)


if __name__ == "__main__":
    main()
