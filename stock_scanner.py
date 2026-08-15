"""미국주식 매수신호 스캐너.

GitHub Actions가 평일마다 실행해 watchlist 종목의 기술적 신호를 스코어링하고,
ALERT_SCORE 이상인 경우 signals 테이블에 저장한 뒤 Telegram으로 알림을 보낸다.
신호의 5/10/20일 수익률은 이후 배치로 갱신된다.
"""

import argparse
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd
import requests
import yfinance as yf
from pandas.tseries.holiday import (AbstractHolidayCalendar, GoodFriday,
                                    USFederalHolidayCalendar)
from supabase import create_client

WATCHLIST_FILE = "watchlist.csv"
ALERT_SCORE = 65  # V5 백테스트 후 조정 권장
ALERT_COOLDOWN_DAYS = 5
STALE_DATA_DAYS = 7
PRUNE_RETENTION_DAYS = 365
SCAN_WORKERS = 8  # 종목별 병렬 분석 워커 수 (Yahoo 스로틀링을 겸함)

_db: Any = None


def get_db() -> Any:
    """지연 초기화되는 Supabase 클라이언트 (import 시 env 변수 불필요)."""
    global _db
    if _db is None:
        _db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    return _db


# ---------------------------------------------------------------------------
# 기술 지표
# ---------------------------------------------------------------------------


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI (RMA 평활 + 첫 period 단순평균 시드).

    ewm(alpha=1/period) 근사치가 아닌, Wilder가 정의한 대로
    첫 평균을 단순이동평균으로 시드하고 이후 재귀적으로 평활한다.
    """
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = gain.copy()
    avg_loss = loss.copy()
    if len(series) <= period:
        return pd.Series(float("nan"), index=series.index)

    # 시드 이전 행은 무효화 (dropna가 걸러내도록)
    avg_gain.iloc[:period] = float("nan")
    avg_loss.iloc[:period] = float("nan")

    # 시드: 1..period 구간의 단순평균 (diff로 인해 행 0은 NaN)
    avg_gain.iloc[period] = gain.iloc[1 : period + 1].mean()
    avg_loss.iloc[period] = loss.iloc[1 : period + 1].mean()

    # Wilder 재귀 평활: avg = (prev * (period - 1) + cur) / period
    for i in range(period + 1, len(series)):
        avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
        avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period

    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def fetch_history(ticker: str, retries: int = 3, backoff: float = 2.0) -> pd.DataFrame:
    """Yahoo Finance에서 1년 일봉 데이터를 받아온다 (재시도 + 지수 백오프)."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            df = yf.download(ticker, period="1y", interval="1d",
                             auto_adjust=True, progress=False)
            if not df.empty and isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            return df
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(backoff ** attempt)
    raise RuntimeError(f"{ticker} 데이터 조회 실패 ({retries}회): {last}") from last


def score_signal(price: float, rv: float, prev: float,
                 ma20: float, ma50: float, dd: float, vr: float,
                 ma50_prev: float | None = None,
                 prev_price: float | None = None) -> tuple[int, list[str]]:
    """기술지표 → (0~100점, 조건목록).

    V5 점수는 단순히 '많이 떨어졌는가'보다
    '과매도 + 반등 조짐 + 중기 추세 유지 + 적당한 눌림'을 높게 평가한다.

    배점:
      RSI 상태       20
      RSI 반등       20
      고점대비       15
      MA20 눌림      15
      중기 추세      20
      거래량/반등    10
      총합          100
    """
    score, cond = 0, []

    # 1) RSI 상태: 극단적 과매도보다 25~35 부근을 선호.
    if 30 <= rv < 35:
        score += 20
        cond.append("RSI30~35")
    elif 25 <= rv < 30:
        score += 17
        cond.append("RSI25~30")
    elif 35 <= rv < 40:
        score += 14
        cond.append("RSI35~40")
    elif 20 <= rv < 25:
        score += 10
        cond.append("RSI20~25")
    elif rv < 20:
        score += 5
        cond.append("RSI<20")
    elif 40 <= rv < 45:
        score += 5
        cond.append("RSI40~45")

    # 2) RSI 반등: 단순 과매도보다 반등 폭을 중요하게 본다.
    delta_rsi = rv - prev
    if delta_rsi >= 5:
        score += 20
        cond.append("RSI강한반등")
    elif delta_rsi >= 2:
        score += 15
        cond.append("RSI반등")
    elif delta_rsi > 0:
        score += 10
        cond.append("RSI소폭반등")

    # 3) 60거래일 고점 대비 눌림: 적당한 조정을 선호하고 급락은 제한.
    if -20 <= dd <= -10:
        score += 15
        cond.append("고점대비-10~-20%")
    elif -10 < dd <= -5:
        score += 8
        cond.append("고점대비-5~-10%")
    elif -25 <= dd < -20:
        score += 7
        cond.append("고점대비-20~-25%")
    elif dd < -25:
        score += 2
        cond.append("고점대비-25%이상급락")

    # 4) MA20: 20일선에서 눌림/접근한 구간을 선호.
    ma20_gap = (price / ma20 - 1) * 100
    if abs(ma20_gap) <= 2:
        score += 15
        cond.append("20일선근접")
    elif abs(ma20_gap) <= 5:
        score += 10
        cond.append("20일선5%이내")
    elif abs(ma20_gap) <= 8:
        score += 5
        cond.append("20일선8%이내")

    # 5) 중기 추세: 가격이 MA50 위이고 MA50 자체도 상승하는 종목을 선호.
    if price > ma50:
        score += 8
        cond.append("50일선위")
    if ma50_prev is not None and ma50 > ma50_prev:
        score += 7
        cond.append("50일선상승")
    if price > ma20 > ma50:
        score += 5
        cond.append("MA20>MA50")

    # 6) 거래량: 무조건 거래량 폭증보다 가격 반등과 동반된 경우를 선호.
    price_up = prev_price is None or price > prev_price
    if vr >= 1.5 and price_up:
        score += 10
        cond.append("반등+거래량1.5배")
    elif vr >= 1.2 and price_up:
        score += 7
        cond.append("반등+거래량증가")
    elif vr >= 1.2:
        score += 3
        cond.append("거래량증가")

    return min(score, 100), cond


