"""
TRADERDECK - Carnivore Trading Portfolio Scraper v3
"""

import os
import json
import datetime
import sys

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright", "-q"])
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"])
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

LOGIN_URL    = "https://carnivoretradedesk.com/login"
SECTOR_URL   = "https://carnivoretradedesk.com/sector-heaters"
LONGTERM_URL = "https://carnivoretradedesk.com/longterm"
OUTPUT_PATH  = "data/carnivore_portfolios.json"


def clean_num(s):
    if s is None:
        return None
    s = str(s).strip().replace("$", "").replace(",", "").replace("%", "").replace("(", "-").replace(")", "")
    try:
        return float(s)
    except ValueError:
        return None


def save_debug(page, label):
    os.makedirs("data", exist_ok=True)
    try:
        text = page.inner_text("body")
        with open(f"data/debug_{label}.txt", "w") as f:
            f.write(f"URL: {page.url}\n\n{text[:5000]}")
        print(f"  Debug saved: data/debug_{label}.txt")
    except Exception as e:
        print(f"  Debug save failed: {e}")


def do_login(page, email, password):
    print("  Going to login page...", end=" ", flush=True)
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)
    print("OK")
    print(f"  URL: {page.url}")

    if "login" not in page.url.lower():
        print("  Already logged in")
        return True

    # Print all inputs for debugging
    all_inputs = page.locator("input").all()
    print(f"  Found {len(all_inputs)} input(s):")
    for inp in all_inputs:
        try:
            t = inp.get_attribute("type") or "text"
            n = inp.get_attribute("name") or ""
            ph = inp.get_attribute("placeholder") or ""
            print(f"    type={t} name={n} placeholder={ph}")
        except Exception:
            pass

    # Fill email - first non-password visible input
    print("  Filling email...", end=" ", flush=True)
    filled_email = False
    for inp in page.locator("input:visible").all():
        try:
            itype = (inp.get_attribute("type") or "text").lower()
            if itype in ("password", "submit", "button", "checkbox", "hidden", "radio"):
                continue
            inp.click()
            inp.fill("")
            inp.type(email, delay=50)
            filled_email = True
            print("OK")
            break
        except Exception:
            continue

    if not filled_email:
        print("FAILED")
        save_debug(page, "no_email_field")
        return False

    # Fill password
    print("  Filling password...", end=" ", flush=True)
    filled_pw = False
    for inp in page.locator("input[type='password'], input[type='Password']").all():
        try:
            inp.click()
            inp.fill("")
            inp.type(password, delay=50)
            filled_pw = True
            print("OK")
            break
        except Exception:
            continue

    if not filled_pw:
        # Tab from email to password
        print("trying Tab...", end=" ", flush=True)
        try:
            page.keyboard.press("Tab")
            page.wait_for_timeout(300)
            page.keyboard.type(password, delay=50)
            filled_pw = True
            print("OK")
        except Exception:
            print("FAILED")
            return False

    page.wait_for_timeout(500)

    # Submit without expecting navigation (SPA)
    print("  Submitting...", end=" ", flush=True)
    submitted = False
    for sel in ['button[type="submit"]', 'input[type="submit"]', 'button:has-text("LOGIN")',
                'button:has-text("Login")', 'button:has-text("Sign In")', 'button:has-text("SIGN IN")']:
        try:
            page.wait_for_selector(sel, timeout=2000)
            page.click(sel)
            submitted = True
            print(f"OK ({sel})")
            break
        except Exception:
            continue

    if not submitted:
        page.keyboard.press("Enter")
        print("OK (Enter)")

    # Poll for redirect (SPA won't fire navigation event)
    print("  Waiting for auth...", end=" ", flush=True)
    for i in range(30):
        page.wait_for_timeout(500)
        if "login" not in page.url.lower():
            print(f"OK ({(i+1)*0.5:.1f}s)")
            page.wait_for_timeout(2000)
            print(f"  Post-login URL: {page.url}")
            print("  Login successful")
            return True
        try:
            cookies = page.context.cookies()
            auth = [c for c in cookies if any(k in c["name"].lower() for k in ["token", "auth", "session", "jwt"])]
            if auth:
                print(f"OK (cookie: {auth[0]['name']})")
                page.wait_for_timeout(2000)
                return True
        except Exception:
            pass

    body = page.inner_text("body")
    if any(k in body.lower() for k in ["dashboard", "portfolio", "sector", "logout", "sign out"]):
        print("OK (content check)")
        return True

    print("TIMEOUT")
    save_debug(page, "login_failed")
    print(f"  Body: {body[:300]}")
    return False


