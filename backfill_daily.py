"""과거 daily_data 백필 도구.

watchlist 종목의 1년 히스토리를 1회 fetch하고 지표를 미리 계산한 뒤,
각 과거 거래일을 행 단위로 스코어링해 daily_data 테이블에 채운다.
모든 스코어링은 해당 행까지의 데이터만 사용하므로 lookahead bias가 없다.
stdout에는 결과 요약만, 진행 로그/경고는 stderr로 출력한다.
"""

import argparse
import sys
from datetime import timedelta
from typing import Any

import pandas as pd

from backtest import _compute_indicators, _get_db_if_available, _load_tickers
from stock_scanner import fetch_history, score_signal

UPSERT_BATCH = 500


def _backfill_ticker(ticker: str, df: pd.DataFrame, start: str, end: str) -> list[dict]:
    """한 ticker를 행 단위로 순회해 daily_data 스키마 dict 목록을 만든다."""
    df = _compute_indicators(df)
    if df.empty:
        return []

    idx = df.index
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    mask = (idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))

    records: list[dict] = []
    close = df["Close"].astype(float)
    for i in range(len(df)):
        if not mask[i]:
            continue
        if i == 0:
            continue
        # 실전 compute_signal의 dropna()와 동일 조건: 5개 지표 + 직전 RSI가
        # 모두 유효해야 스코어링한다 (어느 하나라도 NaN이면 스킵).
        if (pd.isna(df["rsi"].iloc[i]) or pd.isna(df["ma20"].iloc[i])
                or pd.isna(df["ma50"].iloc[i]) or pd.isna(df["high60"].iloc[i])
                or pd.isna(df["avgvol"].iloc[i]) or pd.isna(df["rsi"].iloc[i - 1])):
            continue
        price = float(close.iloc[i])
        rv = float(df["rsi"].iloc[i])
        prev = float(df["rsi"].iloc[i - 1])
        ma20 = float(df["ma20"].iloc[i])
        ma50 = float(df["ma50"].iloc[i])
        dd = (price / float(df["high60"].iloc[i]) - 1) * 100
        vr = float(df["Volume"].iloc[i]) / float(df["avgvol"].iloc[i])

        score, _ = score_signal(price, rv, prev, ma20, ma50, dd, vr)
        records.append({
            "date": str(df.index[i])[:10], "ticker": ticker, "price": price,
            "rsi": rv, "prev_rsi": prev, "ma20": ma20, "ma50": ma50,
            "drawdown": dd, "volume_ratio": vr, "score": score,
        })
    return records


def _run_backfill(weeks: int, tickers: list[str]) -> dict:
    """fetch 루프 + 행 단위 백필 실행.

    fetch 실패 종목은 경고 후 스킵하고, 데이터가 하나도 없으면
    records/tickers가 빈 dict를 반환한다.
    """
    dfs: dict[str, pd.DataFrame] = {}
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
        records.extend(_backfill_ticker(ticker, df, start_str, end_str))

    return {"records": records, "tickers": list(dfs),
            "start": start_str, "end": end_str, "weeks": weeks}


def _upsert_batches(db: Any, rows: list[dict], batch_size: int = UPSERT_BATCH) -> None:
    """daily_data PK(date,ticker) 기준 upsert를 배치 단위로 실행한다."""
    for i in range(0, len(rows), batch_size):
        db.table("daily_data").upsert(
            rows[i:i + batch_size], on_conflict="date,ticker").execute()


def main() -> None:
    parser = argparse.ArgumentParser(description="과거 daily_data 백필")
    parser.add_argument("--weeks", type=int, default=26,
                        help="백필 기간 (df 마지막 날짜 기준 최근 N주)")
    parser.add_argument("--tickers", help="콤마 구분 ticker (지정 시 watchlist 대체)")
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
    print(f"백필 완료: {len(rows)}행 "
          f"({result['start']} ~ {result['end']}, ticker {len(result['tickers'])}개)")


if __name__ == "__main__":
    main()