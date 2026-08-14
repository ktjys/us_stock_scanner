"""과거 기간 threshold별 백테스트 도구.

stock_scanner.py의 fetch_history/rsi/score_signal을 재사용해, watchlist 종목의
1년 히스토리를 1회 fetch하고 지표를 미리 계산한 뒤 행 단위로 스코어링한다.
모든 스코어링은 해당 행까지의 데이터만 사용하므로 lookahead bias가 없다.
stdout에는 결과 테이블만, 진행 로그/경고는 stderr로 출력한다.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from stock_scanner import (ALERT_SCORE, get_db, load_watchlist,
                           fetch_history, rsi, score_signal)

RET_HORIZONS = (5, 10, 20)
DEFAULT_THRESHOLDS = f"{ALERT_SCORE},60,55"


def _get_db_if_available() -> Any:
    """SUPABASE_URL/KEY env가 있으면 DB 클라이언트, 없으면 None (CSV 폴백)."""
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
        try:
            return get_db()
        except Exception as e:  # noqa: BLE001 - 인증 실패 등은 CSV로 폴백
            print(f"경고: DB 연결 실패, CSV로 대체 - {e}", file=sys.stderr)
    return None


def _load_tickers(tickers_arg: str | None, db: Any | None = None) -> list[str]:
    """--tickers가 없으면 스캐너와 동일하게 Supabase watchlist를 우선 사용한다.

    load_watchlist(db): 테이블 우선(active만), 비어 있거나 db=None이면 CSV.
    """
    if tickers_arg:
        return [t.strip().upper() for t in tickers_arg.split(",") if t.strip()]
    return load_watchlist(db)


def _compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """지표를 한 번에 계산해 행 단위 스코어링에 재사용한다.

    모든 컬럼은 해당 행까지의 데이터만 참조하므로 lookahead bias가 없다.
    """
    df = df.copy()
    df["rsi"] = rsi(df["Close"])
    df["ma20"] = df["Close"].rolling(20).mean()
    df["ma50"] = df["Close"].rolling(50).mean()
    df["high60"] = df["High"].rolling(60).max()
    df["avgvol"] = df["Volume"].rolling(20).mean()
    return df


def _future_returns(close: pd.Series, idx: int) -> dict[int, float | None]:
    """신호 행 이후 N거래일 수익률(%); 이후 데이터가 부족하면 None."""
    out = {}
    for n in RET_HORIZONS:
        if idx + n < len(close):
            out[n] = (float(close.iloc[idx + n]) / float(close.iloc[idx]) - 1) * 100
        else:
            out[n] = None
    return out


def _backtest_ticker(ticker: str, df: pd.DataFrame, thresholds: list[int],
                     start: str, end: str) -> list[dict]:
    """한 ticker를 행 단위로 순회해 threshold 이상 신호를 기록한다."""
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
        if score < thresholds[-1]:
            continue
        rets = _future_returns(close, i)
        date = str(df.index[i])[:10]
        for t in thresholds:
            if score >= t:
                records.append({
                    "date": date, "ticker": ticker, "score": score, "price": price,
                    "ret5": rets[5], "ret10": rets[10], "ret20": rets[20],
                    "threshold": t,
                })
    return records


def _run_backtest(thresholds: list[int], weeks: int,
                  tickers: list[str]) -> dict:
    """fetch 루프 + 백테스트 실행 (main과 build_backtest_summary 공용).

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
        records.extend(_backtest_ticker(ticker, df, thresholds, start_str, end_str))

    return {"records": records, "tickers": list(dfs),
            "start": start_str, "end": end_str, "weeks": weeks}


def _fmt_ret(v: float | None) -> str:
    return "-" if v is None else f"{v:+.2f}%"


def _fmt_avg(v: float | None) -> str:
    return "-" if v is None else f"{v:+.1f}%"


def _fmt_win(v: float | None) -> str:
    return "-" if v is None else f"{v:.1f}%"


def _summarize(records: list[dict], thresholds: list[int]) -> pd.DataFrame:
    """threshold별 신호 건수/승률/평균 수익률 요약 테이블."""
    rows = []
    for t in thresholds:
        recs = [r for r in records if r["threshold"] == t]
        rets = {n: [r[f"ret{n}"] for r in recs if r[f"ret{n}"] is not None]
                for n in RET_HORIZONS}

        def mean(xs: list[float]) -> float | None:
            return sum(xs) / len(xs) if xs else None

        win5 = None
        if rets[5]:
            win5 = sum(x > 0 for x in rets[5]) / len(rets[5]) * 100
        rows.append({
            "threshold": t,
            "신호수": len(recs),
            "승률(5일)": _fmt_win(win5),
            "평균수익률 5일": _fmt_avg(mean(rets[5])),
            "평균수익률 10일": _fmt_avg(mean(rets[10])),
            "평균수익률 20일": _fmt_avg(mean(rets[20])),
            "표본수": len(rets[5]),
        })
    return pd.DataFrame(rows)


