import os
import re
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

import mplfinance as mpf
import pandas as pd
import requests
import tvscreener as tv
import yfinance as yf
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from tvscreener import Market, StockField, StockScreener, SymbolType

load_dotenv()

ASX_BASE_URL = "https://www.asx.com.au"
DISCORD_WEBHOOK_URL = None
TZ_SYD = ZoneInfo("Australia/Sydney")
ASX_OPEN = 10
ASX_CLOSE = 16

DATA_DIR = Path("data")
CHARTS_DIR = Path("charts")

BASE_DIR = Path(__file__).resolve().parent


def ensure_dirs():
    (BASE_DIR / DATA_DIR).mkdir(exist_ok=True)
    (BASE_DIR / CHARTS_DIR).mkdir(exist_ok=True)


def fetch_intraday_data(ticker: str, date_str: str) -> pd.DataFrame | None:
    yahoo_ticker = f"{ticker}.AX"
    end_date = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
    start_date = datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)

    try:
        df = yf.download(
            yahoo_ticker,
            start=start_date.strftime("%Y-%m-%d"),
            end=end_date.strftime("%Y-%m-%d"),
            interval="1m",
            progress=False,
        )
    except Exception as e:
        print(f"  [WARN] Failed to download data for {ticker}: {e}")
        return None

    if df.empty:
        print(f"  [WARN] No intraday data for {ticker}")
        return None

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    if df.index.tz is not None:
        df.index = df.index.tz_convert(TZ_SYD)
    else:
        df.index = df.index.tz_localize("UTC").tz_convert(TZ_SYD)

    target = pd.Timestamp(date_str, tz=TZ_SYD)
    df = df[df.index.date == target.date()]

    if df.empty:
        print(f"  [WARN] No 1m data for {ticker} on {date_str}")
        return None

    df = df[df["Volume"] > 0]
    if df.empty:
        print(f"  [WARN] No volume data for {ticker} on {date_str}")
        return None

    csv_path = BASE_DIR / DATA_DIR / f"{ticker}_{date_str}.csv"
    df.to_csv(csv_path)
    print(f"  Saved CSV: {csv_path.name}")

    return df


def calculate_vwap(df: pd.DataFrame) -> pd.Series:
    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3
    cumulative_tp_vol = (typical_price * df["Volume"]).cumsum()
    cumulative_vol = df["Volume"].cumsum()
    return (cumulative_tp_vol / cumulative_vol).rename("VWAP")


def generate_chart(
    ticker: str, df: pd.DataFrame, change_pct: float, date_str: str
) -> str | None:
    df = df.copy()
    df.index.name = "Date"
    vwap = calculate_vwap(df)

    arrow = "+" if change_pct >= 0 else ""
    title = f"{ticker} | {date_str} | {arrow}{change_pct:.2f}%"

    vwap_plot = mpf.make_addplot(vwap, color="#FFD700", width=1.2, panel=0)

    save_path = str(BASE_DIR / CHARTS_DIR / f"{ticker}_{date_str}.png")

    mc = mpf.make_marketcolors(
        up="#26A69A",
        down="#EF5350",
        edge="inherit",
        wick="inherit",
        volume={"up": "#26A69A", "down": "#EF5350"},
    )
    s = mpf.make_mpf_style(
        marketcolors=mc,
        facecolor="#131722",
        figcolor="#131722",
        gridcolor="#2A2E39",
        gridstyle="--",
        gridaxis="both",
        rc={
            "axes.labelcolor": "#D1D4DC",
            "xtick.color": "#D1D4DC",
            "ytick.color": "#D1D4DC",
            "text.color": "#D1D4DC",
        },
    )

    try:
        market_open = df.index[0].replace(hour=ASX_OPEN, minute=0, second=0)
        market_close = df.index[-1].replace(hour=ASX_CLOSE, minute=0, second=0)
        xlim = (market_open, market_close)

        mpf.plot(
            df,
            type="candle",
            style=s,
            title=title,
            volume=True,
            addplot=[vwap_plot],
            figsize=(12, 7),
            xlim=xlim,
            savefig=save_path,
            tight_layout=True,
        )
        print(f"  Saved chart: {Path(save_path).name}")
        return save_path
    except Exception as e:
        print(f"  [WARN] Failed to generate chart for {ticker}: {e}")
        return None


