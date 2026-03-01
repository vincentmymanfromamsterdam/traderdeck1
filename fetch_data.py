"""
TRADERDECK — Daily Market Data Fetcher
Fetches EOD data from Yahoo Finance and saves to data/market_data.json
Run: python fetch_data.py
"""

import json
import datetime
import sys

try:
    import yfinance as yf
except ImportError:
    print("Installing yfinance...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "yfinance", "-q"])
    import yfinance as yf

# ─────────────────────────────────────────────────────────
#  SYMBOLS CONFIG
# ─────────────────────────────────────────────────────────

FUTURES = [
    ("ES=F",  "E-mini S&P 500"),
    ("NQ=F",  "E-mini Nasdaq 100"),
    ("YM=F",  "E-mini Dow Jones"),
    ("RTY=F", "E-mini Russell 2000"),
]

VOL_DOLLAR = [
    ("^VIX",   "VIX Index"),
    ("^VVIX",  "VVIX"),
    ("DX-Y.NYB", "US Dollar Index"),
    ("EURUSD=X", "EUR/USD"),
    ("GBPUSD=X", "GBP/USD"),
    ("USDJPY=X", "USD/JPY"),
]

METALS = [
    ("GC=F",  "Gold Futures"),
    ("SI=F",  "Silver Futures"),
    ("HG=F",  "Copper Futures"),
    ("PL=F",  "Platinum Futures"),
]

ENERGY = [
    ("CL=F",  "WTI Crude Oil"),
    ("BZ=F",  "Brent Crude Oil"),
    ("NG=F",  "Natural Gas"),
    ("RB=F",  "RBOB Gasoline"),
]

YIELDS = [
    ("^IRX",  "13W T-Bill"),
    ("^FVX",  "5Y Treasury"),
    ("^TNX",  "10Y Treasury"),
    ("^TYX",  "30Y Treasury"),
]

GLOBAL_INDICES = [
    ("^FTSE",  "FTSE 100"),
    ("^GDAXI", "DAX"),
    ("^FCHI",  "CAC 40"),
    ("^N225",  "Nikkei 225"),
    ("^HSI",   "Hang Seng"),
    ("^AXJO",  "ASX 200"),
    ("^KS11",  "KOSPI"),
]

SECTORS = [
    ("XLK",  "Technology"),
    ("XLF",  "Financials"),
    ("XLV",  "Health Care"),
    ("XLY",  "Consumer Disc."),
    ("XLP",  "Consumer Staples"),
    ("XLE",  "Energy"),
    ("XLI",  "Industrials"),
    ("XLB",  "Materials"),
    ("XLRE", "Real Estate"),
    ("XLU",  "Utilities"),
    ("XLC",  "Comm. Services"),
]

MAJOR_ETFS = [
    ("SPY",  "S&P 500"),
    ("QQQ",  "Nasdaq 100"),
    ("IWM",  "Russell 2000"),
    ("DIA",  "Dow Jones"),
    ("GLD",  "Gold"),
    ("SLV",  "Silver"),
    ("TLT",  "20Y Treasuries"),
    ("HYG",  "High Yield Corp"),
    ("LQD",  "Investment Grade"),
    ("VNQ",  "Real Estate"),
    ("USO",  "Oil Fund"),
]

CRYPTO = [
    ("BTC-USD", "Bitcoin"),
    ("ETH-USD", "Ethereum"),
]

COUNTRY_ETFS = [
    ("EWG",  "Germany"),
    ("EWU",  "United Kingdom"),
    ("EWJ",  "Japan"),
    ("FXI",  "China"),
    ("INDA", "India"),
    ("EWZ",  "Brazil"),
    ("EWC",  "Canada"),
    ("EWA",  "Australia"),
    ("EWY",  "South Korea"),
    ("EWQ",  "France"),
]

# ─────────────────────────────────────────────────────────
#  FETCH HELPER
# ─────────────────────────────────────────────────────────

def fetch_group(symbols_with_names, label="group"):
    """Fetch a group of tickers and return structured data."""
    tickers = [s[0] for s in symbols_with_names]
    name_map = {s[0]: s[1] for s in symbols_with_names}

    print(f"  Fetching {label} ({len(tickers)} symbols)...", end=" ", flush=True)

    try:
        import datetime as dt
        # Fetch from Jan 1 of current year to get accurate YTD
        # Also need 52 weeks back, so use whichever is earlier
        today = dt.date.today()
        jan1 = dt.date(today.year, 1, 1)
        week52_ago = today - dt.timedelta(weeks=52)
        start_date = min(jan1, week52_ago).strftime("%Y-%m-%d")

        data = yf.download(
            tickers,
            start=start_date,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        print(f"ERROR: {e}")
        return []

    results = []

    for ticker in tickers:
        try:
            if len(tickers) == 1:
                df = data
            else:
                df = data[ticker] if ticker in data.columns.get_level_values(0) else None

            if df is None or df.empty or len(df) < 2:
                continue

            df = df.dropna(subset=["Close"])

            import datetime as dt
            close_today = float(df["Close"].iloc[-1])
            close_1d    = float(df["Close"].iloc[-2]) if len(df) >= 2 else close_today
            close_1w    = float(df["Close"].iloc[-6]) if len(df) >= 6 else close_1d

            # YTD: first trading day of this calendar year
            today_dt   = dt.date.today()
            jan1_str   = f"{today_dt.year}-01-01"
            df_ytd     = df[df.index >= jan1_str]
            close_ytd  = float(df_ytd["Close"].iloc[0]) if len(df_ytd) > 0 else float(df["Close"].iloc[0])

            # 52W: price from exactly 52 weeks ago (closest available)
            week52_str = (today_dt - dt.timedelta(weeks=52)).strftime("%Y-%m-%d")
            df_52w     = df[df.index <= week52_str]
            close_52w  = float(df_52w["Close"].iloc[-1]) if len(df_52w) > 0 else float(df["Close"].iloc[0])

            chg_1d  = round((close_today - close_1d)  / close_1d  * 100, 2)
            chg_1w  = round((close_today - close_1w)  / close_1w  * 100, 2)
            chg_52w = round((close_today - close_52w) / close_52w * 100, 2)
            chg_ytd = round((close_today - close_ytd) / close_ytd * 100, 2)

            # 5-day spark data
            spark_raw = df["Close"].iloc[-6:-1].tolist() if len(df) >= 6 else df["Close"].iloc[-5:].tolist()
            spark = [round(float(v), 4) for v in spark_raw]

            # previous closes for sparkline pct changes
            spark_chgs = []
            for i in range(1, len(spark)):
                c = round((spark[i] - spark[i-1]) / spark[i-1] * 100, 2)
                spark_chgs.append(c)

            results.append({
                "ticker": ticker,
                "name": name_map.get(ticker, ticker),
                "price": round(close_today, 4),
                "chg_1d": chg_1d,
                "chg_1w": chg_1w,
                "chg_52w_hi": chg_52w,
                "chg_ytd": chg_ytd,
                "spark": spark_chgs,
            })

        except Exception as e:
            print(f"\n    Warning: could not process {ticker}: {e}")

    print(f"OK ({len(results)}/{len(tickers)})")
    return results


def fetch_yields_group():
    """Treasury yields need special handling (values are percentages)."""
    symbols_with_names = YIELDS
    print(f"  Fetching yields...", end=" ", flush=True)

    tickers = [s[0] for s in symbols_with_names]
    name_map = {s[0]: s[1] for s in symbols_with_names}
    labels   = {
        "^IRX": "3M",
        "^FVX": "5Y",
        "^TNX": "10Y",
        "^TYX": "30Y",
    }

    try:
        import datetime as dt
        today_dt   = dt.date.today()
        jan1_str   = f"{today_dt.year}-01-01"
        week52_ago = (today_dt - dt.timedelta(weeks=52)).strftime("%Y-%m-%d")
        start_date = min(jan1_str, week52_ago)

        data = yf.download(tickers, start=start_date, interval="1d",
                           group_by="ticker", auto_adjust=True,
                           progress=False, threads=True)
    except Exception as e:
        print(f"ERROR: {e}")
        return []

    results = []
    for ticker in tickers:
        try:
            df = data[ticker] if len(tickers) > 1 else data
            df = df.dropna(subset=["Close"])
            if df.empty or len(df) < 2:
                continue

            import datetime as dt
            today_dt   = dt.date.today()

            yield_today = float(df["Close"].iloc[-1])
            yield_1d    = float(df["Close"].iloc[-2])
            yield_1w    = float(df["Close"].iloc[-6]) if len(df) >= 6 else yield_1d

            df_ytd    = df[df.index >= f"{today_dt.year}-01-01"]
            yield_ytd = float(df_ytd["Close"].iloc[0]) if len(df_ytd) > 0 else float(df["Close"].iloc[0])

            week52_str = (today_dt - dt.timedelta(weeks=52)).strftime("%Y-%m-%d")
            df_52w     = df[df.index <= week52_str]
            yield_52w  = float(df_52w["Close"].iloc[-1]) if len(df_52w) > 0 else float(df["Close"].iloc[0])

            chg_1d_bps  = round((yield_today - yield_1d) * 100, 1)   # basis points
            chg_1w      = round((yield_today - yield_1w)  / yield_1w  * 100, 2)
            chg_52w     = round((yield_today - yield_52w) / yield_52w * 100, 2)
            chg_ytd     = round((yield_today - yield_ytd) / yield_ytd * 100, 2)

            results.append({
                "ticker": ticker,
                "tenor": labels.get(ticker, ticker),
                "name": name_map.get(ticker, ticker),
                "yield_pct": round(yield_today, 2),
                "chg_1d_bps": chg_1d_bps,
                "chg_1w": chg_1w,
                "chg_52w_hi": chg_52w,
                "chg_ytd": chg_ytd,
            })
        except Exception as e:
            print(f"\n    Warning: {ticker}: {e}")

    print(f"OK ({len(results)}/{len(tickers)})")
    return results

# ─────────────────────────────────────────────────────────
#  BREADTH
# ─────────────────────────────────────────────────────────

SP500_TICKERS = [
    "MMM","AOS","ABT","ABBV","ACN","ADBE","AMD","AES","AFL","A","APD","ABNB","AKAM","ALB","ARE",
    "ALGN","ALLE","LNT","ALL","GOOGL","GOOG","MO","AMZN","AMCR","AEE","AAL","AEP","AXP","AIG",
    "AMT","AWK","AMP","AME","AMGN","APH","ADI","ANSS","AON","APA","AAPL","AMAT","APTV","ACGL",
    "ADM","ANET","AJG","AIZ","T","ATO","ADSK","ADP","AZO","AVB","AVY","AXON","BKR","BALL","BAC",
    "BK","BBWI","BAX","BDX","BRK-B","BBY","BIO","TECH","BIIB","BLK","BX","BA","BCR","BSX","BMY",
    "AVGO","BR","BRO","BF-B","BLDR","BG","CDNS","CZR","CPT","CPB","COF","CAH","KMX","CCL","CARR",
    "CTLT","CAT","CBOE","CBRE","CDW","CE","COR","CNC","CNP","CF","CRL","SCHW","CHTR","CVX","CMG",
    "CB","CHD","CI","CINF","CTAS","CSCO","C","CFG","CLX","CME","CMS","KO","CTSH","CL","CMCSA",
    "CMA","CAG","COP","ED","STZ","CEG","COO","CPRT","GLW","CTVA","CSGP","COST","CTRA","CCI","CSX",
    "CMI","CVS","DHI","DHR","DRI","DVA","DAY","DE","DAL","XRAY","DVN","DXCM","FANG","DLR","DFS",
    "DG","DLTR","D","DPZ","DOV","DOW","DHR","DTE","DUK","DD","EMN","ETN","EBAY","ECL","EIX","EW",
    "EA","ELV","EMR","ENPH","ETR","EOG","EPAM","EQT","EFX","EQIX","EQR","ESS","EL","ETSY","EVRG",
    "ES","EXC","EXPE","EXPD","EXR","XOM","FFIV","FDS","FICO","FAST","FRT","FDX","FIS","FITB","FSLR",
    "FE","FI","FLT","FMC","F","FTNT","FTV","FOXA","FOX","BEN","FCX","GRMN","IT","GE","GEHC","GEV",
    "GEN","GNRC","GD","GIS","GM","GPC","GILD","GPN","GL","GDDY","GS","HAL","HIG","HAS","HCA","DOC",
    "HSIC","HSY","HES","HPE","HLT","HOLX","HD","HON","HRL","HST","HWM","HPQ","HUBB","HUM","HBAN",
    "HII","IBM","IEX","IDXX","ITW","INCY","IR","PODD","INTC","ICE","IFF","IP","IPG","INTU","ISRG",
    "IVZ","INVH","IQV","IRM","JBHT","JBL","JKHY","J","JNJ","JCI","JPM","JNPR","K","KVUE","KDP",
    "KEY","KEYS","KMB","KIM","KMI","KLAC","KHC","KR","LHX","LH","LRCX","LW","LVS","LDOS","LEN",
    "LII","LLY","LIN","LYV","LKQ","LMT","L","LOW","LULU","LYB","MTB","MRO","MPC","MKTX","MAR",
    "MMC","MLM","MAS","MA","MTCH","MKC","MCD","MCK","MDT","MRK","META","MET","MTD","MGM","MCHP",
    "MU","MSFT","MAA","MRNA","MHK","MOH","TAP","MDLZ","MPWR","MNST","MCO","MS","MOS","MSI","MSCI",
    "NDAQ","NTAP","NOV","NWSA","NWS","NEE","NKE","NEM","NFLX","NWL","NI","NDSN","NSC","NTRS","NOC",
    "NCLH","NRG","NUE","NVDA","NVR","NXPI","ORLY","OXY","ODFL","OMC","ON","OKE","ORCL","OTIS","OC",
    "PCAR","PKG","PANW","PARA","PH","PAYX","PAYC","PYPL","PNR","PEP","PFE","PCG","PM","PSX","PNW",
    "PXD","PNC","POOL","PPG","PPL","PFG","PG","PGR","PLD","PRU","PEG","PTC","PSA","PHM","QRVO",
    "PWR","QCOM","DGX","RL","RJF","RTX","O","REG","REGN","RF","RSG","RMD","RVTY","ROK","ROL","ROP",
    "ROST","RCL","SPGI","CRM","SBAC","SLB","STX","SRE","NOW","SHW","SPG","SWKS","SJM","SNA","SOLV",
    "SO","LUV","SWK","SBUX","STT","STLD","STE","SYK","SMCI","SYF","SNPS","SYY","TMUS","TROW","TTWO",
    "TPR","TRGP","TGT","TEL","TDY","TFX","TER","TSLA","TXN","TXT","TMO","TJX","TSCO","TT","TDG",
    "TRV","TRMB","TFC","TYL","TSN","USB","UBER","UDR","ULTA","UNP","UAL","UPS","URI","UNH","UHS",
    "VLO","VTR","VLTO","VRSN","VRSK","VZ","VRTX","VTRS","VICI","V","VST","VMC","WRB","GWW","WAB",
    "WBA","WMT","DIS","WBD","WM","WAT","WEC","WFC","WELL","WST","WDC","WY","WHR","WMB","WTW","WYNN",
    "XEL","XYL","YUM","ZBRA","ZBH","ZTS"
]

def fetch_breadth():
    """Fetch S&P 500 breadth: % above MA50/MA200, 52W highs/lows."""
    import datetime as dt
    print(f"  Fetching S&P 500 breadth ({len(SP500_TICKERS)} stocks)...", end=" ", flush=True)

    today = dt.date.today()
    start = (today - dt.timedelta(days=220)).strftime("%Y-%m-%d")  # 200 trading days back

    try:
        data = yf.download(
            SP500_TICKERS,
            start=start,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        print(f"ERROR: {e}")
        return {}

    above_50, above_200, highs_52w, lows_52w, total = 0, 0, 0, 0, 0

    for ticker in SP500_TICKERS:
        try:
            if len(SP500_TICKERS) == 1:
                df = data
            else:
                df = data[ticker] if ticker in data.columns.get_level_values(0) else None
            if df is None or df.empty:
                continue
            df = df.dropna(subset=["Close"])
            if len(df) < 10:
                continue

            close = float(df["Close"].iloc[-1])

            ma50  = float(df["Close"].iloc[-50:].mean())  if len(df) >= 50  else None
            ma200 = float(df["Close"].iloc[-200:].mean()) if len(df) >= 200 else None
            hi52  = float(df["Close"].rolling(252).max().iloc[-1]) if len(df) >= 50 else float(df["Close"].max())
            lo52  = float(df["Close"].rolling(252).min().iloc[-1]) if len(df) >= 50 else float(df["Close"].min())

            total += 1
            if ma50  and close > ma50:  above_50  += 1
            if ma200 and close > ma200: above_200 += 1
            # 52W high/low: within 1% of annual extreme
            if close >= hi52 * 0.99: highs_52w += 1
            if close <= lo52 * 1.01: lows_52w  += 1

        except Exception:
            continue

    if total == 0:
        print("ERROR: no data")
        return {}

    pct_above_50  = round(above_50  / total * 100, 1)
    pct_above_200 = round(above_200 / total * 100, 1)

    print(f"OK ({total} stocks processed)")
    return {
        "pct_above_ma50":  pct_above_50,
        "pct_above_ma200": pct_above_200,
        "new_52w_highs":   highs_52w,
        "new_52w_lows":    lows_52w,
        "total_stocks":    total,
    }


def fetch_fear_greed():
    """Fetch CNN Fear & Greed index via their unofficial API."""
    import urllib.request
    print("  Fetching Fear & Greed...", end=" ", flush=True)
    try:
        url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.cnn.com/",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = json.loads(r.read().decode())
        score = round(float(raw["fear_and_greed"]["score"]), 1)
        rating = raw["fear_and_greed"]["rating"].replace("_", " ").title()
        prev   = round(float(raw["fear_and_greed"]["previous_close"]), 1)
        print(f"OK ({score} — {rating})")
        return {"score": score, "rating": rating, "previous_close": prev}
    except Exception as e:
        print(f"ERROR: {e}")
        return {}


def fetch_put_call():
    """Fetch CBOE total put/call ratio from their website."""
    import urllib.request, re
    print("  Fetching Put/Call ratio...", end=" ", flush=True)
    try:
        url = "https://www.cboe.com/us/options/market_statistics/daily/"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="ignore")
        # CBOE page has the total P/C ratio in a table
        # Look for "Total" row with a decimal number
        match = re.search(r'Total[^<]*</td>\s*<td[^>]*>([\d.]+)</td>\s*<td[^>]*>([\d.]+)</td>\s*<td[^>]*>([\d.]+)</td>', html)
        if match:
            total_pc = float(match.group(3))  # Total P/C ratio
            print(f"OK ({total_pc})")
            return {"total": total_pc, "equity": None, "index": None}
        # Fallback: scan for any ratio-looking number near "total"
        matches = re.findall(r'([\d]+\.[\d]+)', html[html.lower().find("total"):html.lower().find("total")+500])
        ratios = [float(m) for m in matches if 0.3 < float(m) < 3.0]
        if ratios:
            print(f"OK ({ratios[0]})")
            return {"total": ratios[0], "equity": None, "index": None}
        print("WARN: could not parse ratio")
        return {}
    except Exception as e:
        print(f"ERROR: {e}")
        return {}


# ─────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────

def main():
    print("\n" + "─"*50)
    print("  TRADERDECK — Market Data Fetcher")
    print("─"*50)

    now = datetime.datetime.utcnow()
    print(f"  Timestamp: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    payload = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
        "updated_date": now.strftime("%Y-%m-%d"),
        "futures":        fetch_group(FUTURES,        "US Futures"),
        "vol_dollar":     fetch_group(VOL_DOLLAR,     "Vol & Dollar"),
        "metals":         fetch_group(METALS,         "Metals"),
        "energy":         fetch_group(ENERGY,         "Energy"),
        "yields":         fetch_yields_group(),
        "global_indices": fetch_group(GLOBAL_INDICES, "Global Indices"),
        "sectors":        fetch_group(SECTORS,        "S&P Sectors"),
        "major_etfs":     fetch_group(MAJOR_ETFS,     "Major ETFs"),
        "crypto":         fetch_group(CRYPTO,         "Crypto"),
        "country_etfs":   fetch_group(COUNTRY_ETFS,   "Country ETFs"),
        "breadth":        fetch_breadth(),
        "fear_greed":     fetch_fear_greed(),
        "put_call":       fetch_put_call(),
    }

    # Sort sectors by 1W performance
    payload["sectors"].sort(key=lambda x: x.get("chg_1w", 0), reverse=True)
    payload["country_etfs"].sort(key=lambda x: x.get("chg_1w", 0), reverse=True)

    # Write output
    out_path = "data/market_data.json"
    import os
    os.makedirs("data", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"\n  ✓ Saved to {out_path}")
    print(f"  ✓ Total symbols: {sum(len(v) for v in payload.values() if isinstance(v, list))}")
    print("─"*50 + "\n")

if __name__ == "__main__":
    main()
