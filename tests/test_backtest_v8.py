"""V8 백테스트 모드 (--mode v8) 테스트: 전략별 스코어링 정합성."""

import random

import pandas as pd

from backtest import _backtest_ticker, _build_json_report
from stock_scanner import resolve_strategy


def _make_df(n=120, daily=0.01, seed=1):
    """완만한 상승 추세 합성 df (고점 근접 + 20일 모멘텀 확보용)."""
    rng = random.Random(seed)
    closes = [100.0]
    for k in range(1, n):
        closes.append(closes[-1] * (1 + daily))
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "Open": closes,
        "High": [c * 1.01 for c in closes],
        "Low": [c * 0.99 for c in closes],
        "Close": closes,
        "Volume": [1_000_000 + rng.randint(-200_000, 200_000) for _ in range(n)],
    }, index=idx)


def _backtest(ticker, df, mode="v7", strategy="general"):
    start = str(df.index[0].date())
    end = str(df.index[-1].date())
    return _backtest_ticker(ticker, df, [0], start, end, mode=mode, strategy=strategy)


def _last(records):
    return [r for r in records if r["date"] == str(records[-1]["date"])][0]


def test_v8_general_equals_v7_score_on_same_rows():
    df = _make_df()
    v7 = _backtest("TEST", df, mode="v7")
    v8 = _backtest("TEST", df, mode="v8", strategy="general")
    by_date = {r["date"]: r["score"] for r in v7}
    assert by_date == {r["date"]: r["score"] for r in v8}


def test_v8_general_record_matches_v7_field_schema():
    df = _make_df()
    v7 = _backtest("TEST", df, mode="v7")
    v8 = _backtest("TEST", df, mode="v8", strategy="general")
    last_v7 = _last(v7)
    last_v8 = _last(v8)
    assert last_v8["ticker"] == "TEST"
    assert last_v8["strategy"] == "general"
    assert last_v8["momentum_20d_score"] == 10
    assert last_v8["breakout_score"] == 10
    assert last_v8["rsi_state_score"] == last_v7["rsi_state_score"]
    assert "strategy" not in last_v7
    assert "momentum_20d_score" not in last_v7


def test_v8_speculative_scores_above_general_on_uptrend():
    # 상승 추세(고점 근접 + 20일 모멘텀)에서는 speculative가 momentum/breakout
    # 가중치로 general보다 높은 기회 점수를 낸다.
    df = _make_df()
    gen = _last(_backtest("TEST", df, mode="v8", strategy="general"))
    spec = _last(_backtest("TEST", df, mode="v8", strategy="speculative"))
    assert spec["score"] > gen["score"]


class _EmptyQuery:
    def select(self, *a):
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": []})()


class _EmptyDb:
    def table(self, name):
        return _EmptyQuery()


def test_resolve_strategy_empty_db_falls_back_to_general():
    assert resolve_strategy("XXX", db=_EmptyDb(), info=None) == ("general", 0.5)
    assert resolve_strategy("XXX", db=None, info=None) == ("general", 0.5)


def test_json_report_version_reflects_mode():
    report = _build_json_report([], [80], ["AAPL"], "2026-01-01", "2026-02-01", 4,
                                raw_records=[], version="v8")
    assert report["version"] == "v8"


def test_json_report_both_mode_splits_by_score_mode():
    records = [
        {"date": "2026-01-05", "ticker": "AAPL", "score": 70,
         "score_mode": "v7", "ret5": 1.0, "ret10": 2.0, "ret20": 3.0,
         "mae5": -1.0, "mfe5": 2.0, "cooldown_count": 1},
        {"date": "2026-01-05", "ticker": "AAPL", "score": 75,
         "score_mode": "v8", "ret5": 1.5, "ret10": 2.5, "ret20": 3.5,
         "mae5": -1.0, "mfe5": 2.0, "cooldown_count": 1},
    ]
    report = _build_json_report(records, [80], ["AAPL"], "2026-01-01",
                                "2026-02-01", 4, raw_records=records,
                                version="both")
    assert report["version"] == "both"
    assert set(report["modes"]) == {"v7", "v8"}
    assert report["modes"]["v7"]["cooldown_signal_count"] == 1
    assert report["modes"]["v8"]["cooldown_signal_count"] == 1
    assert report["modes"]["v7"]["raw_records"][0]["score"] == 70
    assert report["modes"]["v8"]["raw_records"][0]["score"] == 75


def test_backtest_ticker_records_tag_score_mode():
    df = _make_df()
    v8 = _backtest("TEST", df, mode="v8", strategy="general")
    assert all(r["score_mode"] == "v8" for r in v8)
    v7 = _backtest("TEST", df, mode="v7")
    assert all(r["score_mode"] == "v7" for r in v7)
