"""V8 Opportunity Engine 테스트."""

import math

import pandas as pd
import pytest

from opportunity_engine import (COMPONENT_MAXES, FUND_COMPONENTS,
                                STRATEGY_WEIGHTS, TECH_COMPONENTS,
                                component_sub_scores,
                                compute_fundamental_components,
                                compute_technical_components,
                                opportunity_score, risk_score,
                                signal_confidence)
from stock_scanner import _relative_strength_series, rsi, score_signal


# ---------------------------------------------------------------------------
# 합성 데이터 헬퍼
# ---------------------------------------------------------------------------

def _indicator_df(closes, highs=None, volumes=None):
    """지표 컬럼(rsi/ma20/ma50/high60/avgvol)이 계산된 프레임을 만든다."""
    n = len(closes)
    highs = highs if highs is not None else [c * 1.005 for c in closes]
    volumes = volumes if volumes is not None else [1_000_000] * n
    df = pd.DataFrame({"Close": closes, "High": highs, "Volume": volumes})
    df["rsi"] = rsi(df["Close"])
    df["ma20"] = df["Close"].rolling(20).mean()
    df["ma50"] = df["Close"].rolling(50).mean()
    df["high60"] = df["High"].rolling(60).max()
    df["avgvol"] = df["Volume"].rolling(20).mean()
    return df.dropna().reset_index(drop=True)


def _linspace(a, b, n):
    return list(pd.Series(range(n)) * ((b - a) / max(n - 1, 1)) + a)


def _oscillate(level, half, n, start_high=True):
    """level ± half로 교대하는 n개 종가 시리즈 (마지막 종가는 level±half)."""
    return [level * (1 + half) if (k % 2 == 0) == start_high else level * (1 - half)
            for k in range(n)]


def _volumes_series(base, last_ratio, n):
    """마지막 행만 base*last_ratio, 나머지는 base (avgvol을 낮춰 vr를 키운다)."""
    return [base] * (n - 1) + [base * last_ratio]


def _flat_market(n):
    """종목 5일 수익률이 그대로 상대강도가 되도록 하는 평탄한 시장."""
    return pd.DataFrame({"Close": [100.0] * n})


# ---------------------------------------------------------------------------
# 1) general 전략 == V7 점수
# ---------------------------------------------------------------------------

def test_general_equals_v7_score():
    # 상승 추세 → 눌림 → 반등 패턴 (300행)
    closes = _linspace(100.0, 150.0, 201)          # 0..200: 상승
    closes += _linspace(150.0, 132.0, 90)          # 201..290: 눌림
    closes += [131.5] * 8                          # 291..298: 눌림 바닥
    closes += [141.0]                              # 299: 반등 (+7.2%)
    df = _indicator_df(closes)
    i = len(df) - 1

    components = compute_technical_components(df, i)
    assert components is not None
    assert set(components) == set(TECH_COMPONENTS)

    # V7 8개 컴포넌트 합과 score_signal 직접 호출 결과가 일치해야 한다.
    v7_sum = sum(components[k] for k in
                 ["rsi_state", "rsi_rebound", "price_rebound", "drawdown",
                  "ma20", "trend", "relative_strength", "volume"])

    row, prow, p2 = df.iloc[i], df.iloc[i - 1], df.iloc[i - 2]
    score, cond, details = score_signal(
        float(row["Close"]), float(row["rsi"]), float(prow["rsi"]),
        float(row["ma20"]), float(row["ma50"]),
        (float(row["Close"]) / float(row["high60"]) - 1) * 100,
        float(row["Volume"]) / float(row["avgvol"]),
        ma50_prev=float(prow["ma50"]), prev_price=float(prow["Close"]),
        ma20_prev=float(prow["ma20"]), prev2_rsi=float(p2["rsi"]),
    )
    del cond
    assert v7_sum == score
    for k in ["rsi_state", "rsi_rebound", "price_rebound", "drawdown",
              "ma20", "trend", "relative_strength", "volume"]:
        assert components[k] == details[k]

    # general 전략은 8개 V7 컴포넌트를 동일 가중치로 재정규화 → V7 점수와 동일
    assert opportunity_score(components, "general") == score
    assert 0 <= score <= 100


