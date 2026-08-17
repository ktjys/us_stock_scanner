"""과거 opportunity_scores 백필 도구.

watchlist 종목의 1년 히스토리를 1회 fetch하고 지표를 미리 계산한 뒤,
각 과거 거래일을 행 단위로 V8 스코어링해 opportunity_scores 테이블에 채운다.
V8 백필은 signals 테이블에 저장하지 않는다 (신호는 V7 스캐너가 선별).
모든 스코어링은 해당 행까지의 데이터만 사용하므로 lookahead bias가 없다.
stdout에는 결과 요약만, 진행 로그/경고는 stderr로 출력한다.
"""

import argparse
import sys
from datetime import timedelta
from typing import Any

import pandas as pd

from backtest import _compute_indicators, _get_db_if_available, _load_tickers
from opportunity_engine import (component_sub_scores, compute_technical_components,
                                opportunity_score, risk_score, signal_confidence)
from stock_scanner import (ALERT_SCORE, ALERT_COOLDOWN_DAYS, SCORE_VERSION,
                           fetch_history, fetch_info, resolve_strategy, _relative_strength_series)

UPSERT_BATCH = 500
# (신호일 이후 거래일 수, signals 컬럼명) - _promote_signals(테스트 전용)에서 사용
SIGNAL_RETURN_KEYS = ((5, "return_5d"), (10, "return_10d"), (20, "return_20d"))
# opportunity_scores 테이블 컬럼 (supabase_v9_opportunity_scores.sql 기준, PK date,ticker)
OPPORTUNITY_SCORE_COLUMNS = ("date", "ticker", "strategy_type", "opportunity_score",
                             "risk_level", "risk_score", "signal_confidence",
                             "classification_confidence", "technical_score",
                             "momentum_score", "fundamental_score", "valuation_score",
                             "components")


def _backfill_ticker(ticker: str, df: pd.DataFrame, start: str, end: str,
                     market_df: pd.DataFrame | None = None,
                     strategy: str = "general",
                     classification_confidence: float = 0.5) -> list[dict]:
    """한 ticker의 각 과거 거래일을 V8 전략 인식 방식으로 스코어링한다.

    펀더멘털 info가 없으므로 기술 컴포넌트만으로 기회 점수를 내고 (전략
    가중치 자동 재정규화), 리스크는 beta 중간값·수익성 0점 기본값으로 계산한다.
    행에 필요한 지표가 하나라도 NaN이면 건너뛴다.
    """
    info = fetch_info(ticker)
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
        if not mask[i] or i < 2:
            continue
        comps = compute_technical_components(df, i, market_df, rs_series=rs_series)
        if comps is None:
            continue
        price = float(close.iloc[i])
        rv = float(df["rsi"].iloc[i])
        prev = float(df["rsi"].iloc[i - 1])
        ma20 = float(df["ma20"].iloc[i])
        ma50 = float(df["ma50"].iloc[i])
        dd = (price / float(df["high60"].iloc[i]) - 1) * 100
        vr = float(df["Volume"].iloc[i]) / float(df["avgvol"].iloc[i])
        rs5 = None if pd.isna(rs_series.iloc[i]) else float(rs_series.iloc[i])

        score = opportunity_score(comps, strategy)
        subs = component_sub_scores(comps)
        risk, level = risk_score(df, i, info)
        records.append({
            "date": str(df.index[i])[:10], "ticker": ticker, "price": price,
            "rsi": rv, "prev_rsi": prev, "ma20": ma20, "ma50": ma50,
            "drawdown": dd, "volume_ratio": vr,
            "relative_strength_5d": rs5,
            "score": score, "score_version": SCORE_VERSION,
            "strategy_type": strategy,
            "classification_confidence": classification_confidence,
            "opportunity_score": score,
            "risk_level": level,
            "risk_score": risk,
            "signal_confidence": signal_confidence(score),
            "technical_score": subs["technical_score"],
            "momentum_score": subs["momentum_score"],
            "fundamental_score": subs["fundamental_score"],
            "valuation_score": subs["valuation_score"],
            "components": comps,
        })
    return records


