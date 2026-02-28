# TRADERDECK — Position & Swing Trading Dashboard

A clean, fast market dashboard for position and swing traders. EOD data pulled from Yahoo Finance via GitHub Actions, served as a static site — **no backend, no API costs, completely free**.

---

## 🚀 Quick Setup (15 minutes)

### Step 1 — Create a GitHub repository

1. Go to [github.com/new](https://github.com/new)
2. Name it `traderdeck` (or anything you like)
3. Set it to **Public** (required for free GitHub Pages)
4. Click **Create repository**

### Step 2 — Upload the files

Upload the entire contents of this folder to your new repo:
```
traderdeck/
├── index.html               ← your dashboard
├── fetch_data.py            ← data fetcher script
├── data/
│   └── market_data.json     ← placeholder (overwritten by Action)
└── .github/
    └── workflows/
        └── update-data.yml  ← automatic daily refresh
```

You can do this via the GitHub web UI (drag & drop) or via git:
```bash
git init
git remote add origin https://github.com/YOUR_USERNAME/traderdeck.git
git add .
git commit -m "initial commit"
git push -u origin main
```

### Step 3 — Enable GitHub Pages

1. Go to your repo → **Settings** → **Pages**
2. Under **Source**, select **Deploy from a branch**
3. Choose **main** branch, **/ (root)** folder
4. Click **Save**
5. Your dashboard will be live at:
   `https://YOUR_USERNAME.github.io/traderdeck/`

### Step 4 — Trigger the first data fetch

The GitHub Action runs automatically Mon–Fri at 22:00 UTC. For the first run:

1. Go to your repo → **Actions** tab
2. Click **Daily Market Data Refresh**
3. Click **Run workflow** → **Run workflow**
4. Wait ~60 seconds for it to complete
5. Reload your dashboard — you'll see live data!

---

## 📁 File Structure

| File | Purpose |
|------|---------|
| `index.html` | Dashboard UI — reads `data/market_data.json` |
| `fetch_data.py` | Python script — fetches Yahoo Finance data |
| `data/market_data.json` | Generated JSON — don't edit manually |
| `.github/workflows/update-data.yml` | Runs `fetch_data.py` daily |

---

## ⏱ Data Refresh Schedule

The GitHub Action runs **Monday through Friday at 22:00 UTC** (23:00 CET / 00:00 CEST), which is approximately 6 hours after US market close — giving Yahoo Finance time to settle EOD prices.

You can also trigger a manual refresh anytime via the **Actions** tab.

---

## 🛠 Customization

### Add/remove symbols

Edit the lists at the top of `fetch_data.py`:

```python
FUTURES = [
    ("ES=F",  "E-mini S&P 500"),
    ("NQ=F",  "E-mini Nasdaq 100"),
    # add more here
]
```

Any valid Yahoo Finance ticker works. Find tickers at [finance.yahoo.com](https://finance.yahoo.com).

### Add breadth data (optional)

Add a `breadth` object to your `data/market_data.json`:
```json
{
  "breadth": {
    "% Above MA200": "62%",
    "% Above MA50": "48%",
    "Fear & Greed": "34 (Fear)",
    "Put/Call": "1.14"
  }
}
```

Sources for free breadth data:
- [CNN Fear & Greed](https://cnn.com/markets/fear-and-greed)
- [AAII Sentiment](https://aaii.com/sentimentsurvey)
- [BarChart Breadth](https://barchart.com/stocks/market-overview/breadth)

### Change refresh time

Edit `.github/workflows/update-data.yml`:
```yaml
- cron: '0 22 * * 1-5'  # 22:00 UTC Mon–Fri
```

---

## 🧮 Position Sizing Calculator

The calculator (Section 04) works entirely in-browser — no data needed. Enter:

- **Account equity** — your total trading account size
- **Risk %** — what % of equity you risk per trade (1–2% is standard)
- **Entry price** — your planned entry
- **Stop loss** — your invalidation level

It outputs:
- Exact share count
- Max dollar risk
- Position value & % of equity
- R:R profit targets (customizable)
- Staggered stop levels (2-stop or 3-stop)

---

## 📦 Dependencies

Python only. No paid APIs.

```
yfinance    — Yahoo Finance data (free, no API key needed)
```

Install locally:
```bash
pip install yfinance
python fetch_data.py
```

---

## ⚠️ Disclaimer

This dashboard is for informational purposes only. Not financial advice. Always do your own analysis.
