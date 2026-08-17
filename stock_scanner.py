"""미국주식 매수신호 스캐너.

GitHub Actions가 평일마다 실행해 watchlist 종목의 기술적 신호를 스코어링하고,
ALERT_SCORE 이상인 경우 signals 테이블에 저장한 뒤 Telegram으로 알림을 보낸다.
신호의 5/10/20일 수익률은 이후 배치로 갱신된다.
"""

import argparse
import os
import sys
import time
import threading
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

from asset_classification import classify_asset
from decision_engine import Decision, make_decision

WATCHLIST_FILE = "watchlist.csv"
ALERT_SCORE = 55  # V8: 55-59 밴드가 유일한 기회 구간 (52주 백테스트 검증)
# V7 스캐너(Legacy/Baseline) 전용 버전 라벨. daily_data/signals에 저장되며,
# V8 Opportunity Engine 평가는 opportunity_scores 테이블(score_version 컬럼 없음)을
# 사용하므로 V8 데이터에는 이 값이 기록되지 않는다.
SCORE_VERSION = 7
ALERT_COOLDOWN_DAYS = 5
STALE_DATA_DAYS = 7
PRUNE_RETENTION_DAYS = 365
SCAN_WORKERS = 8  # 종목별 병렬 분석 워커 수 (Yahoo 스로틀링을 겸함)

# V8: Signal 생성이 허용되는 Decision 등급 (evaluate_opportunities 게이트).
_SIGNAL_DECISIONS = (Decision.OPPORTUNITY, Decision.STRONG_OPPORTUNITY)

_db: Any = None
_db_lock = threading.Lock()

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
_session = requests.Session()
_session.headers["User-Agent"] = _UA


