"""
TRADERDECK — IBD Market Pulse scraper
=====================================
Logs in to investors.com with the subscriber's own credentials and reads:
  - Stock Market Exposure card (homepage): outlook + recommended exposure band
  - The Big Picture: latest column (title, date, link, excerpt, first paragraphs)

Secrets (GitHub → Settings → Secrets and variables → Actions):
  IBD_EMAIL, IBD_PASSWORD

Output: data/ibd.json  (keeps a history of outlook changes)
Run:    python scrape_ibd.py
"""

import datetime as dt
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

OUTPUT_PATH = "data/ibd.json"
HOME_URL = "https://www.investors.com/"
SIGNIN_URL = "https://myibd.investors.com/secure/signin.aspx?eurl=https%3A%2F%2Fwww.investors.com%2F"
BIGPIC_URL = "https://www.investors.com/category/market-trend/the-big-picture/"

OUTLOOK_PATTERNS = [
    r"confirmed uptrend", r"uptrend under pressure", r"market in correction",
    r"rally attempt", r"uptrend resumes", r"correction",
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def log(msg):
    print(f"  {msg}", flush=True)


def load_prev():
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def all_frames(page):
    return [page.main_frame] + [f for f in page.frames if f != page.main_frame]


def find_input(page, selectors, timeout_ms=20000):
    """Wait for the first matching visible input in any frame. Returns (frame, selector) or (None, None)."""
    deadline = dt.datetime.now() + dt.timedelta(milliseconds=timeout_ms)
    while dt.datetime.now() < deadline:
        for fr in all_frames(page):
            for sel in selectors:
                try:
                    loc = fr.locator(sel).first
                    if loc.count() and loc.is_visible():
                        return fr, sel
                except Exception:
                    continue
        page.wait_for_timeout(500)
    return None, None


def diag(page, label):
    """Log URL, title and every input on the page (all frames) — so a failed login is debuggable from the log."""
    try:
        log(f"{label}: url={page.url}")
        log(f"{label}: title={page.title()!r}")
        for fr in all_frames(page):
            inputs = fr.evaluate("""() => Array.from(document.querySelectorAll('input,button')).slice(0,30).map(e =>
                [e.tagName, e.type||'', e.name||'', e.id||'', (e.placeholder||e.innerText||'').slice(0,30), getComputedStyle(e).display!=='none'].join('|'))""")
            if inputs:
                log(f"{label}: frame={fr.url[:80]} → " + " ; ".join(inputs))
    except Exception as e:
        log(f"{label}: diag failed: {e}")


def dismiss_banners(page):
    for fr in all_frames(page):
        for sel in ["#onetrust-accept-btn-handler", "button:has-text('Accept All')", "button:has-text('Accept')",
                    "button:has-text('I Accept')", "button:has-text('Agree')", "button:has-text('YES, I AGREE')"]:
            try:
                b = fr.locator(sel).first
                if b.count() and b.is_visible():
                    b.click(timeout=2000)
                    page.wait_for_timeout(600)
                    return
            except Exception:
                pass


EMAIL_SELECTORS = ["input[name='username']", "input#username", "input[type='email']", "input[name*='mail' i]",
                   "input[id*='mail' i]", "input[name*='user' i]", "input[id*='user' i]",
                   "input[autocomplete='username']", "input[type='text']"]
PASSWORD_SELECTORS = ["input[type='password']", "input[name='password']", "input#password"]
SUBMIT_SELECTORS = ["button[type='submit']", "input[type='submit']", "button:has-text('Continue')",
                    "button:has-text('Sign In')", "button:has-text('Sign in')", "button:has-text('Log In')",
                    "button:has-text('Next')"]


def click_submit(fr, page):
    for sel in SUBMIT_SELECTORS:
        try:
            b = fr.locator(sel).first
            if b.count() and b.is_visible():
                b.click(timeout=3000)
                return sel
        except Exception:
            continue
    page.keyboard.press("Enter")
    return "Enter"


def login(page, email, password):
    page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    dismiss_banners(page)
    diag(page, "signin page")

    fr, sel = find_input(page, EMAIL_SELECTORS, timeout_ms=25000)
    if not fr:
        raise RuntimeError("login: email field not found")
    fr.locator(sel).first.fill(email)
    log(f"login: email field {sel} (frame {fr.url[:60]})")

    # Password on the same step, or on a second step after submitting the username (Dow Jones SSO style)
    pfr, psel = find_input(page, PASSWORD_SELECTORS, timeout_ms=2000)
    if not pfr:
        how = click_submit(fr, page)
        log(f"login: submitted username via {how}, waiting for password step")
        pfr, psel = find_input(page, PASSWORD_SELECTORS, timeout_ms=20000)
        if not pfr:
            diag(page, "after username")
            raise RuntimeError("login: password field not found")
    pfr.locator(psel).first.fill(password)
    how = click_submit(pfr, page)
    log(f"login: submitted password via {how}")
    try:
        page.wait_for_url(re.compile(r"investors\.com(?!.*sign)"), timeout=30000)
    except PWTimeout:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeout:
        pass
    page.wait_for_timeout(1500)
    if re.search(r"sign-?in|login|sso\.accounts", page.url, re.I):
        diag(page, "after password")
        raise RuntimeError(f"login: still on sign-in page ({page.url}) — check IBD_EMAIL / IBD_PASSWORD or a bot/2FA check")
    log(f"login: ok → {page.url}")


def read_exposure(page):
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
    card = page.locator(".market-exposure-card").first
    try:
        card.wait_for(state="visible", timeout=20000)
        # wait until the skeleton placeholder is gone (data loaded)
        page.wait_for_function(
            "() => { const c = document.querySelector('.market-exposure-card'); return c && !c.querySelector('.ant-skeleton'); }",
            timeout=25000)
    except PWTimeout:
        log("exposure: card did not finish loading (skeleton still present)")
    page.wait_for_timeout(1000)
    text = card.inner_text() if card.count() else ""
    html = card.inner_html() if card.count() else ""
    text_n = re.sub(r"\s+", " ", text).strip()
    log(f"exposure card text: {text_n[:200]}")

    outlook = None
    for pat in OUTLOOK_PATTERNS:
        m = re.search(pat, text_n, flags=re.I)
        if m:
            outlook = m.group(0).title().replace("Under Pressure", "Under Pressure")
            break
    band = None
    m = re.search(r"(\d{1,3})\s*%\s*to\s*(\d{1,3})\s*%\s*invested", text_n, flags=re.I)
    if m:
        band = [int(m.group(1)), int(m.group(2))]
    if band is None:
        m = re.search(r"(\d{1,3})\s*[-–]\s*(\d{1,3})\s*%", text_n)
        if m:
            band = [int(m.group(1)), int(m.group(2))]
    # anchor-at-N tells which step is highlighted (0..4) — fallback for the band
    if band is None:
        m = re.search(r"anchor-at-(\d)", html)
        if m and not re.search(r"anchor-at-\d\"[^>]*display:\s*none", html):
            i = int(m.group(1))
            band = [i * 20, i * 20 + 20]
    return {"outlook": outlook, "exposure_band": band, "raw": text_n[:300]}


def read_big_picture(page):
    page.goto(BIGPIC_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)
    items = page.locator("li:has(h3 a[href*='/the-big-picture/'])")
    n = items.count()
    log(f"big picture: {n} list items")
    if not n:
        return None
    first = items.first
    a = first.locator("h3 a").first
    title = (a.get_attribute("title") or a.inner_text() or "").strip()
    url = a.get_attribute("href")
    date = ""
    try:
        date = first.locator(".entry-meta .date").first.inner_text().strip()
    except Exception:
        pass
    excerpt = ""
    try:
        ps = first.locator("p:not(.entry-meta)")
        if ps.count():
            excerpt = ps.first.inner_text().strip()
    except Exception:
        pass

    # open the article for the first paragraphs (rendered after login)
    paras = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)
        for p in page.locator("article p").all()[:12]:
            t = re.sub(r"\s+", " ", p.inner_text()).strip()
            if len(t) > 60 and not re.search(r"^(Related|See also|YOU MIGHT|Follow)", t, re.I):
                paras.append(t)
            if len(paras) >= 2:
                break
    except Exception as e:
        log(f"big picture article: {e}")
    return {"title": title, "url": url, "date": date, "excerpt": excerpt, "paragraphs": paras}