def compute_signal(ticker: str, df: pd.DataFrame) -> dict[str, Any] | None:
    """가격 데이터프레임에서 V5 신호를 계산한다 (순수 함수)."""
    if df.empty:
        return None

    df = df.copy()
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
    ma50_prev = float(b["ma50"])
    dd = (price / float(a["high60"]) - 1) * 100
    vr = float(a["Volume"]) / float(a["avgvol"])
    prev_price = float(b["Close"])

    score, cond = score_signal(
        price, rv, prev, ma20, ma50, dd, vr,
        ma50_prev=ma50_prev, prev_price=prev_price
    )
    return dict(ticker=ticker, price=price, rsi=rv, prev_rsi=prev,
                ma20=ma20, ma50=ma50, drawdown=dd,
                volume_ratio=vr, score=score, conditions=cond,
                data_date=str(df.index[-1].date()) if hasattr(df.index[-1], "date") else str(df.index[-1])[:10])


def analyze(ticker: str, date: str | None = None) -> dict[str, Any] | None:
    """ticker의 데이터를 받아 compute_signal()로 신호를 계산한다.

    최근 데이터가 STALE_DATA_DAYS일보다 오래됐으면 스킵한다.
    """
    df = fetch_history(ticker)
    if df.empty:
        return None
    as_of = (datetime.strptime(date, "%Y-%m-%d").date()
             if date else datetime.now(timezone.utc).date())
    last = df.index.max()
    if hasattr(last, "date"):
        age = (as_of - last.date()).days
        if age > STALE_DATA_DAYS:
            print(f"{ticker}: 최근 데이터가 {age}일 전 ({last.date()}) - 스킵")
            return None
    return compute_signal(ticker, df)


# ---------------------------------------------------------------------------
# 저장
# ---------------------------------------------------------------------------


def save_daily(x: dict[str, Any], date: str) -> None:
    get_db().table("daily_data").upsert({
        "date": date, "ticker": x["ticker"], "price": x["price"],
        "rsi": x["rsi"], "prev_rsi": x["prev_rsi"],
        "ma20": x["ma20"], "ma50": x["ma50"],
        "drawdown": x["drawdown"], "volume_ratio": x["volume_ratio"],
        "score": x["score"], "score_version": 5,
    }).execute()


def save_signal(x: dict[str, Any], date: str, threshold: int = ALERT_SCORE) -> None:
    if x["score"] < threshold:
        return
    get_db().table("signals").upsert({
        "signal_date": date, "ticker": x["ticker"],
        "signal_price": x["price"], "score": x["score"], "score_version": 5,
        "rsi": x["rsi"], "drawdown": x["drawdown"],
    }, on_conflict="signal_date,ticker").execute()


def _date_key(value: Any) -> str:
    """Supabase date 필드를 'YYYY-MM-DD' 문자열로 정규화."""
    return str(value)[:10]


def _fetch_all(query, page_size: int = 1000) -> list[dict[str, Any]]:
    """Supabase 기본 1000행 제한을 피해 range() 페이지네이션으로 전부 조회한다."""
    rows: list[dict[str, Any]] = []
    start = 0
    while True:
        page = query.range(start, start + page_size - 1).execute().data or []
        rows.extend(page)
        if len(page) < page_size:
            break
        start += page_size
    return rows