def get_db() -> Any:
    """지연 초기화되는 Supabase 클라이언트 (import 시 env 변수 불필요)."""
    global _db
    with _db_lock:
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
                             auto_adjust=True, progress=False, session=_session)
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
                 prev_price: float | None = None,
                 ma20_prev: float | None = None,
                 relative_strength_5d: float | None = None,
                 prev2_rsi: float | None = None) -> tuple[int, list[str], dict]:
    """V7 반등확인형 스코어 (0~100).

    V6 백테스트에서 변별력이 낮았던 '소폭 RSI/가격 상승'의 과대평가를 줄이고,
    적정 눌림·MA20 회복·상대강도·거래량을 더 중요하게 반영한다.

    배점:
      RSI 상태             20
      RSI 반등             15
      가격 반등            15
      적정 눌림폭          15
      MA20 회복/접근       15
      중기 추세              5
      QQQ 대비 상대강도     10
      반등+거래량            5
      총합                 100

    RSI 반등은 가능하면 직전 2개 RSI까지 사용해 'RSI가 실제로 방향을 돌리는지' 확인한다.
    prev2_rsi가 없으면 기존 호출과의 호환성을 위해 당일 RSI 변화만 사용한다.

    returns: (점수, 조건 목록, 영역별 세부 점수 dict)
    """
    score, cond = 0, []
    details = {
        "rsi_state": 0, "rsi_rebound": 0, "price_rebound": 0,
        "drawdown": 0, "ma20": 0, "trend": 0,
        "relative_strength": 0, "volume": 0,
    }

    # 1) RSI 상태: 30~40을 가장 선호하되 극단적 과매도는 보수적으로 평가.
    if 30 <= rv < 40:
        score += 20; details["rsi_state"] = 20; cond.append("RSI30~40")
    elif 25 <= rv < 30:
        score += 16; details["rsi_state"] = 16; cond.append("RSI25~30")
    elif 40 <= rv < 45:
        score += 10; details["rsi_state"] = 10; cond.append("RSI40~45")
    elif 20 <= rv < 25:
        score += 8; details["rsi_state"] = 8; cond.append("RSI20~25")
    elif rv < 20:
        score += 2; details["rsi_state"] = 2; cond.append("RSI<20")

    # 2) RSI 반등: 가능하면 2개 연속 상승까지 확인한다.
    delta_rsi = rv - prev
    rsi_turning_up = prev2_rsi is None or prev > prev2_rsi
    if rv < 45 and rsi_turning_up:
        if delta_rsi >= 3:
            score += 15; details["rsi_rebound"] = 15; cond.append("RSI강한반등")
        elif delta_rsi >= 2:
            score += 10; details["rsi_rebound"] = 10; cond.append("RSI반등")
        elif delta_rsi >= 1:
            score += 5; details["rsi_rebound"] = 5; cond.append("RSI소폭반등")

    # 3) 가격 반등: 아주 작은 상승은 점수에서 제외한다.
    day_return = ((price / prev_price) - 1) * 100 if prev_price else 0.0
    if day_return >= 2.0:
        score += 15; details["price_rebound"] = 15; cond.append("가격강한반등")
    elif day_return >= 1.0:
        score += 10; details["price_rebound"] = 10; cond.append("가격반등")
    elif day_return >= 0.5:
        score += 5; details["price_rebound"] = 5; cond.append("가격소폭반등")

    # 4) 60일 고점 대비 눌림: 적정 눌림을 가장 선호, -25% 이하 급락은 제외.
    if -15 <= dd <= -5:
        score += 15; details["drawdown"] = 15; cond.append("적정눌림-5~-15%")
    elif -25 <= dd < -15:
        score += 10; details["drawdown"] = 10; cond.append("눌림-15~-25%")
    elif -5 < dd <= -2:
        score += 5; details["drawdown"] = 5; cond.append("얕은눌림")
    elif dd < -25:
        cond.append("과도한급락")

    # 5) MA20: 단순 근접보다 하단에서 회복하는 경우를 우선.
    ma20_gap = (price / ma20 - 1) * 100
    crossed_ma20 = (
        ma20_prev is not None and prev_price is not None
        and prev_price <= ma20_prev and price > ma20
    )
    if crossed_ma20:
        score += 15; details["ma20"] = 15; cond.append("MA20회복")
    elif price >= ma20 and ma20_gap <= 3:
        score += 10; details["ma20"] = 10; cond.append("MA20위근접")
    elif -3 <= ma20_gap < 0:
        score += 5; details["ma20"] = 5; cond.append("MA20아래근접")

    # 6) 중기 추세는 보조조건으로만 사용.
    ma50_rising = ma50_prev is not None and ma50 > ma50_prev
    if price > ma50 and ma50_rising:
        score += 5; details["trend"] = 5; cond.append("상승추세")
    elif price > ma50:
        score += 3; details["trend"] = 3; cond.append("50일선위")

    # 7) QQQ 대비 5거래일 상대강도: 변별력을 높인다.
    if relative_strength_5d is not None:
        if relative_strength_5d >= 2:
            score += 10; details["relative_strength"] = 10; cond.append("QQQ대비강함")
        elif relative_strength_5d > 0:
            score += 5; details["relative_strength"] = 5; cond.append("QQQ대비우위")

    # 8) 반등과 거래량이 같이 나타날 때만 가점.
    if vr >= 1.5 and day_return > 0:
        score += 5; details["volume"] = 5; cond.append("반등+거래량1.5배")
    elif vr >= 1.2 and day_return > 0:
        score += 3; details["volume"] = 3; cond.append("반등+거래량증가")

    return min(score, 100), cond, details


_market_df_cache: pd.DataFrame | None = None
_market_df_cache_lock = threading.Lock()


def _market_history() -> pd.DataFrame:
    """V7 상대강도 계산용 QQQ 일봉. 프로세스 내 1회 캐시."""
    global _market_df_cache
    with _market_df_cache_lock:
        if _market_df_cache is None or _market_df_cache.empty:
            _market_df_cache = fetch_history("QQQ")
    return _market_df_cache.copy()


