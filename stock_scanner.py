import os
import time
from datetime import datetime, timezone

import pandas as pd
import requests
import yfinance as yf
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
db = create_client(SUPABASE_URL, SUPABASE_KEY)

WATCHLIST_FILE = "watchlist.csv"
ALERT_SCORE = 65

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    al = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    return 100 - 100 / (1 + ag / al)

def analyze(ticker):
    df = yf.download(ticker, period="1y", interval="1d",
                     auto_adjust=True, progress=False)
    if df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df["rsi"] = rsi(df["Close"])
    df["ma20"] = df["Close"].rolling(20).mean()
    df["ma50"] = df["Close"].rolling(50).mean()
    df["high60"] = df["High"].rolling(60).max()
    df["avgvol"] = df["Volume"].rolling(20).mean()
    df = df.dropna()
    if len(df) < 2:
        return None

    a, b = df.iloc[-1], df.iloc[-2]
    price = float(a["Close"])
    rv = float(a["rsi"])
    prev = float(b["rsi"])
    ma20 = float(a["ma20"])
    ma50 = float(a["ma50"])
    dd = (price / float(a["high60"]) - 1) * 100
    vr = float(a["Volume"]) / float(a["avgvol"])

    score, cond = 0, []
    if rv < 40: score += 20; cond.append("RSI<40")
    if rv < 35: score += 10; cond.append("RSI<35")
    if rv > prev: score += 20; cond.append("RSI반등")
    if dd <= -10: score += 15; cond.append("고점대비-10%")
    if abs(price / ma20 - 1) <= .03: score += 10; cond.append("20일선근접")
    if price > ma50: score += 10; cond.append("50일선위")
    if vr >= 1.2: score += 10; cond.append("거래량증가")

    return dict(ticker=ticker, price=price, rsi=rv, prev_rsi=prev,
                ma20=ma20, ma50=ma50, drawdown=dd,
                volume_ratio=vr, score=score, conditions=cond)

def save_daily(x, date):
    db.table("daily_data").upsert({
        "date": date, "ticker": x["ticker"], "price": x["price"],
        "rsi": x["rsi"], "prev_rsi": x["prev_rsi"],
        "ma20": x["ma20"], "ma50": x["ma50"],
        "drawdown": x["drawdown"], "volume_ratio": x["volume_ratio"],
        "score": x["score"]
    }).execute()

def save_signal(x, date):
    if x["score"] < ALERT_SCORE:
        return
    db.table("signals").upsert({
        "signal_date": date, "ticker": x["ticker"],
        "signal_price": x["price"], "score": x["score"],
        "rsi": x["rsi"], "drawdown": x["drawdown"]
    }, on_conflict="signal_date,ticker").execute()

def update_returns():
    signals = db.table("signals").select("*").execute().data or []
    for s in signals:
        rows = db.table("daily_data").select("date,price") \
            .eq("ticker", s["ticker"]).gt("date", s["signal_date"]) \
            .order("date").limit(20).execute().data or []
        updates = {}
        for n, key in [(5,"return_5d"),(10,"return_10d"),(20,"return_20d")]:
            if len(rows) >= n:
                updates[key] = (rows[n-1]["price"] / s["signal_price"] - 1) * 100
        if updates:
            db.table("signals").update(updates).eq("id", s["id"]).execute()

def telegram(msg):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(msg); return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": msg}, timeout=15)
    r.raise_for_status()

def main():
    # 한국시간 날짜가 아니라 실제 실행일을 DB 기준 날짜로 사용.
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    candidates = []

    for ticker in pd.read_csv(WATCHLIST_FILE)["ticker"]:
        try:
            x = analyze(ticker)
            if x:
                save_daily(x, date)
                save_signal(x, date)
                if x["score"] >= ALERT_SCORE:
                    candidates.append(x)
            time.sleep(1)
        except Exception as e:
            print(ticker, "오류:", e)

    update_returns()

    if candidates:
        candidates.sort(key=lambda x: x["score"], reverse=True)
        msg = f"📊 미국주식 매수 후보\n📅 {date}\n\n"
        for x in candidates:
            msg += (f"{'🔥' if x['score']>=80 else '🟢'} {x['ticker']} "
                    f"{x['score']}점\n"
                    f"가격 ${x['price']:.2f} | RSI {x['rsi']:.1f}\n"
                    f"고점대비 {x['drawdown']:.1f}%\n"
                    f"조건: {', '.join(x['conditions'])}\n\n")
        telegram(msg)

if __name__ == "__main__":
    main()