def fetch_announcements(ticker: str) -> list[dict]:
    url = f"{ASX_BASE_URL}/asx/v2/statistics/announcements.do?by=asxCode&asxCode={ticker}&timeframe=Y&year={datetime.now().year}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    table = soup.select("table")
    if not table:
        return []

    rows = table[0].select("tr")[1:]
    results = []
    for row in rows:
        cells = row.select("td")
        if len(cells) < 3:
            continue
        link = cells[2].find("a")
        if not link:
            continue
        text_lines = cells[2].get_text().split("\n")
        headline = text_lines[2].strip() if len(text_lines) >= 3 else ""
        price_sensitive = bool(cells[1].find("img", class_="pricesens"))
        date_raw = cells[0].get_text().strip()
        results.append({
            "headline": headline,
            "price_sensitive": price_sensitive,
            "date_raw": date_raw,
        })
    return results


def find_catalyst(ticker: str, change_pct: float) -> str:
    ticker = ticker.replace("ASX:", "")
    cutoff = datetime.now() - timedelta(days=5)
    cutoff_str = cutoff.strftime("%d/%m/%Y")

    anns = fetch_announcements(ticker)
    recent = []
    for a in anns:
        date_match = re.search(r"(\d{2}/\d{2}/\d{4})", a["date_raw"])
        if date_match and date_match.group(1) >= cutoff_str:
            recent.append(a)

    if not recent:
        return "No recent ASX announcements found"

    lines = []
    for a in recent:
        ps = " [PS]" if a["price_sensitive"] else ""
        date_match = re.search(r"(\d{2}/\d{2}/\d{4})", a["date_raw"])
        date_str = date_match.group(1) if date_match else ""
        lines.append(f"  {date_str}  {a['headline']}{ps}")
    return "\n".join(lines)


def post_to_discord(sym: str, name: str, price: float, chg: float, chg_abs: float,
                    vol: float, mcap: float, sector: str, catalyst: str,
                    chart_path: str | None = None):
    global DISCORD_WEBHOOK_URL
    if not DISCORD_WEBHOOK_URL:
        return

    color = 0x00FF00 if chg >= 0 else 0xFF0000
    arrow = "+" if chg >= 0 else ""

    fields = [
        {"name": "Price", "value": f"${price:.3f}", "inline": True},
        {"name": "Change", "value": f"{arrow}{chg:.2f}% (${chg_abs:+.3f})", "inline": True},
        {"name": "Volume", "value": f"{vol:,.0f}", "inline": True},
        {"name": "Sector", "value": sector, "inline": True},
    ]

    if mcap and mcap == mcap:
        if mcap >= 1e9:
            mcap_str = f"${mcap / 1e9:.2f}B"
        elif mcap >= 1e6:
            mcap_str = f"${mcap / 1e6:.2f}M"
        else:
            mcap_str = f"${mcap:,.0f}"
        fields.append({"name": "Market Cap", "value": mcap_str, "inline": True})

    embed = {
        "title": f"{sym} - {name}",
        "url": f"https://www.tradingview.com/symbols/ASX-{sym.replace('ASX:', '')}/",
        "color": color,
        "fields": fields,
    }

    if catalyst and catalyst != "No recent ASX announcements found":
        ann_lines = catalyst.strip().split("\n")
        ann_text = "\n".join(ann_lines[:8])
        if len(ann_lines) > 8:
            ann_text += f"\n  ... and {len(ann_lines) - 8} more"
        embed["description"] = f"**Recent Announcements**\n{ann_text}"

    if chart_path and Path(chart_path).exists():
        filename = Path(chart_path).name
        embed["image"] = {"url": f"attachment://{filename}"}

    payload = {"embeds": [embed]}

    try:
        if chart_path and Path(chart_path).exists():
            filename = Path(chart_path).name
            with open(chart_path, "rb") as f:
                requests.post(
                    DISCORD_WEBHOOK_URL,
                    data={"payload_json": __import__("json").dumps(payload)},
                    files={"file": (filename, f, "image/png")},
                    timeout=30,
                )
        else:
            requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    except Exception:
        pass