def _relative_strength_series(df: pd.DataFrame, market_df: pd.DataFrame | None) -> pd.Series:
    """날짜별 5일 상대강도(종목 5일 수익률 - QQQ 5일 수익률, %p)."""
    out = pd.Series(float("nan"), index=df.index)
    if market_df is None or market_df.empty:
        return out
    try:
        stock = df["Close"].astype(float)
        market = market_df["Close"].astype(float)
        market = market[~market.index.duplicated(keep="last")].sort_index()
        stock_ret = stock.pct_change(5) * 100
        market_ret = market.pct_change(5) * 100
        market_ret = market_ret.reindex(df.index, method="ffill")
        out = stock_ret - market_ret
        return out
    except Exception:
        return out


def _relative_strength_5d(df: pd.DataFrame, market_df: pd.DataFrame) -> float | None:
    """같은 날짜 기준 종목 5일 수익률 - QQQ 5일 수익률(%p)."""
    s = _relative_strength_series(df, market_df)
    if s.empty or pd.isna(s.iloc[-1]):
        return None
    return float(s.iloc[-1])


def compute_signal(ticker: str, df: pd.DataFrame,
                   market_df: pd.DataFrame | None = None) -> dict[str, Any] | None:
    """가격 데이터프레임에서 V7 신호를 계산한다 (순수 함수)."""
    if df.empty:
        return None

    df = df.copy()
    df["rsi"] = rsi(df["Close"])
    df["ma20"] = df["Close"].rolling(20).mean()
    df["ma50"] = df["Close"].rolling(50).mean()
    df["high60"] = df["High"].rolling(60).max()
    df["avgvol"] = df["Volume"].rolling(20).mean()
    df = df.dropna()
    if len(df) < 3:
        return None

    a, b, c = df.iloc[-1], df.iloc[-2], df.iloc[-3]
    price = float(a["Close"])
    rv = float(a["rsi"])
    prev = float(b["rsi"])
    ma20 = float(a["ma20"])
    ma50 = float(a["ma50"])
    ma50_prev = float(b["ma50"])
    ma20_prev = float(b["ma20"])
    dd = (price / float(a["high60"]) - 1) * 100
    vr = float(a["Volume"]) / float(a["avgvol"])
    prev_price = float(b["Close"])
    rs5 = _relative_strength_5d(df, market_df) if market_df is not None else None

    score, cond, _ = score_signal(
        price, rv, prev, ma20, ma50, dd, vr,
        ma50_prev=ma50_prev, prev_price=prev_price,
        ma20_prev=ma20_prev, relative_strength_5d=rs5,
        prev2_rsi=float(c["rsi"]),
    )
    return dict(ticker=ticker, price=price, rsi=rv, prev_rsi=prev, prev2_rsi=float(c["rsi"]),
                ma20=ma20, ma50=ma50, drawdown=dd,
                volume_ratio=vr, relative_strength_5d=rs5,
                score=score, conditions=cond,
                score_version=SCORE_VERSION,
                data_date=str(df.index[-1].date()) if hasattr(df.index[-1], "date") else str(df.index[-1])[:10])


