"""
TRADERDECK — Source Diagnostics
Run this to see which data sources are reachable from this environment.
"""
import sys, datetime
import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

SOURCES = [
    # Label, URL, expected_content_hint
    ("GitHub raw — House (timothycarambat)",
     "https://raw.githubusercontent.com/timothycarambat/house-stock-watcher-data/master/data/all_transactions.json",
     "transaction_date"),

    ("GitHub raw — Senate (timothycarambat)",
     "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json",
     "transaction_date"),

    ("S3 House us-west-2",
     "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json",
     "transaction_date"),

    ("S3 Senate us-east-2",
     "https://senate-stock-watcher-data.s3-us-east-2.amazonaws.com/aggregate/all_transactions.json",
     "transaction_date"),

    ("QuiverQuant congress",
     "https://api.quiverquant.com/beta/live/congresstrading",
     "Ticker"),

    ("Capitol Trades BFF",
     "https://bff.capitoltrades.com/trades?page=1&pageSize=5",
     "data"),

    ("Unusual Whales congress (no auth)",
     "https://api.unusualwhales.com/api/congress/recent-trades",
     "data"),

    ("housestockwatcher.com API page",
     "https://housestockwatcher.com/api",
     "transaction"),

    ("senatestockwatcher.com API page",
     "https://senatestockwatcher.com/api",
     "transaction"),
]

print("\n" + "="*65)
print("  TRADERDECK — Source Diagnostics")
print(f"  {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
print("="*65)

results = []
for label, url, hint in SOURCES:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
        status = r.status_code
        size   = len(r.content)
        # Check if response contains expected data
        has_data = hint.lower() in r.text.lower() if status == 200 else False
        ct = r.headers.get("content-type", "")[:40]

        if status == 200 and has_data:
            icon = "✅"
            note = f"HTTP 200 · {size:,} bytes · data hint '{hint}' FOUND"
        elif status == 200:
            icon = "⚠️ "
            note = f"HTTP 200 · {size:,} bytes · but hint '{hint}' NOT found · preview: {r.text[:80]!r}"
        else:
            icon = "❌"
            note = f"HTTP {status} · {r.text[:100]!r}"
    except Exception as e:
        icon = "❌"
        note = f"EXCEPTION: {e}"

    print(f"\n  {icon}  {label}")
    print(f"      {note}")
    results.append((icon, label))

print("\n" + "="*65)
ok = [l for i, l in results if "✅" in i]
warn = [l for i, l in results if "⚠" in i]
fail = [l for i, l in results if "❌" in i]
print(f"  SUMMARY: {len(ok)} OK · {len(warn)} partial · {len(fail)} failed")
if ok:
    print(f"  WORKING: {', '.join(ok)}")
print("="*65 + "\n")

sys.exit(0)  # always exit 0 so workflow continues