def post_watchlist(tickers: list[str]):
    global DISCORD_WEBHOOK_URL
    if not DISCORD_WEBHOOK_URL:
        return

    tv_list = ",".join(tickers)

    payload = {
        "embeds": [{
            "title": "TradingView Watchlist",
            "fields": [
                {"name": "TradingView Watchlist", "value": f"```\n{tv_list}\n```", "inline": False},
            ],
            "color": 0x5865F2,
            "footer": {"text": f"Total: {len(tickers)} stocks"},
        }]
    }

    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    except Exception:
        pass


def main():
    global DISCORD_WEBHOOK_URL
    DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

    if not DISCORD_WEBHOOK_URL:
        print("WARNING: DISCORD_WEBHOOK_URL not set in .env, Discord posting disabled.")

    ensure_dirs()

    today_str = date.today().strftime("%Y-%m-%d")

    ss = StockScreener()
    ss.set_markets(Market.AUSTRALIA)
    ss.set_symbol_types(SymbolType.COMMON_STOCK, SymbolType.ETF)
    ss.where(StockField.PRICE > 0.05)
    ss.where(StockField.CHANGE_PERCENT.not_between(-8, 8))
    ss.where(StockField.AVGVALUE_TRADED_10D > 200000)
    ss.select(
        StockField.NAME,
        StockField.PRICE,
        StockField.CHANGE_PERCENT,
        StockField.CHANGE,
        StockField.VOLUME,
        StockField.MARKET_CAPITALIZATION,
        StockField.SECTOR,
    )
    ss.sort_by(StockField.CHANGE_PERCENT, ascending=False)
    ss.set_range(0, 1000)

    df = ss.get()
    if df.empty:
        print("No ASX stocks moved >= 8% today (with price > $0.05).")
        return

    symbols = df["Symbol"].tolist()

    print(f"ASX Movers (>= 8%): {len(df)} stocks found\n")

    for sym in symbols:
        row = df[df["Symbol"] == sym].iloc[0]
        ticker = sym.replace("ASX:", "")
        name = row["Name"]
        price = row["Price"]
        chg = row["Change %"]
        chg_abs = row["Change"]
        vol = row["Volume"]
        mcap = row["Market Capitalization"]
        sector = row["Sector"]

        arrow = "+" if chg >= 0 else ""
        print(f"{'='*72}")
        print(f"  {sym}  {name}")
        print(f"  Price: ${price:.3f}   Change: {arrow}{chg:.2f}% (${chg_abs:+.3f})")
        print(f"  Volume: {vol:,.0f}   MCap: ${mcap:,.0f}   Sector: {sector}")
        catalyst = find_catalyst(ticker, chg)
        print(f"  Catalyst:")
        print(catalyst)

        intraday_df = fetch_intraday_data(ticker, today_str)
        chart_path = None
        if intraday_df is not None and not intraday_df.empty:
            chart_path = generate_chart(ticker, intraday_df, chg, today_str)

        print()

        post_to_discord(sym, name, price, chg, chg_abs, vol, mcap, sector, catalyst,
                        chart_path=chart_path)

    post_watchlist(symbols)
    print(f"Watchlist posted: {len(symbols)} stocks")


if __name__ == "__main__":
    main()
