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
# V7 스코어링
# ---------------------------------------------------------------------------


def test_rsi_tiers_are_exclusive():
    # RSI 37 → 15점 + 얕은눌림(-2%) → 4점 = 19점 (V7 배점)
    score, cond, details = score_signal(100.0, 37.0, 37.0, 110.0, 120.0, -2.0, 1.0)
    assert score == 25
    assert cond == ["RSI30~40", "얕은눌림"]
    assert details["rsi_state"] == 20
    assert details["drawdown"] == 5


def test_rsi_rebound_is_stronger_with_larger_delta():
    weak, _, _ = score_signal(100, 31, 30, 110, 120, -2, 1, prev2_rsi=29)
    strong, _, _ = score_signal(100, 35, 29, 110, 120, -2, 1, prev2_rsi=28)
    assert strong > weak


def test_deep_crash_is_not_maximally_rewarded():
    moderate, _, _ = score_signal(100, 32, 30, 110, 120, -15, 1)
    crash, _, _ = score_signal(100, 32, 30, 110, 120, -35, 1)
    assert moderate > crash


def test_uptrend_adds_more_points_than_broken_trend():
    healthy, _, _ = score_signal(
        100, 32, 30, 100, 95, -12, 1.5, ma50_prev=94, prev_price=98, prev2_rsi=29
    )
    broken, _, _ = score_signal(
        80, 32, 30, 90, 100, -12, 1.5, ma50_prev=101, prev_price=82, prev2_rsi=29
    )
    assert healthy > broken


def test_full_combo_v7_max_score():
    # RSI20 + RSI반등15 + 가격반등15 + 적정눌림15 + MA20회복15
    # + 상승추세5 + QQQ대비10 + 반등+거래량5 = 100
    score, cond, details = score_signal(
        100.0, 32.0, 26.0, 99.0, 90.0, -15.0, 1.7,
        ma50_prev=89.0, prev_price=98.0, ma20_prev=101.0,
        relative_strength_5d=3.0, prev2_rsi=25.0
    )
    assert score == 100
    assert len(cond) == 8
    assert sum(details.values()) == 100


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