def compute_signal_v8(ticker: str, df: pd.DataFrame,
                      market_df: pd.DataFrame | None = None,
                      strategy: str = "general",
                      info: dict | None = None,
                      classification_confidence: float = 0.5) -> dict[str, Any] | None:
    """가격 데이터프레임에서 V8 전략 인식 신호를 계산한다 (순수 함수).

    opportunity_engine의 전략별 가중치로 opportunity_score를 산출하고
    risk_level/신뢰도를 함께 반환한다. V7 compute_signal()은 그대로 유지된다.
    """
    if df.empty:
        return None

    df = df.copy()
    df["rsi"] = rsi(df["Close"])
    df["ma20"] = df["Close"].rolling(20).mean()
    df["ma50"] = df["Close"].rolling(50).mean()
    df["high60"] = df["High"].rolling(60).max()
    df["avgvol"] = df["Volume"].rolling(20).mean()
    df = df.dropna()
    if len(df) < 3:
        return None

    a, b, c = df.iloc[-1], df.iloc[-2], df.iloc[-3]
    price = float(a["Close"])
    rv = float(a["rsi"])
    prev = float(b["rsi"])
    ma20 = float(a["ma20"])
    ma50 = float(a["ma50"])
    dd = (price / float(a["high60"]) - 1) * 100
    vr = float(a["Volume"]) / float(a["avgvol"])
    rs5 = _relative_strength_5d(df, market_df) if market_df is not None else None

    # opportunity_engine은 stock_scanner를 모듈 레벨에서 임포트하지 않으므로
    # 여기서는 lazy import로 순환 임포트를 피한다.
    from opportunity_engine import (component_sub_scores,
                                    compute_fundamental_components,
                                    compute_technical_components,
                                    opportunity_score, risk_score,
                                    signal_confidence)

    i = len(df) - 1
    comps = compute_technical_components(df, i, market_df)
    comps.update(compute_fundamental_components(info))
    oscore = opportunity_score(comps, strategy)
    rscore, rlevel = risk_score(df, i, info)
    conf = signal_confidence(oscore)
    subs = component_sub_scores(comps)
    decision = make_decision(oscore, rlevel, conf, strategy, classification_confidence)

    # 알림 메시지의 기술적 조건 텍스트는 V7 조건문을 그대로 사용한다
    # (V8 전략별 스코어링과 무관하게 동작하는 표시용 정보).
    _, cond, _ = score_signal(
        price, rv, prev, ma20, ma50, dd, vr,
        ma50_prev=float(b["ma50"]), prev_price=float(b["Close"]),
        ma20_prev=float(b["ma20"]), relative_strength_5d=rs5,
        prev2_rsi=float(c["rsi"]),
    )
    return dict(ticker=ticker, price=price, rsi=rv, prev_rsi=prev, prev2_rsi=float(c["rsi"]),
                ma20=ma20, ma50=ma50, drawdown=dd,
                volume_ratio=vr, relative_strength_5d=rs5,
                score=oscore, conditions=cond,
                strategy_type=strategy,
                classification_confidence=classification_confidence,
                opportunity_score=oscore,
                risk_level=rlevel, risk_score=rscore,
                signal_confidence=conf,
                decision=decision,
                technical_score=subs["technical_score"],
                momentum_score=subs["momentum_score"],
                fundamental_score=subs["fundamental_score"],
                valuation_score=subs["valuation_score"],
                components=comps,
                data_date=str(df.index[-1].date()) if hasattr(df.index[-1], "date") else str(df.index[-1])[:10])


def fetch_info(ticker: str) -> dict | None:
    """Yahoo Finance 메타데이터를 best-effort로 조회한다 (실패 시 None)."""
    try:
        info = yf.Ticker(ticker, session=_session).info
        return info if info else None
    except Exception:  # noqa: BLE001 - 메타데이터 부재는 스코어링에 치명적이지 않음
        return None


def resolve_strategy(ticker: str, db: Any | None = None,
                     info: dict | None = None) -> tuple[str, float]:
    """ticker의 strategy_type과 분류 confidence를 결정한다.

    우선순위:
      1. DB asset_classification (사용자 manual override 포함)
      2. 즉석 분류 (info가 있으면 classify_asset)
      3. general 폴백
    """
    if db is not None:
        try:
            rows = (db.table("asset_classification")
                    .select("strategy_type", "confidence")
                    .eq("ticker", ticker)
                    .execute().data or [])
            if rows and rows[0].get("strategy_type"):
                return rows[0]["strategy_type"], float(rows[0].get("confidence") or 0.5)
        except Exception:  # noqa: BLE001 - 테이블 미존재 등, 즉석 분류로 폴백
            pass
    if info:
        try:
            c = classify_asset(ticker, info)
            return c.strategy_type, c.confidence
        except Exception:  # noqa: BLE001
            pass
    return "general", 0.5


