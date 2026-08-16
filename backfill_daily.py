"""과거 daily_data 백필 도구.

watchlist 종목의 1년 히스토리를 1회 fetch하고 지표를 미리 계산한 뒤,
각 과거 거래일을 행 단위로 스코어링해 daily_data 테이블에 채운다.
score >= threshold인 행은 signals 테이블로 승격하며, daily_data가 신호일
이후까지 이미 있으므로 5/10/20거래일 수익률도 즉시 계산해 함께 저장한다.
모든 스코어링은 해당 행까지의 데이터만 사용하므로 lookahead bias가 없다.
stdout에는 결과 요약만, 진행 로그/경고는 stderr로 출력한다.
"""

import argparse
import sys
from datetime import timedelta
from typing import Any

import pandas as pd

from backtest import _compute_indicators, _get_db_if_available, _load_tickers
from stock_scanner import (ALERT_SCORE, ALERT_COOLDOWN_DAYS, SCORE_VERSION,
                           fetch_history, score_signal, _relative_strength_series)

UPSERT_BATCH = 500
# (신호일 이후 거래일 수, signals 컬럼명)
SIGNAL_RETURN_KEYS = ((5, "return_5d"), (10, "return_10d"), (20, "return_20d"))


def _backfill_ticker(ticker: str, df: pd.DataFrame, start: str, end: str,
                     market_df: pd.DataFrame | None = None) -> list[dict]:
    """한 ticker의 각 과거 거래일을 V6 방식으로 스코어링한다."""
    df = _compute_indicators(df)
    if df.empty:
        return []

    idx = df.index
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    mask = (idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))
    rs_series = _relative_strength_series(df, market_df)

    records: list[dict] = []
    close = df["Close"].astype(float)
    for i in range(len(df)):
        if not mask[i] or i == 0:
            continue
        if (pd.isna(df["rsi"].iloc[i]) or pd.isna(df["ma20"].iloc[i])
                or pd.isna(df["ma50"].iloc[i]) or pd.isna(df["high60"].iloc[i])
                or pd.isna(df["avgvol"].iloc[i]) or pd.isna(df["rsi"].iloc[i - 1])):
            continue
        price = float(close.iloc[i])
        rv = float(df["rsi"].iloc[i])
        prev = float(df["rsi"].iloc[i - 1])
        ma20 = float(df["ma20"].iloc[i])
        ma50 = float(df["ma50"].iloc[i])
        ma50_prev = float(df["ma50"].iloc[i - 1])
        ma20_prev = float(df["ma20"].iloc[i - 1])
        dd = (price / float(df["high60"].iloc[i]) - 1) * 100
        vr = float(df["Volume"].iloc[i]) / float(df["avgvol"].iloc[i])
        prev_price = float(close.iloc[i - 1])
        rs5 = None if pd.isna(rs_series.iloc[i]) else float(rs_series.iloc[i])

        score, _ = score_signal(
            price, rv, prev, ma20, ma50, dd, vr,
            ma50_prev=ma50_prev, prev_price=prev_price,
            ma20_prev=ma20_prev, relative_strength_5d=rs5,
        )
        records.append({
            "date": str(df.index[i])[:10], "ticker": ticker, "price": price,
            "rsi": rv, "prev_rsi": prev, "ma20": ma20, "ma50": ma50,
            "drawdown": dd, "volume_ratio": vr,
            "relative_strength_5d": rs5,
            "score": score, "score_version": SCORE_VERSION,
        })
    return records


def _promote_signals(records: list[dict], threshold: int) -> list[dict]:
    """V6 신호 승격. 동일 종목 5일 cooldown 내에서는 최고점 1건만 남긴다."""
    by_ticker: dict[str, list[dict]] = {}
    for r in records:
        if r["score"] >= threshold:
            by_ticker.setdefault(r["ticker"], []).append(r)

    signals: list[dict] = []
    for rows in by_ticker.values():
        rows = sorted(rows, key=lambda r: r["date"])
        selected: list[dict] = []
        i = 0
        while i < len(rows):
            anchor = rows[i]
            anchor_date = pd.Timestamp(anchor["date"])
            group = [anchor]
            j = i + 1
            while j < len(rows) and (pd.Timestamp(rows[j]["date"]) - anchor_date).days <= ALERT_COOLDOWN_DAYS:
                group.append(rows[j])
                j += 1
            selected.append(max(group, key=lambda r: (r["score"], r["date"])))
            i = j

        for r in selected:
            after = [x for x in rows if x["date"] > r["date"]]
            rets: dict[str, float | None] = {}
            for n, key in SIGNAL_RETURN_KEYS:
                rets[key] = (
                    (after[n - 1]["price"] / r["price"] - 1) * 100
                    if len(after) >= n and r["price"] else None
                )
            signals.append({
                "signal_date": r["date"], "ticker": r["ticker"],
                "signal_price": r["price"], "score": r["score"],
                "rsi": r["rsi"], "drawdown": r["drawdown"],
                "score_version": SCORE_VERSION, **rets,
            })
    return sorted(signals, key=lambda x: (x["signal_date"], x["ticker"]), reverse=True)