def build_backtest_summary(weeks: int = 26, tickers: str | None = None) -> str:
    """주간 리포트용 백테스트 요약 텍스트 (실패 시 빈 문자열)."""
    try:
        thresholds = sorted({int(t) for t in DEFAULT_THRESHOLDS.split(",")},
                            reverse=True)
        db = _get_db_if_available()
        result = _run_backtest(thresholds, weeks, _load_tickers(tickers, db))
        if not result["tickers"]:
            return "백테스트 데이터 없음"
        lines = [f"📊 백테스트 (최근 {weeks}주, {len(result['tickers'])}종목)"]
        for _, row in _summarize(result["records"], thresholds).iterrows():
            win = row["승률(5일)"]
            if win == "-":
                lines.append(f"{row['threshold']}점: {row['신호수']}건 | 데이터 부족")
            else:
                lines.append(f"{row['threshold']}점: {row['신호수']}건 | 승률 {win}")
        return "\n".join(lines)
    except Exception:
        return ""


def _build_json_report(records: list[dict], thresholds: list[int], tickers: list[str],
                       start: str, end: str, weeks: int) -> dict:
    """대시보드용 JSON 리포트 dict를 만든다 (스키마는 dashboard와 공유)."""
    thr_rows = []
    for t in sorted(thresholds, reverse=True):
        recs = [r for r in records if r["threshold"] == t]
        rets = {n: [r[f"ret{n}"] for r in recs if r[f"ret{n}"] is not None]
                for n in RET_HORIZONS}

        def mean(xs: list[float]) -> float | None:
            return sum(xs) / len(xs) if xs else None

        win5 = None
        if rets[5]:
            win5 = sum(x > 0 for x in rets[5]) / len(rets[5]) * 100
        thr_rows.append({
            "threshold": t,
            "signals": len(recs),
            "win_rate": win5,
            "avg_5d": mean(rets[5]),
            "avg_10d": mean(rets[10]),
            "avg_20d": mean(rets[20]),
            "sample_size": len(rets[5]),
        })

    uniq = {}
    for r in sorted(records, key=lambda r: (r["date"], r["ticker"], -r["score"])):
        uniq.setdefault((r["date"], r["ticker"]), r)
    recent = [
        {"date": r["date"], "ticker": r["ticker"], "score": r["score"],
         "ret5": r["ret5"], "ret10": r["ret10"], "ret20": r["ret20"]}
        for r in sorted(uniq.values(), key=lambda r: (r["date"], r["score"]),
                        reverse=True)[:20]
    ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_start": start,
        "period_end": end,
        "weeks": weeks,
        "ticker_count": len(tickers),
        "tickers": tickers,
        "thresholds": thr_rows,
        "recent_signals": recent,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="과거 기간 threshold별 백테스트")
    parser.add_argument("--thresholds", default=DEFAULT_THRESHOLDS,
                        help="콤마 구분 스코어 임계값 (기본: 65,60,55)")
    parser.add_argument("--weeks", type=int, default=26,
                        help="시뮬레이션 기간 (df 마지막 날짜 기준 최근 N주)")
    parser.add_argument("--tickers", help="콤마 구분 ticker (지정 시 watchlist 대체)")
    parser.add_argument("--verbose", action="store_true", help="신호 상세 목록 출력")
    parser.add_argument("--json", default=None,
                        help="결과를 이 경로에 JSON으로 저장")
    args = parser.parse_args()

    thresholds = sorted({int(t) for t in args.thresholds.split(",")}, reverse=True)
    if not thresholds:
        parser.error("--thresholds는 1개 이상의 정수가 필요합니다")

    db = _get_db_if_available()
    tickers = _load_tickers(args.tickers, db)
    print(f"백테스트 대상 {len(tickers)}개: {', '.join(tickers)}", file=sys.stderr)
    result = _run_backtest(thresholds, args.weeks, tickers)

    if not result["tickers"]:
        print("백테스트할 데이터 없음", file=sys.stderr)
        sys.exit(1)

    print(f"=== 백테스트 결과 (기간: {result['start']} ~ {result['end']}, "
          f"ticker {len(result['tickers'])}개) ===")
    print(_summarize(result["records"], thresholds).to_string(index=False))

    if args.json:
        report = _build_json_report(result["records"], thresholds, result["tickers"],
                                    result["start"], result["end"], args.weeks)
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"JSON 저장 완료: {args.json}", file=sys.stderr)

    if args.verbose:
        uniq = {}
        for r in sorted(result["records"], key=lambda r: (r["date"], r["ticker"], -r["score"])):
            uniq.setdefault((r["date"], r["ticker"]), r)
        print("\n=== 신호 상세 ===")
        if uniq:
            detail = pd.DataFrame([
                {"날짜": r["date"], "ticker": r["ticker"], "점수": r["score"],
                 "5일": _fmt_ret(r["ret5"]), "10일": _fmt_ret(r["ret10"]),
                 "20일": _fmt_ret(r["ret20"])}
                for r in uniq.values()
            ])
            print(detail.to_string(index=False))
        else:
            print("신호 없음")


if __name__ == "__main__":
    main()