# ---------------------------------------------------------------------------
# 2) 모든 전략의 opportunity_score가 0~100 int
# ---------------------------------------------------------------------------

def test_all_strategies_score_in_bounds():
    zero = {k: 0 for k in TECH_COMPONENTS + FUND_COMPONENTS}
    full = {k: COMPONENT_MAXES[k] for k in TECH_COMPONENTS + FUND_COMPONENTS}
    half = {k: COMPONENT_MAXES[k] // 2 for k in TECH_COMPONENTS + FUND_COMPONENTS}

    for strategy in STRATEGY_WEIGHTS:
        s0 = opportunity_score(zero, strategy)
        s100 = opportunity_score(full, strategy)
        s_half = opportunity_score(half, strategy)
        assert s0 == 0
        assert s100 == 100
        assert isinstance(s_half, int)
        assert 0 <= s_half <= 100


# ---------------------------------------------------------------------------
# 3) 펀더멘털 키 없이 기술 컴포넌트만으로 재정규화
# ---------------------------------------------------------------------------

def test_renormalizes_without_fundamentals():
    tech_full = {k: COMPONENT_MAXES[k] for k in TECH_COMPONENTS}
    assert opportunity_score(tech_full, "quality") == 100


# ---------------------------------------------------------------------------
# 4) momentum_20d 티어
# ---------------------------------------------------------------------------

def test_momentum_20d_tiers():
    # 마지막 21행이 100 → 109 (+9%) 로 상승 → momentum_20d == 10
    # (앞부분은 RSI가 NaN이 되지 않도록 완만한 상승으로 유지)
    closes = _linspace(80.0, 100.0, 279) + _linspace(100.0, 109.0, 21)
    df = _indicator_df(closes)
    components = compute_technical_components(df, len(df) - 1)
    assert components is not None
    assert components["momentum_20d"] == 10


# ---------------------------------------------------------------------------
# 5) breakout 티어
# ---------------------------------------------------------------------------

def test_breakout_tiers():
    # 하락 후 60일 고점에 근접한 반등 → high60 대비 -2% 이내 → breakout == 10
    closes = _linspace(150.0, 100.0, 241)      # 0..240: 하락
    closes += _linspace(100.0, 110.0, 59)      # 241..299: 반등 (고점 근접)
    df = _indicator_df(closes)
    components = compute_technical_components(df, len(df) - 1)
    assert components is not None
    bdd = float(df.iloc[-1]["Close"]) / float(df.iloc[-1]["high60"]) - 1
    assert bdd >= -0.02
    assert components["breakout"] == 10


# ---------------------------------------------------------------------------
# 6) 펀더멘털 컴포넌트 티어
# ---------------------------------------------------------------------------

def test_fundamental_components_tiers():
    # valuation 티어
    assert compute_fundamental_components({"trailingPE": 15})["valuation"] == 10
    assert compute_fundamental_components({"trailingPE": 30})["valuation"] == 7
    assert compute_fundamental_components({"trailingPE": 50})["valuation"] == 4
    assert compute_fundamental_components({"trailingPE": 100})["valuation"] == 2
    assert compute_fundamental_components({"trailingPE": -5})["valuation"] == 1
    # P/S >= 20이면 valuation 0 (PER이 좋아도 과대평가 우선)
    assert compute_fundamental_components(
        {"trailingPE": 15, "priceToSalesTrailing12Months": 25})["valuation"] == 0
    # float 변환 실패 → 0
    assert "valuation" not in compute_fundamental_components({"trailingPE": "abc"})

    # profitability 티어
    assert compute_fundamental_components({"profitMargins": 0.15})["profitability"] == 10
    assert compute_fundamental_components({"profitMargins": 0.07})["profitability"] == 7
    assert compute_fundamental_components({"profitMargins": 0.02})["profitability"] == 4
    assert compute_fundamental_components({"profitMargins": -0.1})["profitability"] == 0

    # dividend 티어 (Yahoo dividendYield는 비율 값)
    assert compute_fundamental_components({"dividendYield": 0.03})["dividend"] == 10
    assert compute_fundamental_components({"dividendYield": 0.025})["dividend"] == 7
    assert compute_fundamental_components({"dividendYield": 0.015})["dividend"] == 4
    assert compute_fundamental_components({"dividendYield": 0.005})["dividend"] == 0

    # earnings 티어
    assert compute_fundamental_components({"earningsGrowth": 0.20})["earnings"] == 10
    assert compute_fundamental_components({"earningsGrowth": 0.15})["earnings"] == 7
    assert compute_fundamental_components({"earningsGrowth": 0.05})["earnings"] == 4
    assert compute_fundamental_components({"earningsGrowth": -0.1})["earnings"] == 0

    # info=None → 데이터 없음 (no data, not 0 points)
    assert compute_fundamental_components(None) == {}


# ---------------------------------------------------------------------------
# 6-1) quality_callback: 핵심 펀더멘털 필드 결측 감지
# ---------------------------------------------------------------------------

def test_fundamental_components_quality_callback_fires_on_missing_key_fields():
    calls = []

    def cb(missing, comps):
        calls.append((missing, comps))

    compute_fundamental_components({"dividendYield": 0.03}, quality_callback=cb)
    assert len(calls) == 1
    missing, comps = calls[0]
    assert missing == ["trailingPE", "profitMargins", "earningsGrowth"]
    assert comps == {"dividend": 10}


def test_fundamental_components_quality_callback_silent_when_all_fields_present():
    calls = []

    def cb(missing, comps):
        calls.append((missing, comps))

    compute_fundamental_components(
        {"trailingPE": 15, "profitMargins": 0.15, "dividendYield": 0.03,
         "earningsGrowth": 0.2},
        quality_callback=cb)
    assert calls == []


def test_fundamental_components_quality_callback_silent_when_values_bad_not_missing():
    """필드가 존재하지만 값이 나쁜 경우(0점)는 '결측'이 아니므로 호출하지 않는다."""
    calls = []

    def cb(missing, comps):
        calls.append((missing, comps))

    compute_fundamental_components(
        {"trailingPE": 100, "priceToSalesTrailing12Months": 25,
         "profitMargins": -0.1, "dividendYield": 0.03, "earningsGrowth": 0.2},
        quality_callback=cb)
    assert calls == []


def test_fundamental_components_quality_callback_silent_when_info_none():
    """info=None은 호출자(fundamental_null 로깅)가 처리하므로 콜백을 부르지 않는다."""
    calls = []
    compute_fundamental_components(None, quality_callback=calls.append)
    assert calls == []


# ---------------------------------------------------------------------------
# 7) speculative: 높은 기회 점수 + 높은 리스크 (좋은 기회 ≠ 안전한 투자)
# ---------------------------------------------------------------------------

def test_speculative_high_opportunity_high_risk():
    # 강한 상승 추세: 20일 +8.5%, 고점 근접(돌파), 거래량 3배, QQQ 대비 강함.
    # 마지막 20행은 급등 → 고변동 진동 → 눌림 후 돌파 반등 패턴 (동일 df로 리스크도 평가).
    # 주의: 이 시나리오는 momentum/breakout 추격형이므로 2026-08 가중치 조정
    # (추격 3→2, 되돌림 2→3) 후에는 65점 미만이 정상이다 — 과대평가 완화가 의도.
    closes = _linspace(200.0, 100.0, 241)           # 0..240: 하락 (고점 기준선)
    closes += _linspace(100.0, 102.3, 39)           # 241..279: 완만 상승
    closes += [102.3, 104.5, 106.5, 108.5]          # 280..283: 급등
    osc = [108.5 * (1 - 0.02) if k % 2 == 0 else 108.5 * (1 + 0.02)
           for k in range(13)]                       # 284..296: 고변동 진동
    closes += osc
    closes += [108.5, 108.5, 111.0]                 # 297..299: 돌파 반등
    volumes = _volumes_series(1_000_000, 3.0, len(closes))
    df = _indicator_df(closes, volumes=volumes)
    market_df = _flat_market(len(df))

    i = len(df) - 1
    components = compute_technical_components(df, i, market_df)
    assert components is not None

    ret20 = float(df.iloc[i]["Close"]) / float(df.iloc[i - 20]["Close"]) - 1
    rs5 = float(_relative_strength_series(df, market_df).iloc[i])
    vr = float(df.iloc[i]["Volume"]) / float(df.iloc[i]["avgvol"])
    bdd = float(df.iloc[i]["Close"]) / float(df.iloc[i]["high60"]) - 1
    assert ret20 >= 0.08
    assert rs5 >= 2.0
    assert vr >= 2.0
    assert bdd >= -0.02

    score = opportunity_score(components, "speculative")
    assert 60 <= score < 65

    info = {"beta": 2.5, "profitMargins": -0.2, "trailingPE": 120}
    risk, level = risk_score(df, i, info)
    assert risk >= 55
    assert level in ("HIGH", "VERY_HIGH")


# ---------------------------------------------------------------------------
# 8) risk_score 등급 버킷
# ---------------------------------------------------------------------------

def _risk_scenario(vol_level, half, peak_close, last_close, vol_ratio,
                   beta, pm, pe):
    """지정 패턴의 df를 만들고 risk_score 결과를 돌려준다."""
    n = 300
    closes = _linspace(90.0, peak_close, 190)            # 0..189: 상승
    closes += [peak_close] * 20                          # 190..209: 고점 유지
    closes += _linspace(peak_close, last_close, 30)      # 210..239: 눌림
    closes += _oscillate(last_close, half, 20)           # 240..259: 고변동 진동
    volumes = _volumes_series(1_000_000, vol_ratio, len(closes))
    df = _indicator_df(closes, volumes=volumes)
    info = {"beta": beta, "profitMargins": pm, "trailingPE": pe}
    return risk_score(df, len(df) - 1, info)


def test_risk_level_buckets():
    # LOW: 저변동 + 저beta + 고점 근접 + 정상 거래량 + 정상 펀더멘털
    score_low, level_low = _risk_scenario(
        vol_level=None, half=0.003, peak_close=100.0, last_close=99.5,
        vol_ratio=1.0, beta=0.5, pm=0.3, pe=15)
    assert 0 <= score_low <= 34
    assert level_low == "LOW"

    # MEDIUM: 중변동 + 중beta + 눌림 + 거래량 2.5배 + 음수 수익성
    score_med, level_med = _risk_scenario(
        vol_level=None, half=0.009, peak_close=110.0, last_close=99.0,
        vol_ratio=2.5, beta=1.5, pm=-0.1, pe=30)
    assert 35 <= score_med <= 54
    assert level_med == "MEDIUM"

    # HIGH: 고변동 + 고beta + 깊은 눌림 + 거래량 3배 이상 + 과대평가
    score_high, level_high = _risk_scenario(
        vol_level=None, half=0.015, peak_close=110.0, last_close=88.0,
        vol_ratio=4.0, beta=2.0, pm=0.1, pe=90)
    assert 55 <= score_high <= 74
    assert level_high == "HIGH"

    # VERY_HIGH: 극단 변동 + 초고beta + 극단 눌림 + 이상 거래량 + 음수 수익성·과대평가
    score_vh, level_vh = _risk_scenario(
        vol_level=None, half=0.025, peak_close=110.0, last_close=66.0,
        vol_ratio=4.0, beta=3.0, pm=-0.2, pe=120)
    assert 75 <= score_vh <= 100
    assert level_vh == "VERY_HIGH"


# ---------------------------------------------------------------------------
# 9) signal_confidence 범위
# ---------------------------------------------------------------------------

def test_signal_confidence_range():
    assert signal_confidence(0) == pytest.approx(0.4)
    assert signal_confidence(100) == pytest.approx(1.0)
    assert signal_confidence(50) == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# 10) component_sub_scores 4축 집계
# ---------------------------------------------------------------------------

def test_sub_scores_full_components_all_100():
    comps = {k: COMPONENT_MAXES[k] for k in TECH_COMPONENTS + FUND_COMPONENTS}
    subs = component_sub_scores(comps)
    assert subs == {"technical_score": 100, "momentum_score": 100,
                    "fundamental_score": 100, "valuation_score": 100}


def test_sub_scores_tech_only_leaves_fundamental_none():
    # 백테스트처럼 펀더멘털이 없으면 기술/모멘텀만 계산되고 펀더멘털/밸류는 None
    comps = {k: COMPONENT_MAXES[k] for k in TECH_COMPONENTS}
    subs = component_sub_scores(comps)
    assert subs["technical_score"] == 100
    assert subs["momentum_score"] == 100
    assert subs["fundamental_score"] is None
    assert subs["valuation_score"] is None


def test_sub_scores_axis_renormalization():
    # relative_strength만 있는 모멘텀 축: 그 컴포넌트 만점 대비 100%
    subs = component_sub_scores({"relative_strength": 10})
    assert subs["momentum_score"] == 100
    assert subs["technical_score"] is None
    assert subs["fundamental_score"] is None
    assert subs["valuation_score"] is None


def test_sub_scores_half_scores_round_to_50():
    # 각 축의 전체 컴포넌트를 채워, 각 축 점수 합이 정확히 만점 합의 절반이
    # 되도록 명시한다 (존재 키만 분모로 쓰는 재정규화 수식 검증).
    comps = {
        # technical (만점 합 90): 45/90 = 50
        "rsi_state": 10, "rsi_rebound": 7, "price_rebound": 7,
        "drawdown": 7, "ma20": 7, "trend": 2, "volume": 5,
        # momentum (만점 합 30): 15/30 = 50
        "relative_strength": 5, "momentum_20d": 5, "breakout": 5,
        # fundamental (만점 합 30): 15/30 = 50
        "profitability": 5, "earnings": 5, "dividend": 5,
        # valuation (만점 합 10): 5/10 = 50
        "valuation": 5,
    }
    subs = component_sub_scores(comps)
    assert subs["technical_score"] == 50
    assert subs["momentum_score"] == 50
    assert subs["fundamental_score"] == 50
    assert subs["valuation_score"] == 50


def test_component_correlation():
    """Verify technical and momentum components are independently weighted.

    Technical components (rsi_state..volume) and momentum components
    (relative_strength, momentum_20d, breakout) are weighted separately
    by STRATEGY_WEIGHTS. When only technical components are present,
    technical_score should be computed and momentum_score should be None.
    """
    # Only technical components present (no momentum components)
    comps = {
        "rsi_state": 20, "rsi_rebound": 15, "price_rebound": 15, "drawdown": 15,
        "ma20": 15, "trend": 5, "volume": 5,
    }
    subs = component_sub_scores(comps)
    # Technical score: all 7 tech components present, maxed out
    assert subs["technical_score"] == 100
    # No momentum components → None
    assert subs["momentum_score"] is None

    # Only momentum components present (no technical)
    comps2 = {
        "relative_strength": 10, "momentum_20d": 10, "breakout": 10,
    }
    subs2 = component_sub_scores(comps2)
    # No technical components → None
    assert subs2["technical_score"] is None
    # Momentum score: all 3 present at max
    assert subs2["momentum_score"] == 100
