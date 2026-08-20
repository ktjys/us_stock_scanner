"""과거 기간 threshold별 백테스트 도구.

stock_scanner.py의 fetch_history/rsi를 재사용해, watchlist 종목의
1년 히스토리를 1회 fetch하고 지표를 미리 계산한 뒤 V8 행 단위로 스코어링한다.
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

from stock_scanner import (get_db, load_watchlist, fetch_history, rsi,
                           _relative_strength_series, resolve_strategy)
from opportunity_engine import (compute_technical_components, opportunity_score,
                                risk_score, signal_confidence)

import json as _json

RET_HORIZONS = (5, 10, 20)
DEFAULT_THRESHOLDS = "80,75,70,65,60,55,50,45,40"
SCORE_BANDS = [(40,44),(45,49),(50,54),(55,59),(60,64),(65,69),(70,74),(75,79),(80,100)]
RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "VERY_HIGH", "UNKNOWN")
COOLDOWN_DAYS = 5


def _json_safe(obj: Any) -> Any:
    """NaN/Infinity를 None으로 치환 — 브라우저 JSON.parse는 NaN 토큰을 거부하므로
    (pandas DataFrame의 None→NaN 변환 + json.dump 기본 allow_nan=True 조합으로
    NaN이 파일에 새겨질 수 있다) 대시보드용 JSON에는 항상 통과시킨다."""
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _get_db_if_available() -> Any:
    """SUPABASE_URL/KEY env가 있으면 DB 클라이언트, 없으면 None (CSV 폴백)."""
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
        try:
            return get_db()
        except Exception as e:  # noqa: BLE001 - 인증 실패 등은 CSV로 폴백
            print(f"경고: DB 연결 실패, CSV로 대체 - {e}", file=sys.stderr)
    return None


def _load_historical_classification(
    ticker: str,
    signal_date: str,
    db: Any,
) -> dict | None:
    """historical_classification 테이블에서 given date의 스냅샷을 조회한다.

    없으면 current classification을 반환(fallback)한다.
    """
    if db is None:
        return None
    try:
        row = db.execute(
            """
            SELECT strategy_type, confidence, source
            FROM historical_classification
            WHERE ticker = %s AND effective_date <= %s
            ORDER BY effective_date DESC
            LIMIT 1
            """,
            (ticker, signal_date),
        ).fetchone()
        if row is None:
            return None
        return {
            "strategy_type": row[0],
            "confidence": row[1],
            "source": row[2],
        }
    except Exception:
        return None


def _load_historical_fundamental(
    ticker: str,
    signal_date: str,
    db: Any,
) -> dict | None:
    """historical_fundamental 테이블에서 signal_date 이전에 사용 가능한
    fundamental 데이터를 jsonb로 반환한다. 없으면 None을 반환한다.
    """
    if db is None:
        return None
    try:
        row = db.execute(
            """
            SELECT data
            FROM historical_fundamental
            WHERE ticker = %s AND available_date <= %s
            ORDER BY available_date DESC
            LIMIT 1
            """,
            (ticker, signal_date),
        ).fetchone()
        if row is None:
            return None
        return _json.loads(row[0])
    except Exception:
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


def _future_metrics(df: pd.DataFrame, idx: int) -> dict[int, dict[str, float | None]]:
    """신호 이후 N거래일 종가수익률 + MFE/MAE를 계산한다.

    MFE = 해당 기간 고가 기준 최대 상승폭,
    MAE = 해당 기간 저가 기준 최대 하락폭.
    모두 신호 당일 종가를 기준으로 계산한다.
    """
    entry = float(df["Close"].iloc[idx])
    out: dict[int, dict[str, float | None]] = {}
    for n in RET_HORIZONS:
        if idx + n >= len(df):
            out[n] = {"ret": None, "mfe": None, "mae": None}
            continue
        window = df.iloc[idx + 1: idx + n + 1]
        ret = (float(df["Close"].iloc[idx + n]) / entry - 1) * 100
        mfe = (float(window["High"].max()) / entry - 1) * 100
        mae = (float(window["Low"].min()) / entry - 1) * 100
        out[n] = {"ret": ret, "mfe": mfe, "mae": mae}
    return out


def _backtest_ticker(ticker: str, df: pd.DataFrame, thresholds: list[int],
                     start: str, end: str,
                     market_df: pd.DataFrame | None = None,
                     mode: str = "v8", strategy: str = "general",
                     db: Any | None = None) -> list[dict]:
    """한 ticker를 행 단위로 순회한다.

    thresholds의 최소값 이상인 점수만 기록하되, 한 날짜에는 한 건만 기록한다.
    이후 _apply_cooldown에서 실전형 중복 신호를 제거한다.
    전략(strategy)별 opportunity_score로 스코어링한다.
    """
    df = _compute_indicators(df)
    if df.empty:
        return []

    # --- historical data loading (Phase 6) ---
    # db가 있으면 시점별 classification/fundamental 스냅샷을 우선 사용
    _h_class = None
    _h_fund = None
    if db is not None:
        _h_class = _load_historical_classification(ticker, str(df.index[i].date()), db)
        _h_fund = _load_historical_fundamental(ticker, str(df.index[i].date()), db)
    # ------------------------------------------

    idx = df.index
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    mask = (idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))
    rs_series = _relative_strength_series(df, market_df)
    min_score = min(thresholds) if thresholds else 0

    records: list[dict] = []
    close = df["Close"].astype(float)
    for i in range(len(df)):
        if not mask[i] or i < 2:
            continue
        if (pd.isna(df["rsi"].iloc[i]) or pd.isna(df["ma20"].iloc[i])
                or pd.isna(df["ma50"].iloc[i]) or pd.isna(df["high60"].iloc[i])
                or pd.isna(df["avgvol"].iloc[i]) or pd.isna(df["rsi"].iloc[i - 1])
                or pd.isna(df["rsi"].iloc[i - 2])):
            continue

        price = float(close.iloc[i])
        rv = float(df["rsi"].iloc[i])
        prev = float(df["rsi"].iloc[i - 1])
        ma20 = float(df["ma20"].iloc[i])
        ma50 = float(df["ma50"].iloc[i])
        dd = (price / float(df["high60"].iloc[i]) - 1) * 100
        vr = float(df["Volume"].iloc[i]) / float(df["avgvol"].iloc[i])
        rs5 = None if pd.isna(rs_series.iloc[i]) else float(rs_series.iloc[i])

        comps = compute_technical_components(df, i, market_df, rs_series=rs_series)
        if comps is None:
            continue
        score = opportunity_score(comps, strategy)
        if score < min_score:
            continue
        score_keys = {name + "_score": comps[name] for name in (
            "rsi_state", "rsi_rebound", "price_rebound", "drawdown",
            "ma20", "trend", "relative_strength", "volume")}
        v8_extra: dict[str, Any] = {
            "momentum_20d_score": comps["momentum_20d"],
            "breakout_score": comps["breakout"],
            "strategy": strategy,
        }

        # 리스크/신뢰도 (info=None: 백테스트에서 Yahoo info 조회는 너무 느려 생략.
        # risk_score는 info가 없으면 beta를 중간값으로 처리하므로 그대로 점수화된다)
        risk_result = risk_score(df, i, info=None)
        risk_total, risk_level = (risk_result if risk_result is not None
                                  else (None, "UNKNOWN"))
        confidence = signal_confidence(score)

        metrics = _future_metrics(df, i)
        date = str(df.index[i])[:10]

        records.append({
            # 기본 정보
            "date": date,
            "ticker": ticker,
            "score": score,
            "score_mode": mode,
            "price": price,

            # 리스크/신뢰도
            "risk_score": risk_total,
            "risk_level": risk_level,
            "signal_confidence": confidence,

            # 원본 지표
            "rsi": rv,
            "rsi_delta": rv - prev,
            "ma20": ma20,
            "ma50": ma50,
            "drawdown": dd,
            "volume_ratio": vr,
            "relative_strength_5d": rs5,

            # 세부 점수
            **score_keys,
            **v8_extra,

            # 미래 수익률
            "ret5": metrics[5]["ret"],
            "ret10": metrics[10]["ret"],
            "ret20": metrics[20]["ret"],

            # MFE
            "mfe5": metrics[5]["mfe"],
            "mfe10": metrics[10]["mfe"],
            "mfe20": metrics[20]["mfe"],

            # MAE
            "mae5": metrics[5]["mae"],
            "mae10": metrics[10]["mae"],
            "mae20": metrics[20]["mae"],
        })

    return records


def _apply_cooldown(records: list[dict], cooldown_days: int = COOLDOWN_DAYS) -> list[dict]:
    """동일 종목의 연속 신호를 cooldown 기간 내 1건으로 제한한다.

    같은 기간에 여러 점수가 발생하면 첫 신호가 아니라 가장 높은 점수를 남긴다.
    """
    out: list[dict] = []
    by_ticker: dict[str, list[dict]] = {}
    for r in records:
        by_ticker.setdefault(r["ticker"], []).append(r)

    for rows in by_ticker.values():
        rows = sorted(rows, key=lambda r: r["date"])
        i = 0
        while i < len(rows):
            anchor = rows[i]
            anchor_date = pd.Timestamp(anchor["date"])
            group = [anchor]
            j = i + 1
            while j < len(rows):
                d = pd.Timestamp(rows[j]["date"])
                if (d - anchor_date).days > cooldown_days:
                    break
                group.append(rows[j])
                j += 1
            # cooldown 구간에서는 가장 높은 점수의 신호 하나만 채택
            chosen = max(group, key=lambda r: (r["score"], r["date"]))
            chosen = dict(chosen)
            chosen["cooldown_count"] = len(group)
            out.append(chosen)
            i = j
    return sorted(out, key=lambda r: (r["date"], r["ticker"]))


def _run_backtest(thresholds: list[int], weeks: int,
                  tickers: list[str], mode: str = "v8",
                  db: Any | None = None) -> dict:
    """fetch + V8 백테스트. QQQ 상대강도와 5일 cooldown을 함께 적용한다."""
    if db is None:
        db = _get_db_if_available()
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
        return {"records": [], "raw_records": [], "tickers": [], "start": "", "end": "", "weeks": weeks}

    end = min(df.index.max() for df in dfs.values())
    start = end - timedelta(weeks=weeks)
    start_str, end_str = str(start.date()), str(end.date())

    raw_records: list[dict] = []
    for ticker, df in dfs.items():
        strategy, _ = resolve_strategy(ticker, db)
        raw_records.extend(_backtest_ticker(
            ticker, df, thresholds, start_str, end_str, market_df,
            mode=mode, strategy=strategy,
        ))

    records = _apply_cooldown(raw_records, COOLDOWN_DAYS)
    return {"records": records, "raw_records": raw_records,
            "tickers": list(dfs), "start": start_str,
            "end": end_str, "weeks": weeks}


def _fmt_ret(v: float | None) -> str:
    return "-" if v is None else f"{v:+.2f}%"


def _fmt_avg(v: float | None) -> str:
    return "-" if v is None else f"{v:+.1f}%"


def _fmt_win(v: float | None) -> str:
    return "-" if v is None else f"{v:.1f}%"


def _score_band(score: int) -> str:
    for lo, hi in SCORE_BANDS:
        if lo <= score <= hi:
            return f"{lo}-{hi}" if hi < 100 else "80+"
    return "<40"


def _summarize_bands(records: list[dict],
                     strategy_filter: str | None = None) -> pd.DataFrame:
    """점수 구간별(중복 신호 제거 후) 성과 요약.

    strategy_filter가 주어지면 해당 strategy 신호만 집계한다 (None이면 전체).
    """
    if strategy_filter is not None:
        records = [r for r in records if r.get("strategy") == strategy_filter]
    rows = []
    for lo, hi in SCORE_BANDS:
        label = f"{lo}-{hi}" if hi < 100 else "80+"
        recs = [r for r in records if lo <= r["score"] <= hi]

        def vals(key: str) -> list[float]:
            return [r[key] for r in recs if r[key] is not None]

        def mean(xs: list[float]) -> float | None:
            return sum(xs) / len(xs) if xs else None

        r5, r10, r20 = vals("ret5"), vals("ret10"), vals("ret20")
        win5 = sum(x > 0 for x in r5) / len(r5) * 100 if r5 else None
        rows.append({
            "band": label,
            "min_score": lo,
            "max_score": hi,
            "signals": len(recs),
            "win_rate": win5,
            "avg_5d": mean(r5),
            "avg_10d": mean(r10),
            "avg_20d": mean(r20),
            "avg_mae_5d": mean(vals("mae5")),
            "avg_mfe_5d": mean(vals("mfe5")),
            "sample_size": len(r5),
        })
    return pd.DataFrame(rows)


def _group_band_summaries(records: list[dict], key: str,
                          default: str) -> dict[str, list[dict]]:
    """records를 key 필드 값별로 그룹핑해 점수구간 요약을 만든다.

    key 필드가 없는 레코드는 default로 귀속한다. 반환 형태:
    {그룹명: _summarize_bands의 행 dict 리스트}.
    """
    by_group: dict[str, list[dict]] = {}
    for r in records:
        by_group.setdefault(r.get(key) or default, []).append(r)
    return {
        group: _summarize_bands(recs).to_dict(orient="records")
        for group, recs in sorted(by_group.items())
    }


def _summarize_by_strategy(records: list[dict]) -> dict[str, list[dict]]:
    """전략별 점수구간 요약 (strategy 없음 = general)."""
    return _group_band_summaries(records, "strategy", "general")


def _summarize_by_risk(records: list[dict]) -> dict[str, list[dict]]:
    """리스크 등급별 점수구간 요약 (risk_level 없음 = UNKNOWN)."""
    return _group_band_summaries(records, "risk_level", "UNKNOWN")


def walk_forward_validation(
    df: pd.DataFrame,
    market_df: pd.DataFrame,
    train_window: int = 252,
    test_window: int = 63,
    thresholds: list[int] | None = None,
    strategy: str = "general",
) -> list[dict]:
    """Walk-forward validation: train on train_window, test on test_window,
    slide forward by test_window.

    Returns list of dicts with train/test period info and performance metrics.
    """
    if thresholds is None:
        thresholds = [40, 45, 50, 55, 60, 65, 70, 75, 80]

    df = _compute_indicators(df)
    if df.empty:
        return []

    idx = df.index
    if idx.tz is not None:
        idx = idx.tz_localize(None)

    rs_series = _relative_strength_series(df, market_df)
    min_score = min(thresholds)

    results: list[dict] = []
    n = len(df)

    start_idx = train_window
    while start_idx + test_window < n:
        train_end = start_idx
        test_start = train_end
        test_end = min(test_start + test_window, n)

        train_df = df.iloc[:train_end]
        test_df = df.iloc[test_start:test_end]
        test_market_df = market_df.iloc[test_start:test_end] if len(market_df) > test_start else pd.DataFrame()

        if len(test_df) < 3:
            break

        # Run backtest on test window
        test_records = _backtest_ticker(
            "WALK_FWD", test_df, thresholds,
            str(test_df.index[0].date()), str(test_df.index[-1].date()),
            test_market_df, mode="v8", strategy=strategy
        )

        # Aggregate performance
        if test_records:
            ret5_vals = [r["ret5"] for r in test_records if r["ret5"] is not None]
            ret10_vals = [r["ret10"] for r in test_records if r["ret10"] is not None]
            ret20_vals = [r["ret20"] for r in test_records if r["ret20"] is not None]

            win5 = sum(1 for v in ret5_vals if v > 0) / len(ret5_vals) * 100 if ret5_vals else None
            avg5 = sum(ret5_vals) / len(ret5_vals) if ret5_vals else None
            avg10 = sum(ret10_vals) / len(ret10_vals) if ret10_vals else None
            avg20 = sum(ret20_vals) / len(ret20_vals) if ret20_vals else None
        else:
            win5 = avg5 = avg10 = avg20 = None

        results.append({
            "train_start": str(train_df.index[0].date()),
            "train_end": str(train_df.index[-1].date()),
            "test_start": str(test_df.index[0].date()),
            "test_end": str(test_df.index[-1].date()),
            "train_days": len(train_df),
            "test_days": len(test_df),
            "signals": len(test_records),
            "win_rate_5d": win5,
            "avg_ret_5d": avg5,
            "avg_ret_10d": avg10,
            "avg_ret_20d": avg20,
        })

        start_idx += test_window

    return results


def build_backtest_summary(weeks: int = 26, tickers: str | None = None,
                           mode: str = "v8") -> str:
    """주간 리포트용 V8 점수구간 요약 텍스트."""
    try:
        thresholds = sorted({int(t) for t in DEFAULT_THRESHOLDS.split(",")}, reverse=True)
        db = _get_db_if_available()
        result = _run_backtest(thresholds, weeks, _load_tickers(tickers, db), mode=mode, db=db)
        if not result["tickers"]:
            return "백테스트 데이터 없음"
        label = "V8"
        lines = [f"📊 {label} 백테스트 (최근 {weeks}주, {len(result['tickers'])}종목, cooldown {COOLDOWN_DAYS}일)"]
        for _, row in _summarize_bands(result["records"]).iterrows():
            win = row["win_rate"]
            lines.append(
                f"{row['band']}점: {row['signals']}건 | "
                f"5일승률 {win:.1f}% | 5일평균 {row['avg_5d']:+.2f}%"
                if win == win and row["avg_5d"] == row["avg_5d"]
                else f"{row['band']}점: {row['signals']}건 | 데이터 부족"
            )
        return "\n".join(lines)
    except Exception:
        return ""


def _build_json_report(records: list[dict], thresholds: list[int], tickers: list[str],
                       start: str, end: str, weeks: int,
                       raw_records: list[dict] | None = None,
                       version: str = "v8", breakdown: str = "none") -> dict:
    """대시보드용 점수구간 JSON 리포트 (version은 실행된 스코어링 모드).

    breakdown="strategy"/"all"이면 전략별, "risk"/"all"이면 리스크 등급별
    점수구간 요약을 by_strategy/by_risk에 추가한다.
    """
    bands = _summarize_bands(records).to_dict(orient="records")
    uniq = {}
    for r in sorted(records, key=lambda r: (r["date"], r["ticker"], -r["score"])):
        uniq.setdefault((r["date"], r["ticker"]), r)
    recent = [
        {"date": r["date"], "ticker": r["ticker"], "score": r["score"],
         "ret5": r["ret5"], "ret10": r["ret10"], "ret20": r["ret20"],
         "mae5": r["mae5"], "mfe5": r["mfe5"],
         "cooldown_count": r.get("cooldown_count", 1)}
        for r in sorted(uniq.values(), key=lambda r: (r["date"], r["score"]), reverse=True)[:30]
    ]
    report = {
        "version": version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_start": start,
        "period_end": end,
        "weeks": weeks,
        "ticker_count": len(tickers),
        "tickers": tickers,
        "cooldown_days": COOLDOWN_DAYS,
        "thresholds": [],  # backward compatibility
        "bands": bands,
        "recent_signals": recent,
        "raw_signal_count": len(raw_records or []),
        "cooldown_signal_count": len(records),
        "raw_records": raw_records or [],
    }
    if breakdown in ("strategy", "all"):
        report["by_strategy"] = _summarize_by_strategy(records)
    if breakdown in ("risk", "all"):
        report["by_risk"] = _summarize_by_risk(records)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="V8 점수구간 백테스트")
    parser.add_argument("--thresholds", default=DEFAULT_THRESHOLDS,
                        help="최저 스코어 필터로 사용할 임계값 목록 (기본 40~80)")
    parser.add_argument("--weeks", type=int, default=52,
                        help="시뮬레이션 기간 (기본 52주)")
    parser.add_argument("--tickers", help="콤마 구분 ticker (지정 시 watchlist 대체)")
    parser.add_argument("--verbose", action="store_true", help="신호 상세 목록 출력")
    parser.add_argument("--json", default=None, help="결과 JSON 경로")
    parser.add_argument("--breakdown", choices=["none", "strategy", "risk", "all"],
                        default="none",
                        help="전략/리스크 등급별 점수구간 요약 추가 (기본 none)")
    args = parser.parse_args()

    thresholds = sorted({int(t) for t in args.thresholds.split(",")}, reverse=True)
    if not thresholds:
        parser.error("--thresholds는 1개 이상의 정수가 필요합니다")

    db = _get_db_if_available()
    tickers = _load_tickers(args.tickers, db)
    print(f"백테스트 대상 {len(tickers)}개: {', '.join(tickers)}", file=sys.stderr)
    result = _run_backtest(thresholds, args.weeks, tickers, db=db)

    if not result["tickers"]:
        print("백테스트할 데이터 없음", file=sys.stderr)
        sys.exit(1)

    print(f"=== V8 백테스트 결과 ({result['start']} ~ {result['end']}, "
          f"ticker {len(result['tickers'])}개, cooldown {COOLDOWN_DAYS}일) ===")
    print(_summarize_bands(result["records"]).to_string(index=False))

    if args.breakdown in ("strategy", "all"):
        print("\n=== 전략별 점수구간 요약 ===")
        for strat, band_rows in _summarize_by_strategy(result["records"]).items():
            print(f"--- {strat} ---")
            print(pd.DataFrame(band_rows).to_string(index=False))

    if args.breakdown in ("risk", "all"):
        print("\n=== 리스크 등급별 점수구간 요약 ===")
        for level, band_rows in _summarize_by_risk(result["records"]).items():
            print(f"--- {level} ---")
            print(pd.DataFrame(band_rows).to_string(index=False))

    if args.json:
        report = _build_json_report(
            result["records"], thresholds, result["tickers"],
            result["start"], result["end"], args.weeks, result["raw_records"],
            version="v8", breakdown=args.breakdown
        )
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(_json_safe(report), f, ensure_ascii=False, indent=2)
        print(f"JSON 저장 완료: {args.json}", file=sys.stderr)

    if args.verbose:
        detail = pd.DataFrame([
            {"날짜": r["date"], "ticker": r["ticker"], "점수": r["score"],
             "모드": "V8",
             "구간": _score_band(r["score"]),
             "5일": _fmt_ret(r["ret5"]), "10일": _fmt_ret(r["ret10"]),
             "20일": _fmt_ret(r["ret20"]),
             "cooldown내 원신호": r.get("cooldown_count", 1)}
            for r in result["records"]
        ])
        print("\n=== 채택 신호 상세 ===")
        print(detail.to_string(index=False) if not detail.empty else "신호 없음")


if __name__ == "__main__":
    main()