def _run_backfill(weeks: int, tickers: list[str]) -> dict:
    """fetch 루프 + 행 단위 백필 실행.

    fetch 실패 종목은 경고 후 스킵하고, 데이터가 하나도 없으면
    records/tickers가 빈 dict를 반환한다.
    """
    dfs: dict[str, pd.DataFrame] = {}
    try:
        market_df = fetch_history("QQQ")
    except Exception as e:
        market_df = pd.DataFrame()
        print(f"경고: QQQ 상대강도 데이터 조회 실패 - {e}", file=sys.stderr)
    for ticker in tickers:
        try:
            df = fetch_history(ticker)
        except Exception as e:
            print(f"경고: {ticker} fetch 실패 - {e}", file=sys.stderr)
            continue
        if df is None or df.empty:
            print(f"경고: {ticker} 데이터 없음", file=sys.stderr)
            continue
        dfs[ticker] = df
        print(f"{ticker} fetch 완료", file=sys.stderr)

    if not dfs:
        return {"records": [], "tickers": [], "start": "", "end": "", "weeks": weeks}

    end = min(df.index.max() for df in dfs.values())
    start = end - timedelta(weeks=weeks)
    start_str, end_str = str(start.date()), str(end.date())

    records: list[dict] = []
    for ticker, df in dfs.items():
        records.extend(_backfill_ticker(ticker, df, start_str, end_str, market_df))

    return {"records": records, "tickers": list(dfs),
            "start": start_str, "end": end_str, "weeks": weeks}


def _upsert_batches(db: Any, rows: list[dict], batch_size: int = UPSERT_BATCH) -> None:
    """daily_data PK(date,ticker) 기준 upsert를 배치 단위로 실행한다."""
    for i in range(0, len(rows), batch_size):
        db.table("daily_data").upsert(
            rows[i:i + batch_size], on_conflict="date,ticker").execute()


def _upsert_signals(db: Any, rows: list[dict], batch_size: int = UPSERT_BATCH) -> None:
    """signals unique(signal_date,ticker) 기준 upsert를 배치 단위로 실행한다.

    id는 identity PK이므로 본문에 넣지 않는다.
    """
    for i in range(0, len(rows), batch_size):
        db.table("signals").upsert(
            rows[i:i + batch_size], on_conflict="signal_date,ticker").execute()


def main() -> None:
    parser = argparse.ArgumentParser(description="과거 daily_data 백필")
    parser.add_argument("--weeks", type=int, default=52,
                        help="백필 기간 (df 마지막 날짜 기준 최근 N주, 기본 52주)")
    parser.add_argument("--tickers", help="콤마 구분 ticker (지정 시 watchlist 대체)")
    parser.add_argument("--with-signals", dest="with_signals", action="store_true",
                        default=True, help="신호 승격 (기본 True)")
    parser.add_argument("--no-signals", dest="with_signals", action="store_false",
                        help="신호 승격 끄기")
    parser.add_argument("--threshold", type=int, default=ALERT_SCORE,
                        help="신호 임계값 (기본 65)")
    args = parser.parse_args()

    db = _get_db_if_available()
    tickers = _load_tickers(args.tickers, db)
    print(f"백필 대상 {len(tickers)}개: {', '.join(tickers)}", file=sys.stderr)
    result = _run_backfill(args.weeks, tickers)

    if not result["tickers"]:
        print("백필할 데이터 없음", file=sys.stderr)
        sys.exit(1)
    if db is None:
        print("경고: DB 연결 없음 - 저장 생략", file=sys.stderr)
        sys.exit(1)

    rows = result["records"]
    _upsert_batches(db, rows)
    signal_count = 0
    if args.with_signals:
        signals = _promote_signals(rows, args.threshold)
        if signals:
            _upsert_signals(db, signals)
        signal_count = len(signals)
    print(f"백필 완료: {len(rows)}행 daily + {signal_count}개 신호 "
          f"({result['start']} ~ {result['end']}, ticker {len(result['tickers'])}개)")


if __name__ == "__main__":
    main()