def update_returns() -> None:
    """미완료 신호(return_20d가 비어 있음)의 5/10/20일 수익률을 갱신한다."""
    db = get_db()
    signals = _fetch_all(db.table("signals").select("*").is_("return_20d", None))
    if not signals:
        return

    tickers = {s["ticker"] for s in signals}
    min_date = min(_date_key(s["signal_date"]) for s in signals)
    rows = _fetch_all(db.table("daily_data")
                      .select("date,ticker,price")
                      .in_("ticker", list(tickers))
                      .gte("date", min_date)
                      .order("date"))

    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_ticker[r["ticker"]].append(r)

    to_update: list[dict[str, Any]] = []
    for s in signals:
        series = by_ticker.get(s["ticker"], [])
        after = [r for r in series if _date_key(r["date"]) > _date_key(s["signal_date"])]
        updates: dict[str, Any] = {}
        for n, key in [(5, "return_5d"), (10, "return_10d"), (20, "return_20d")]:
            if len(after) >= n and s.get("signal_price"):
                updates[key] = (after[n - 1]["price"] / s["signal_price"] - 1) * 100
        if updates:
            updates["id"] = s["id"]
            to_update.append(updates)

    if to_update:
        db.table("signals").upsert(to_update, on_conflict="id").execute()


def prune_daily_data(days: int = PRUNE_RETENTION_DAYS) -> int:
    """PRUNE_RETENTION_DAYS일보다 오래된 daily_data 행을 삭제하고 개수를 반환한다."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    res = (get_db().table("daily_data")
           .delete()
           .lt("date", cutoff)
           .execute())
    return len(res.data or [])


# ---------------------------------------------------------------------------
# 알림
# ---------------------------------------------------------------------------


def telegram(msg: str) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(msg)
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": msg}, timeout=15)
    r.raise_for_status()


def recent_alert_tickers(db: Any, date: str, tickers: list[str]) -> set[str]:
    """ALERT_COOLDOWN_DAYS 이내에 이미 신호가 저장된 ticker 집합 (당일 제외)."""
    if not tickers:
        return set()
    since = (datetime.strptime(date, "%Y-%m-%d")
             - timedelta(days=ALERT_COOLDOWN_DAYS)).strftime("%Y-%m-%d")
    rows = (db.table("signals")
            .select("ticker")
            .in_("ticker", tickers)
            .gte("signal_date", since)
            .lt("signal_date", date)
            .execute().data or [])
    return {r["ticker"] for r in rows}


def filter_recent_alerts(candidates: list[dict[str, Any]],
                         recent_tickers: set[str]) -> list[dict[str, Any]]:
    """최근에 알림을 보낸 ticker를 후보에서 제외한다."""
    return [c for c in candidates if c["ticker"] not in recent_tickers]


# ---------------------------------------------------------------------------
# watchlist
# ---------------------------------------------------------------------------


def load_watchlist(db: Any | None = None) -> list[str]:
    """Supabase watchlist 테이블을 우선 사용하고, 비어 있으면 CSV로 시드한다.

    테이블에 행이 있으나 전부 비활성화면 []를 반환한다 (CSV 폴백 없음).
    db=None이면 (DB 미사용 모드) 조회/시드 없이 CSV만 사용한다.
    """
    if db is not None:
        try:
            rows = (db.table("watchlist")
                    .select("ticker", "active")
                    .execute().data or [])
        except Exception as e:  # noqa: BLE001 - 테이블 미존재 등, CSV로 폴백
            print("watchlist 조회 실패, CSV로 대체:", e)
            rows = None
        if rows is not None:
            if rows:
                return [r["ticker"] for r in rows if r.get("active")]
            return _seed_from_csv(db)
    return _read_csv_tickers()


def _read_csv_tickers() -> list[str]:
    df = pd.read_csv(WATCHLIST_FILE)
    return df["ticker"].tolist()


def _seed_from_csv(db: Any) -> list[str]:
    df = pd.read_csv(WATCHLIST_FILE)
    seed = [{"ticker": r["ticker"], "name": r["name"]} for _, r in df.iterrows()]
    try:
        db.table("watchlist").upsert(seed, on_conflict="ticker").execute()
    except Exception as e:  # noqa: BLE001
        print("watchlist 시드 실패(무시):", e)
    return df["ticker"].tolist()


# ---------------------------------------------------------------------------
# 미국 시장 휴일
# ---------------------------------------------------------------------------

# NYSE 휴장일 캘린더. USFederalHolidayCalendar에는 콜럼버스 데이/재향군인의 날이
# 포함되지만 NYSE는 휴장하지 않으므로 제외하고, 캘린더에 없는 굿 프라이데이를 추가한다.
_US_MARKET_HOLIDAYS = AbstractHolidayCalendar(
    rules=[r for r in USFederalHolidayCalendar.rules
           if r.name not in ("Columbus Day", "Veterans Day")]
    + [GoodFriday]
)


def is_us_market_holiday(d: date) -> bool:
    """미국 증시 휴장일 여부 (주말 제외, NYSE 공식 휴일 기준)."""
    if d.weekday() >= 5:
        return False
    return _US_MARKET_HOLIDAYS.holidays(start=d, end=d).size > 0


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------


def build_alert_message(candidates: list[dict[str, Any]], date: str) -> str:
    """후보 목록을 스코어 내림차순으로 정렬해 텔레그램 메시지로 포맷한다."""
    ordered = sorted(candidates, key=lambda x: x["score"], reverse=True)
    msg = f"📊 미국주식 매수 후보\n📅 {date}\n\n"
    for x in ordered:
        msg += (f"{'🔥' if x['score'] >= 80 else '🟢'} {x['ticker']} "
                f"{x['score']}점\n"
                f"가격 ${x['price']:.2f} | RSI {x['rsi']:.1f}\n"
                f"고점대비 {x['drawdown']:.1f}%\n"
                f"조건: {', '.join(x['conditions'])}\n\n")
    return msg


def scan(date: str | None = None,
         persist: bool = True,
         notify: bool = True,
         threshold: int = ALERT_SCORE) -> tuple[list[dict[str, Any]], list[str]]:
    """스캔 파이프라인을 실행하고 (후보 목록, 실패 ticker 목록)을 반환한다.

    - persist=False: DB 저장/수익률 갱신 생략 (Supabase 미사용, 분석만)
    - notify=False: 텔레그램 대신 콘솔에 알림 메시지를 출력
    """
    # 한국시간 날짜가 아니라 실제 실행일을 DB 기준 날짜로 사용.
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if is_us_market_holiday(datetime.strptime(date, "%Y-%m-%d").date()):
        print(f"[{date}] 미국 시장 휴일 — 스캔 생략")
        return [], []
    db = get_db() if persist else None
    tickers = load_watchlist(db)

    def _process(ticker: str) -> tuple[str, dict[str, Any] | None, Exception | None]:
        try:
            x = analyze(ticker, date)
            if x and persist:
                market_date = x.get("data_date", date)
                save_daily(x, market_date)
                save_signal(x, market_date, threshold)
            return ticker, x, None
        except Exception as e:
            return ticker, None, e

    candidates: list[dict[str, Any]] = []
    failures: list[str] = []
    # 종목별 분석을 병렬로 수행한다. 워커 수 제한이 Yahoo 스로틀링을 대신한다.
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        for ticker, x, err in pool.map(_process, tickers):
            if err is not None:
                failures.append(ticker)
                print(ticker, "오류:", err)
            elif x and x["score"] >= threshold:
                candidates.append(x)

    if persist:
        update_returns()

    # 중복 알림 방지: 최근 5일 이내 신호가 있던 종목은 텔레그램에서 제외
    if notify and db is not None:
        alert_date = max((c.get("data_date", date) for c in candidates), default=date)
        recent = recent_alert_tickers(db, alert_date, [c["ticker"] for c in candidates])
        candidates = filter_recent_alerts(candidates, recent)

    if candidates:
        alert_date = max(c.get("data_date", date) for c in candidates)
        msg = build_alert_message(candidates, alert_date)
        if notify:
            try:
                telegram(msg)
            except Exception as e:
                print("텔레그램 전송 실패:", e)
        else:
            print(msg)
    else:
        # 텔레그램은 보내지 않되, 스캔이 정상 실행됐음을 로그로 남긴다
        # (실패한 것인지 후보가 없는 것인지 구분용)
        if failures:
            print(f"[{date}] 후보 0건 (실패 {len(failures)}개: {', '.join(failures)})")
        else:
            print(f"[{date}] 후보 0건 ({threshold}점 이상 종목 없음)")
    return candidates, failures


def main() -> None:
    parser = argparse.ArgumentParser(description="미국주식 스캐너")
    parser.add_argument("--threshold", type=int, default=ALERT_SCORE,
                        help="신호 임계값 (기본 65)")
    args = parser.parse_args()
    _, failures = scan(threshold=args.threshold)
    if failures:
        sys.exit(f"❌ {len(failures)}개 종목 처리 실패: {', '.join(failures)}")


if __name__ == "__main__":
    main()
