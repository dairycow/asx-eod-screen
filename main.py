import os
import re
from datetime import datetime, timedelta

import requests
import tvscreener as tvs
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from tvscreener import Market, StockField, StockScreener, SymbolType

load_dotenv()

ASX_BASE_URL = "https://www.asx.com.au"
DISCORD_WEBHOOK_URL = None


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
                    vol: float, mcap: float, sector: str, catalyst: str):
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

    payload = {
        "embeds": [{
            "title": f"{sym} - {name}",
            "url": f"https://www.tradingview.com/symbols/ASX-{sym.replace('ASX:', '')}/",
            "color": color,
            "fields": fields,
        }]
    }

    if catalyst and catalyst != "No recent ASX announcements found":
        ann_lines = catalyst.strip().split("\n")
        ann_text = "\n".join(ann_lines[:8])
        if len(ann_lines) > 8:
            ann_text += f"\n  ... and {len(ann_lines) - 8} more"
        payload["embeds"][0]["description"] = f"**Recent Announcements**\n{ann_text}"

    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    except Exception:
        pass


def post_watchlist(tickers: list[str]):
    global DISCORD_WEBHOOK_URL
    if not DISCORD_WEBHOOK_URL:
        return

    body = ", ".join(tickers)

    payload = {
        "embeds": [{
            "title": "TradingView Watchlist",
            "description": f"Copy/paste into TradingView:\n\n{body}",
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

    ss = StockScreener()
    ss.set_markets(Market.AUSTRALIA)
    ss.set_symbol_types(SymbolType.COMMON_STOCK, SymbolType.ETF)
    ss.where(StockField.PRICE > 0.05)
    ss.where(StockField.CHANGE_PERCENT.not_between(-8, 8))
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
        print()

        post_to_discord(sym, name, price, chg, chg_abs, vol, mcap, sector, catalyst)

    post_watchlist(symbols)
    print(f"Watchlist posted: {len(symbols)} stocks")


if __name__ == "__main__":
    main()
