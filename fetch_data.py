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
