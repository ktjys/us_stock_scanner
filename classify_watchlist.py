"""
V8 Phase 2 - Watchlist 자동 분류 Runner

기존 V7 Scanner의 scoring / signal 로직에는 손대지 않는다.

역할:
1. Supabase에서 active watchlist 조회
2. Yahoo Finance metadata 조회
3. asset_classification.py의 분류 엔진으로 자동 분류
4. Supabase asset_classification에 저장
5. 사용자가 수동 지정한 분류는 덮어쓰지 않음
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import yfinance as yf
from supabase import create_client

from asset_classification import classify_asset


def get_supabase_client():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_PUBLISHABLE_KEY")

    if not url or not key:
        raise RuntimeError(
           "SUPABASE_URL 또는 SUPABASE_PUBLISHABLE_KEY 환경변수가 없습니다." 
        )

    return create_client(url, key)


def get_active_watchlist(supabase):
    response = (
        supabase
        .table("watchlist")
        .select("ticker")
        .eq("active", True)
        .order("ticker")
        .execute()
    )

    return [row["ticker"].upper() for row in response.data]


def get_existing_classification(supabase, ticker: str):
    response = (
        supabase
        .table("asset_classification")
        .select("*")
        .eq("ticker", ticker)
        .limit(1)
        .execute()
    )

    if response.data:
        return response.data[0]

    return None


def fetch_yahoo_info(ticker: str) -> dict:
    print(f"[YAHOO] {ticker}")

    stock = yf.Ticker(ticker)

    try:
        info = stock.info
    except Exception as exc:
        raise RuntimeError(
            f"{ticker}: Yahoo Finance metadata 조회 실패: {exc}"
        ) from exc

    if not info:
        raise RuntimeError(f"{ticker}: Yahoo Finance metadata가 비어 있습니다.")

    return info


def save_classification(supabase, classification):
    data = classification.to_dict()

    # timezone-aware UTC timestamp
    data["classified_at"] = datetime.now(timezone.utc).isoformat()
    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    supabase.table("asset_classification").upsert(
        data,
        on_conflict="ticker",
    ).execute()


def classify_one(supabase, ticker: str):
    existing = get_existing_classification(supabase, ticker)

    # 사용자가 직접 지정한 분류는 자동 분류가 절대 덮어쓰지 않는다.
    if existing and existing.get("classification_source") == "manual":
        print(
            f"[SKIP] {ticker}: manual classification "
            f"({existing.get('strategy_type')})"
        )
        return "skipped_manual"

    info = fetch_yahoo_info(ticker)
    if ticker == "AAPL":
        print("[DEBUG] AAPL Yahoo metadata")
        for key in (
	    "quoteType",
	    "marketCap",
	    "sector",
	    "industry",
	    "revenueGrowth",
	    "earningsGrowth",
	    "beta",
        ):
            print(f"  {key}: {info.get(key)}")

    result = classify_asset(ticker, info)

    save_classification(supabase, result)

    print(
        f"[CLASSIFIED] {ticker}: "
        f"{result.asset_type} / "
        f"{result.strategy_type} / "
        f"confidence={result.confidence:.2f}"
    )
    print(f"             reason: {result.reason}")

    return "classified"


def main():
    print("=" * 60)
    print("V8 Phase 2 - Watchlist Classification")
    print("=" * 60)

    supabase = get_supabase_client()

    tickers = get_active_watchlist(supabase)

    if not tickers:
        print("[INFO] active watchlist 종목이 없습니다.")
        return 0

    print(f"[INFO] active watchlist: {len(tickers)} symbols")
    print()

    classified = 0
    skipped_manual = 0
    failed = 0

    for ticker in tickers:
        try:
            result = classify_one(supabase, ticker)

            if result == "classified":
                classified += 1
            elif result == "skipped_manual":
                skipped_manual += 1

        except Exception as exc:
            failed += 1
            print(f"[ERROR] {ticker}: {exc}", file=sys.stderr)

        print()

    print("=" * 60)
    print("Classification Summary")
    print("=" * 60)
    print(f"Total          : {len(tickers)}")
    print(f"Classified     : {classified}")
    print(f"Manual skipped : {skipped_manual}")
    print(f"Failed         : {failed}")
    print("=" * 60)

    # 일부 종목이 실패해도 전체 작업은 완료할 수 있도록 한다.
    # 다만 CI/CD에서는 실패를 명확히 알 수 있도록 exit code 1을 반환한다.
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