def analyze(ticker: str, date: str | None = None,
            db: Any | None = None) -> dict[str, Any] | None:
    """ticker의 데이터를 받아 V8 신호를 계산한다.

    Yahoo 가격 데이터를 받아 V8 전략별 Opportunity Score를 산출한다.
    V7 compute_signal()은 backtest 비교용으로만 유지된다.
    """
    df = fetch_history(ticker)
    if df.empty:
        if db is not None:
            log_data_quality(ticker, "missing_price", {"reason": "no_data"})
        return None
    as_of = (datetime.strptime(date, "%Y-%m-%d").date()
             if date else datetime.now(timezone.utc).date())
    last = df.index.max()
    if hasattr(last, "date"):
        age = (as_of - last.date()).days
        if age > STALE_DATA_DAYS:
            print(f"{ticker}: 최근 데이터가 {age}일 전 ({last.date()}) - 스킵")
            if db is not None:
                log_data_quality(ticker, "stale_data",
                                 {"age_days": age, "last_date": str(last.date())})
            return None
    info = fetch_info(ticker)
    strategy, cconf = resolve_strategy(ticker, db, info)
    return compute_signal_v8(ticker, df, _market_history(), strategy, info, cconf)


def evaluate_opportunities(date: str | None = None,
                           persist: bool = True) -> list[dict[str, Any]]:
    """21개 watchlist 전부를 V8 Opportunity Engine으로 평가한다.

    스캐너(V7 후보 선별)와 분리된 경로로, 모든 종목의
    Opportunity/Risk 점수를 opportunity_scores 테이블에 저장한다.
    Phase 3 알림은 이 결과를 사용한다.
    """
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db = get_db() if persist else None
    tickers = load_watchlist(db)
    results: list[dict[str, Any]] = []
    for ticker in tickers:
        try:
            df = fetch_history(ticker)
            if df.empty:
                continue
            info = fetch_info(ticker)
            strategy, cconf = resolve_strategy(ticker, db, info)
            x = compute_signal_v8(ticker, df, _market_history(), strategy, info, cconf)
            if x is None:
                continue
            if persist:
                market_date = x.get("data_date", date)
                save_opportunity_score(x, market_date)
                # V8 spec §8: Decision이 알림 조건일 때만 Signal 생성 (threshold=0으로
                # V7 ALERT_SCORE 우회 — general OPPORTUNITY는 40점부터 가능).
                if x.get("decision") in _SIGNAL_DECISIONS:
                    save_signal(x, market_date, threshold=0)
            results.append(x)
        except Exception as e:
            print(f"{ticker} Opportunity 평가 실패: {e}", file=sys.stderr)
    return results




# ---------------------------------------------------------------------------
# 저장
# ---------------------------------------------------------------------------


def save_daily(x: dict[str, Any], date: str) -> None:
    get_db().table("daily_data").upsert({
        "date": date, "ticker": x["ticker"], "price": x["price"],
        "rsi": x["rsi"], "prev_rsi": x["prev_rsi"],
        "ma20": x["ma20"], "ma50": x["ma50"],
        "drawdown": x["drawdown"], "volume_ratio": x["volume_ratio"],
        "relative_strength_5d": x.get("relative_strength_5d"),
        "score": x["score"], "score_version": SCORE_VERSION,
        "strategy_type": x.get("strategy_type"),
        "opportunity_score": x.get("opportunity_score"),
        "risk_level": x.get("risk_level"),
        "technical_score": x.get("technical_score"),
        "momentum_score": x.get("momentum_score"),
        "fundamental_score": x.get("fundamental_score"),
        "valuation_score": x.get("valuation_score"),
        "components": x.get("components"),
    }).execute()