def scrape_page(page, url, label):
    print(f"\n  Loading {label}...", end=" ", flush=True)
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(4000)
    print("OK")
    print(f"  URL: {page.url}")

    if "login" in page.url.lower():
        print("  Redirected to login")
        save_debug(page, f"{label}_redirect")
        return []

    save_debug(page, label)

    tables = page.locator("table").all()
    print(f"  Found {len(tables)} table(s)")

    if tables:
        best, best_count = None, 0
        for t in tables:
            try:
                n = len(t.locator("tbody tr").all())
                if n > best_count:
                    best_count, best = n, t
            except Exception:
                pass

        if best and best_count > 0:
            print(f"  Parsing table ({best_count} rows)...")
            headers = [c.inner_text().strip() for c in best.locator("thead th, thead td").all()]
            if not headers:
                headers = [c.inner_text().strip() for c in best.locator("tr").first.locator("td,th").all()]
            rows = []
            for row in best.locator("tbody tr").all():
                cells = [c.inner_text().strip() for c in row.locator("td,th").all()]
                if cells and any(cells):
                    rows.append({headers[i] if i < len(headers) else f"col_{i}": cells[i] for i in range(len(cells))})
            return rows

    print("  No tables found")
    return []


def normalize(rows):
    out = []
    for row in rows:
        kl = {k.lower().strip(): v for k, v in row.items()}

        def get(*keys):
            for k in keys:
                for rk, rv in kl.items():
                    if k in rk:
                        return rv
            return None

        ticker = get("ticker", "symbol", "stock")
        if not ticker:
            continue
        out.append({
            "ticker":         ticker.upper().strip(),
            "name":           get("company", "name", "description") or ticker,
            "shares":         clean_num(get("shares", "qty", "quantity")),
            "avg_cost":       clean_num(get("avg", "cost", "entry", "basis")),
            "curr_price":     clean_num(get("current", "price", "last")),
            "market_value":   clean_num(get("market", "value", "mkt")),
            "unrealized_pnl": clean_num(get("unrealized", "gain", "p&l", "pnl")),
            "pct_return":     clean_num(get("return", "change", "gain%")),
            "weight":         clean_num(get("weight", "alloc")),
            "stop_loss":      clean_num(get("stop")),
            "buy_up_to":      clean_num(get("buy up", "target")),
            "entry_date":     get("date", "entry date"),
        })
    return out


def main():
    print("\n" + "-"*55)
    print("  TRADERDECK - Carnivore Scraper v3")
    print("-"*55)

    email    = os.environ.get("CARNIVORE_EMAIL")
    password = os.environ.get("CARNIVORE_PASSWORD")

    if not email or not password:
        print("  ERROR: Missing CARNIVORE_EMAIL or CARNIVORE_PASSWORD")
        sys.exit(1)

    print(f"  Account: {email[:4]}***{email.split('@')[-1]}")

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
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            locale="en-US",
        )
        page = context.new_page()

        if not do_login(page, email, password):
            print("\n  Login failed - keeping existing data")
            browser.close()
            sys.exit(1)

        sr_raw = scrape_page(page, SECTOR_URL, "sector_rotation")
        sr = normalize(sr_raw) if sr_raw else existing.get("sector_rotation", [])

        lt_raw = scrape_page(page, LONGTERM_URL, "long_term")
        if not lt_raw:
            lt_raw = scrape_page(page, "https://carnivoretradedesk.com/long-term", "long_term_alt")
        lt = normalize(lt_raw) if lt_raw else existing.get("long_term", [])

        browser.close()

    os.makedirs("data", exist_ok=True)
    payload = {
        "last_updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source": "carnivoretradedesk.com",
        "sector_rotation": sr,
        "long_term": lt,
    }
    with open(OUTPUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"\n  Sector Rotation: {len(sr)} positions")
    print(f"  Long Term:       {len(lt)} positions")
    print(f"  Saved to {OUTPUT_PATH}")
    print("-"*55)

    if len(sr) == 0 and len(lt) == 0:
        print("  No data extracted")
        sys.exit(1)


if __name__ == "__main__":
    main()


