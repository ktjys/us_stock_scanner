"""스코어링 / RSI / 알림 쿨다운 로직 테스트."""

import pandas as pd
import pytest

from stock_scanner import (compute_signal, filter_recent_alerts,
                           recent_alert_tickers, rsi, score_signal)


def _df(closes, highs=None, volumes=None):
    n = len(closes)
    highs = highs if highs is not None else [c * 1.02 for c in closes]
    volumes = volumes if volumes is not None else [1_000_000] * n
    return pd.DataFrame({"Close": closes, "High": highs, "Volume": volumes})


# ---------------------------------------------------------------------------
# RSI (Wilder 정확 구현)
# ---------------------------------------------------------------------------


def test_rsi_rising_series_is_100():
    s = pd.Series(range(1, 261), dtype=float)
    assert rsi(s).iloc[-1] == pytest.approx(100.0)


def test_rsi_falling_series_is_0():
    s = pd.Series(range(260, 0, -1), dtype=float)
    assert rsi(s).iloc[-1] == pytest.approx(0.0)


def test_rsi_alternating_converges_to_50():
    # ±1 교대 시리즈는 수렴 후에도 날짜 홀짝에 따라 48.15 ↔ 51.85로 진동한다.
    prices = [100.0] + [100.0 + (1 if i % 2 else -1) for i in range(1, 301)]
    assert rsi(pd.Series(prices)).iloc[-1] == pytest.approx(50.0, abs=2.0)


def test_rsi_seed_matches_hand_computed_wilder():
    # period=3, 수작업 계산과 일치해야 함
    prices = [10.0, 11.0, 12.0, 13.0, 14.0, 13.0, 15.0, 12.0, 16.0, 17.0]
    out = rsi(pd.Series(prices), period=3)
    # 시드 이전(0..2)은 무효
    assert out.iloc[:3].isna().all()
    # 시드 행(index 3): 첫 3개 delta(+1,+1,+1)의 단순평균 → loss 0 → RSI 100
    assert out.iloc[3] == pytest.approx(100.0)
    # 재귀 평활 검증 (수작업 계산값)
    assert out.iloc[5] == pytest.approx(100 - 100 / 3, abs=1e-9)          # 66.667
    assert out.iloc[7] == pytest.approx(2000 / 51, abs=1e-9)              # 39.216
    assert out.iloc[9] == pytest.approx(37700 / 501, abs=1e-9)            # 75.250


# ---------------------------------------------------------------------------
# 스코어링
# ---------------------------------------------------------------------------


def test_rsi_tiers_are_exclusive():
    # 35 <= rv < 40 → +10만 가산 (RSI<35의 +20과 중복 불가)
    score, cond = score_signal(100.0, 37.0, 40.0, 110.0, 120.0, -5.0, 1.0)
    assert score == 10
    assert cond == ["RSI<40 과매도"]


def test_rsi_deep_oversold_tier():
    score, cond = score_signal(100.0, 30.0, 35.0, 110.0, 120.0, -5.0, 1.0)
    assert score == 20
    assert "RSI<35 과매도" in cond
    assert "RSI<40 과매도" not in cond


def test_no_rsi_points_when_above_40():
    score, cond = score_signal(100.0, 45.0, 50.0, 110.0, 120.0, -5.0, 1.0)
    assert score == 0
    assert cond == []


def test_rebound_alone():
    score, cond = score_signal(100.0, 50.0, 40.0, 110.0, 120.0, -5.0, 1.0)
    assert score == 15
    assert cond == ["RSI반등"]


def test_full_combo_max_score():
    # 과매도 20 + 반등 15 + 고점대비 20 + 20일선 15 + 50일선 10 + 거래량 10 = 90
    score, cond = score_signal(100.0, 30.0, 25.0, 98.0, 90.0, -15.0, 2.0)
    assert score == 90
    assert len(cond) == 6


# ---------------------------------------------------------------------------
# compute_signal
# ---------------------------------------------------------------------------


def test_compute_signal_returns_none_on_empty():
    assert compute_signal("AAPL", pd.DataFrame()) is None


def test_compute_signal_structure_and_bounds():
    closes = [100 * (1 + 0.001 * i) for i in range(300)]
    crash = [closes[-1] * (1 - 0.06 * (i + 1)) for i in range(5)]
    result = compute_signal("TEST", _df(closes[:-5] + crash))
    assert result is not None
    assert result["ticker"] == "TEST"
    assert 0 <= result["score"] <= 100
    assert {"price", "rsi", "prev_rsi", "ma20", "ma50",
            "drawdown", "volume_ratio", "score", "conditions"} <= set(result)
    assert isinstance(result["conditions"], list)


def test_compute_signal_requires_2_valid_rows():
    # 데이터가 너무 적으면 None (dropna 후 2행 미만)
    closes = [100.0, 101.0, 102.0]
    result = compute_signal("TEST", _df(closes))
    assert result is None


# ---------------------------------------------------------------------------
# 알림 쿨다운
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *cols):
        return self

    def in_(self, col, vals):
        return self

    def gte(self, col, val):
        return self

    def lt(self, col, val):
        return self

    def execute(self):
        return _FakeResponse(self._rows)


class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return _FakeQuery(self._rows)


def test_recent_alert_tickers_returns_alerted_set():
    db = _FakeDb([{"ticker": "AAPL"}, {"ticker": "NVDA"}])
    recent = recent_alert_tickers(db, "2026-08-13", ["AAPL", "MSFT", "NVDA"])
    assert recent == {"AAPL", "NVDA"}


def test_recent_alert_tickers_empty_input():
    assert recent_alert_tickers(_FakeDb([]), "2026-08-13", []) == set()


def test_filter_recent_alerts_drops_recent_tickers():
    cands = [{"ticker": "AAPL", "score": 70}, {"ticker": "MSFT", "score": 66}]
    out = filter_recent_alerts(cands, {"AAPL"})
    assert [c["ticker"] for c in out] == ["MSFT"]