def save_signal(x: dict[str, Any], date: str, threshold: int = ALERT_SCORE) -> None:
    if x["score"] < threshold:
        return
    get_db().table("signals").upsert({
        "signal_date": date, "ticker": x["ticker"],
        "signal_price": x["price"], "score": x["score"], "score_version": SCORE_VERSION,
        "rsi": x["rsi"], "drawdown": x["drawdown"],
        "strategy_type": x.get("strategy_type"),
        "opportunity_score": x.get("opportunity_score"),
        "risk_level": x.get("risk_level"),
        "risk_score": x.get("risk_score"),
        "signal_confidence": x.get("signal_confidence"),
        "classification_confidence": x.get("classification_confidence"),
        "decision": x.get("decision"),
        "technical_score": x.get("technical_score"),
        "momentum_score": x.get("momentum_score"),
        "fundamental_score": x.get("fundamental_score"),
        "valuation_score": x.get("valuation_score"),
        "components": x.get("components"),
    }, on_conflict="signal_date,ticker").execute()


def save_opportunity_score(x: dict[str, Any], date: str) -> None:
    """V8 Opportunity Engine 결과를 opportunity_scores 테이블에 저장한다."""
    get_db().table("opportunity_scores").upsert({
        "date": date, "ticker": x["ticker"],
        "strategy_type": x.get("strategy_type"),
        "opportunity_score": x.get("opportunity_score"),
        "risk_level": x.get("risk_level"),
        "risk_score": x.get("risk_score"),
        "signal_confidence": x.get("signal_confidence"),
        "classification_confidence": x.get("classification_confidence"),
        "decision": x.get("decision"),
        "technical_score": x.get("technical_score"),
        "momentum_score": x.get("momentum_score"),
        "fundamental_score": x.get("fundamental_score"),
        "valuation_score": x.get("valuation_score"),
        "components": x.get("components"),
    }, on_conflict="date,ticker").execute()


def start_run() -> int:
    """scan_runs에 실행 시작을 기록하고 run_id를 반환한다.

    started_at은 Supabase 서버 타임스탬프(now())를 사용하고,
    version은 테이블 기본값(v10)으로 남긴다.
    """
    res = (get_db().table("scan_runs")
           .insert({"started_at": "now()", "status": "running"})
           .execute())
    return res.data[0]["id"]


def finish_run(run_id: int, stats: dict[str, Any]) -> None:
    """scan_runs 실행 결과를 갱신한다.

    stats 키: total, evaluated, signals, failed, status, error_summary
    """
    get_db().table("scan_runs").update({
        "finished_at": "now()",
        "watchlist_count": stats["total"],
        "evaluated_count": stats["evaluated"],
        "signal_count": stats["signals"],
        "failure_count": stats["failed"],
        "status": stats["status"],
        "error_summary": stats.get("error_summary"),
    }).eq("id", run_id).execute()


def log_data_quality(ticker: str, issue_type: str, details: dict | None = None) -> None:
    """데이터 품질 이슈를 data_quality_log 테이블에 기록한다 (best-effort).

    issue_type: "api_failure" | "stale_data" | "missing_price" | "calculation_error"
    logged_at은 Supabase 서버 타임스탬프(now())를 사용한다.
    로깅 실패는 스캔 파이프라인을 중단시키지 않는다.
    """
    try:
        get_db().table("data_quality_log").insert({
            "ticker": ticker,
            "issue_type": issue_type,
            "details": details,
            "logged_at": "now()",
        }).execute()
    except Exception:  # noqa: BLE001 - 로깅은 best-effort
        pass


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
        for update in to_update:
            db.table("signals").update(
                {
                    key: value
                    for key, value in update.items()
                    if key != "id"
                }
            ).eq("id", update["id"]).execute()

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
    """후보 목록을 V8 기준으로 포맷해 텔레그램 메시지로 반환한다."""
    ordered = sorted(candidates, key=lambda x: x.get("opportunity_score", x.get("score", 0)), reverse=True)
    msg = f"📊 미국주식 매수 후보 (V8)\n📅 {date}\n\n"
    for x in ordered:
        decision = x.get("decision", "WATCH")
        emoji = _DECISION_EMOJI.get(decision, "❔")
        strategy = _STRATEGY_LABELS.get(x.get("strategy_type", "general"), "일반")
        score = x.get("opportunity_score", x.get("score", 0))
        risk = x.get("risk_level", "-")
        conf = x.get("signal_confidence", 0)
        msg += (f"{emoji} {x['ticker']} [{decision}]\n"
                f"전략: {strategy} | 리스크: {risk}\n"
                f"기회점수: {score} | 신뢰도: {conf:.2f}\n"
                f"가격 ${x['price']:.2f} | RSI {x['rsi']:.1f}\n"
                f"고점대비 {x['drawdown']:.1f}%\n\n")
    return msg