def _promote_signals(records: list[dict], threshold: int) -> list[dict]:
    """V8 신호 승격. 동일 종목 5일 cooldown 내에서는 최고점 1건만 남긴다.

    수익률은 모든 daily_data 행을 기준으로 계산한다 (고득점 신호만 비교하면
    실제 거래일 수익률과 달라지므로).
    """
    # 모든 행을 종목별로 인덱싱 (수익률 계산용)
    all_by_ticker: dict[str, list[dict]] = {}
    for r in records:
        all_by_ticker.setdefault(r["ticker"], []).append(r)
    for ticker_rows in all_by_ticker.values():
        ticker_rows.sort(key=lambda r: r["date"])

    # 고득점 신호만 필터링 (승격 대상)
    by_ticker: dict[str, list[dict]] = {}
    for r in records:
        if r["score"] >= threshold:
            by_ticker.setdefault(r["ticker"], []).append(r)

    signals: list[dict] = []
    for ticker, rows in by_ticker.items():
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

        # 모든 daily_data를 기준으로 수익률 계산
        all_rows = all_by_ticker.get(ticker, [])
        for r in selected:
            after = [x for x in all_rows if x["date"] > r["date"]]
            rets: dict[str, float | None] = {}
            for n, key in SIGNAL_RETURN_KEYS:
                rets[key] = (
                    (after[n - 1]["price"] / r["price"] - 1) * 100
                    if len(after) >= n and r["price"] else None
                )
            signals.append({
                "signal_date": r["date"], "ticker": r["ticker"],
                "signal_price": r["price"], "score": r["score"],
                "score_version": r["score_version"],
                "rsi": r["rsi"], "drawdown": r["drawdown"],
                "strategy_type": r["strategy_type"],
                "opportunity_score": r["opportunity_score"],
                "risk_level": r["risk_level"],
                "risk_score": r["risk_score"],
                "signal_confidence": r["signal_confidence"],
                "classification_confidence": r["classification_confidence"],
                "technical_score": r["technical_score"],
                "momentum_score": r["momentum_score"],
                "fundamental_score": r["fundamental_score"],
                "valuation_score": r["valuation_score"],
                "components": r["components"],
                **rets,
            })
    return sorted(signals, key=lambda x: (x["signal_date"], x["ticker"]), reverse=True)


def _run_backfill(weeks: int, tickers: list[str],
                  strategies: dict[str, tuple[str, float]] | None = None) -> dict:
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
        strategy, cconf = (strategies or {}).get(ticker, ("general", 0.5))
        records.extend(_backfill_ticker(ticker, df, start_str, end_str, market_df,
                                        strategy=strategy,
                                        classification_confidence=cconf))

    return {"records": records, "tickers": list(dfs),
            "start": start_str, "end": end_str, "weeks": weeks}


def _upsert_batches(db: Any, rows: list[dict], batch_size: int = UPSERT_BATCH) -> None:
    """opportunity_scores PK(date,ticker) 기준 upsert를 배치 단위로 실행한다.

    records에는 daily_data 전용 필드(score_version 등)도 섞여 있으므로
    opportunity_scores 컬럼만 추려 보낸다.
    """
    for i in range(0, len(rows), batch_size):
        batch = [{k: r[k] for k in OPPORTUNITY_SCORE_COLUMNS} for r in rows[i:i + batch_size]]
        db.table("opportunity_scores").upsert(batch, on_conflict="date,ticker").execute()


def main() -> None:
    parser = argparse.ArgumentParser(description="과거 opportunity_scores 백필")
    parser.add_argument("--weeks", type=int, default=52,
                        help="백필 기간 (df 마지막 날짜 기준 최근 N주, 기본 52주)")
    parser.add_argument("--tickers", help="콤마 구분 ticker (지정 시 watchlist 대체)")
    parser.add_argument("--with-signals", dest="with_signals", action="store_true",
                        default=True,
                        help="(deprecated) V8 백필은 signals 테이블에 저장하지 않음")
    parser.add_argument("--no-signals", dest="with_signals", action="store_false",
                        help="(deprecated) V8 백필은 signals 테이블에 저장하지 않음")
    parser.add_argument("--threshold", type=int, default=ALERT_SCORE,
                        help="(deprecated) 신호 승격 제거로 미사용 (CLI 호환용)")
    args = parser.parse_args()

    db = _get_db_if_available()
    tickers = _load_tickers(args.tickers, db)
    strategies = {t: resolve_strategy(t, db) for t in tickers}
    print(f"백필 대상 {len(tickers)}개: {', '.join(tickers)}", file=sys.stderr)
    result = _run_backfill(args.weeks, tickers, strategies)

    if not result["tickers"]:
        print("백필할 데이터 없음", file=sys.stderr)
        sys.exit(1)
    if db is None:
        print("경고: DB 연결 없음 - 저장 생략", file=sys.stderr)
        sys.exit(1)

    rows = result["records"]
    _upsert_batches(db, rows)
    print(f"백필 완료: {len(rows)}행 opportunity_scores "
          f"({result['start']} ~ {result['end']}, ticker {len(result['tickers'])}개)")


if __name__ == "__main__":
    main()