"""V8 백테스트 모드 테스트: 전략별 스코어링 정합성."""

import random

import pandas as pd

from backtest import (_backtest_ticker, _build_json_report, _summarize_bands,
                      _summarize_by_risk, _summarize_by_strategy)
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


def _backtest(ticker, df, mode="v8", strategy="general"):
    start = str(df.index[0].date())
    end = str(df.index[-1].date())
    return _backtest_ticker(ticker, df, [0], start, end, mode=mode, strategy=strategy)


def _last(records):
    return [r for r in records if r["date"] == str(records[-1]["date"])][0]


def test_v8_general_record_schema():
    df = _make_df()
    v8 = _backtest("TEST", df, mode="v8", strategy="general")
    last_v8 = _last(v8)
    assert last_v8["ticker"] == "TEST"
    assert last_v8["strategy"] == "general"
    assert last_v8["momentum_20d_score"] == 10
    assert last_v8["breakout_score"] == 10
    assert last_v8["rsi_state_score"] >= 0


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


def test_backtest_ticker_records_tag_score_mode():
    df = _make_df()
    v8 = _backtest("TEST", df, mode="v8", strategy="general")
    assert all(r["score_mode"] == "v8" for r in v8)


def _band_records():
    """전략 필드가 있는 V8 스타일 레코드 3건 (quality 2건 + speculative 1건)."""
    return [
        {"score": 70, "strategy": "quality", "ret5": 2.0, "ret10": 3.0, "ret20": 4.0,
         "mae5": -1.0, "mfe5": 3.0},
        {"score": 72, "strategy": "quality", "ret5": 1.5, "ret10": 2.5, "ret20": 3.5,
         "mae5": -1.0, "mfe5": 3.0},
        {"score": 71, "strategy": "speculative", "ret5": 1.0, "ret10": 2.0, "ret20": 3.0,
         "mae5": -2.0, "mfe5": 2.0},
    ]


def test_summarize_bands_default_keeps_all_records():
    df = _summarize_bands(_band_records())
    assert df["signals"].sum() == 3


def test_summarize_bands_strategy_filter_filters_records():
    df = _summarize_bands(_band_records(), strategy_filter="quality")
    assert df["signals"].sum() == 2
    row = df[df["band"] == "70-74"].iloc[0]
    assert row["avg_5d"] == 1.75
    assert row["signals"] == 2


def test_summarize_by_strategy_groups_band_summaries():
    out = _summarize_by_strategy(_band_records())
    assert set(out) == {"quality", "speculative"}
    quality = {b["band"]: b for b in out["quality"]}
    speculative = {b["band"]: b for b in out["speculative"]}
    assert quality["70-74"]["signals"] == 2
    assert speculative["70-74"]["signals"] == 1


def test_json_report_by_strategy_added_when_breakdown_strategy():
    records = [
        {"date": "2026-01-05", "ticker": "AAPL", "score": 70,
         "strategy": "quality", "score_mode": "v8",
         "ret5": 1.0, "ret10": 2.0, "ret20": 3.0,
         "mae5": -1.0, "mfe5": 2.0, "cooldown_count": 1},
        {"date": "2026-01-05", "ticker": "AAPL", "score": 75,
         "strategy": "speculative", "score_mode": "v8",
         "ret5": 1.5, "ret10": 2.5, "ret20": 3.5,
         "mae5": -1.0, "mfe5": 2.0, "cooldown_count": 1},
    ]
    report = _build_json_report(records, [80], ["AAPL"], "2026-01-01",
                                "2026-02-01", 4, raw_records=records,
                                version="v8", breakdown="strategy")
    assert "by_strategy" in report
    assert set(report["by_strategy"]) == {"quality", "speculative"}
    quality = {b["band"]: b for b in report["by_strategy"]["quality"]}
    speculative = {b["band"]: b for b in report["by_strategy"]["speculative"]}
    assert quality["70-74"]["signals"] == 1
    assert speculative["75-79"]["signals"] == 1


def test_json_report_no_by_strategy_by_default():
    report = _build_json_report([], [80], ["AAPL"], "2026-01-01", "2026-02-01", 4,
                                raw_records=[], version="v8")
    assert "by_strategy" not in report
    assert "by_risk" not in report


def _risk_records():
    """risk_level 필드가 있는 레코드 3건 (LOW 2건 + HIGH 1건)."""
    return [
        {"score": 70, "risk_level": "LOW", "ret5": 2.0, "ret10": 3.0, "ret20": 4.0,
         "mae5": -1.0, "mfe5": 3.0},
        {"score": 72, "risk_level": "LOW", "ret5": 1.5, "ret10": 2.5, "ret20": 3.5,
         "mae5": -1.0, "mfe5": 3.0},
        {"score": 71, "risk_level": "HIGH", "ret5": 1.0, "ret10": 2.0, "ret20": 3.0,
         "mae5": -2.0, "mfe5": 2.0},
    ]


def test_summarize_by_risk_groups_band_summaries():
    out = _summarize_by_risk(_risk_records())
    assert set(out) == {"LOW", "HIGH"}
    low = {b["band"]: b for b in out["LOW"]}
    high = {b["band"]: b for b in out["HIGH"]}
    assert low["70-74"]["signals"] == 2
    assert high["70-74"]["signals"] == 1


def test_json_report_by_risk_added_when_breakdown_risk():
    records = [
        {"date": "2026-01-05", "ticker": "AAPL", "score": 70,
         "risk_level": "LOW", "score_mode": "v8",
         "ret5": 1.0, "ret10": 2.0, "ret20": 3.0,
         "mae5": -1.0, "mfe5": 2.0, "cooldown_count": 1},
        {"date": "2026-01-05", "ticker": "AAPL", "score": 75,
         "risk_level": "HIGH", "score_mode": "v8",
         "ret5": 1.5, "ret10": 2.5, "ret20": 3.5,
         "mae5": -1.0, "mfe5": 2.0, "cooldown_count": 1},
    ]
    report = _build_json_report(records, [80], ["AAPL"], "2026-01-01",
                                "2026-02-01", 4, raw_records=records,
                                version="v8", breakdown="risk")
    assert "by_risk" in report
    assert set(report["by_risk"]) == {"LOW", "HIGH"}
    low = {b["band"]: b for b in report["by_risk"]["LOW"]}
    high = {b["band"]: b for b in report["by_risk"]["HIGH"]}
    assert low["70-74"]["signals"] == 1
    assert high["75-79"]["signals"] == 1


def test_json_report_breakdown_all_includes_strategy_and_risk():
    records = [
        {"date": "2026-01-05", "ticker": "AAPL", "score": 70,
         "strategy": "quality", "risk_level": "LOW", "score_mode": "v8",
         "ret5": 1.0, "ret10": 2.0, "ret20": 3.0,
         "mae5": -1.0, "mfe5": 2.0, "cooldown_count": 1},
    ]
    report = _build_json_report(records, [80], ["AAPL"], "2026-01-01",
                                "2026-02-01", 4, raw_records=records,
                                version="v8", breakdown="all")
    assert "by_strategy" in report
    assert "by_risk" in report


def test_backtest_ticker_records_include_risk_fields():
    df = _make_df()
    v8 = _last(_backtest("TEST", df, mode="v8", strategy="general"))
    assert "risk_score" in v8
    assert v8["risk_level"] in ("LOW", "MEDIUM", "HIGH", "VERY_HIGH", "UNKNOWN")
    assert 0 <= v8["signal_confidence"] <= 1.0