_STRATEGY_LABELS = {
    "general": "일반",
    "quality": "우량주",
    "established_growth": "성장주",
    "speculative": "고변동",
    "broad_market_etf": "시장ETF",
    "growth_etf": "성장ETF",
    "sector_etf": "섹터ETF",
    "dividend_etf": "배당ETF",
    "income_etf": "소득ETF",
    "other_etf": "기타ETF",
}

_DECISION_EMOJI = {
    Decision.STRONG_OPPORTUNITY: "🔥",
    Decision.OPPORTUNITY: "🟢",
    Decision.WATCH: "👀",
    Decision.NEUTRAL: "⚪",
    Decision.AVOID: "🚫",
}


def format_signal_message(x: dict[str, Any]) -> str:
    """V8 신호를 텔레그램 메시지로 포맷한다 (V8 spec §12)."""
    decision = x.get("decision", Decision.WATCH)
    strategy = x.get("strategy_type", "general")
    label = _STRATEGY_LABELS.get(strategy, strategy)
    emoji = _DECISION_EMOJI.get(decision, "❔")

    comps = x.get("components") or {}
    reason = ", ".join(
        f"{k}({v})" for k, v in sorted(
            ((k, v) for k, v in comps.items() if v),
            key=lambda kv: (-kv[1], kv[0]),
        )
    ) or "-"

    def _axis(label: str, key: str) -> str:
        value = x.get(key)
        return f"{label}: {value if value is not None else '-'}"

    return "\n".join([
        x["ticker"],
        f"Strategy: {label}",
        f"Decision: {emoji} {decision}",
        "",
        f"Opportunity: {x.get('opportunity_score') or x.get('score') or 0}",
        f"Risk: {x.get('risk_level', '-')}",
        f"Confidence: {(x.get('signal_confidence') or 0):.2f}",
        "",
        _axis("Technical", "technical_score"),
        _axis("Momentum", "momentum_score"),
        _axis("Fundamental", "fundamental_score"),
        _axis("Valuation", "valuation_score"),
        "",
        f"Reason: {reason}",
    ])


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
            x = analyze(ticker, date, db)
            if x and persist:
                market_date = x.get("data_date", date)
                save_daily(x, market_date)
                save_signal(x, market_date, threshold)
            return ticker, x, None
        except Exception as e:
            if db is not None:
                log_data_quality(ticker, "api_failure", {"error": str(e)})
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
                        help="신호 임계값 (기본 55)")
    args = parser.parse_args()
    run_id = start_run()
    try:
        candidates, failures = scan(threshold=args.threshold)
        evaluate_opportunities()
        total = len(load_watchlist(get_db()))
        stats = {
            "total": total,
            "evaluated": total - len(failures),
            "signals": len(candidates),
            "failed": len(failures),
            "status": "failed" if failures else "completed",
            "error_summary": ", ".join(failures) if failures else None,
        }
    except Exception as e:
        stats = {
            "total": 0, "evaluated": 0, "signals": 0, "failed": 0,
            "status": "failed", "error_summary": str(e),
        }
        finish_run(run_id, stats)
        raise
    finish_run(run_id, stats)
    if failures:
        sys.exit(f"❌ {len(failures)}개 종목 처리 실패: {', '.join(failures)}")


if __name__ == "__main__":
    main()