def main():
    email, password = os.environ.get("IBD_EMAIL"), os.environ.get("IBD_PASSWORD")
    if not email or not password:
        print("IBD_EMAIL / IBD_PASSWORD not set — skipping IBD scrape")
        return 0
    print("TRADERDECK — IBD Market Pulse")
    prev = load_prev()
    today = dt.date.today().isoformat()
    result = {"updated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
              "updated_date": today, "ok": False}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900}, locale="en-US")
        page = ctx.new_page()
        try:
            login(page, email, password)
            exp = read_exposure(page)
            result.update(exp)
            bp = read_big_picture(page)
            result["big_picture"] = bp
            result["ok"] = bool(exp.get("outlook") or exp.get("exposure_band") or bp)
        except Exception as e:
            log(f"ERROR: {e}")
            try:
                os.makedirs("data", exist_ok=True)
                page.screenshot(path="data/ibd_debug.png", full_page=True)
                with open("data/ibd_debug.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                log(f"saved data/ibd_debug.png + ibd_debug.html (url={page.url})")
            except Exception:
                pass
            result["error"] = str(e)
        finally:
            browser.close()

    # Keep what worked last time if today failed
    if not result["ok"] and prev.get("ok"):
        for k in ("outlook", "exposure_band", "big_picture"):
            result.setdefault(k, prev.get(k))
        result["stale_from"] = prev.get("updated_date")

    # Outlook history: one entry per change
    hist = prev.get("outlook_history") or []
    if result.get("outlook"):
        last = hist[-1] if hist else None
        if not last or last.get("outlook") != result["outlook"] or last.get("band") != result.get("exposure_band"):
            hist.append({"date": today, "outlook": result["outlook"], "band": result.get("exposure_band")})
    result["outlook_history"] = hist[-40:]

    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result, f, indent=1)
    log(f"outlook={result.get('outlook')} band={result.get('exposure_band')} bigpic={(result.get('big_picture') or {}).get('title', '')[:60]}")
    log(f"saved {OUTPUT_PATH} (ok={result['ok']})")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
