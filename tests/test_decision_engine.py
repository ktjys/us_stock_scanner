"""V8 Decision Engine 테스트."""

import pytest

from decision_engine import Decision, make_decision


def _decide(opp, risk="LOW", conf=1.0, strategy="quality", class_conf=1.0):
    """make_decision 호출 헬퍼 (기본값은 모든 신뢰도 필터를 통과하는 설정)."""
    return make_decision(opp, risk, conf, strategy, class_conf)


# (strategy, strong, opportunity, watch, neutral) — V8 spec §7 임계값.
_STRATEGY_BOUNDARIES = [
    ("quality", 65, 55, 40, 25),
    ("established_growth", 65, 55, 40, 25),
    ("speculative", 70, 65, 50, 30),
    ("broad_market_etf", 60, 50, 35, 20),
    ("growth_etf", 60, 50, 35, 20),
    ("dividend_etf", 55, 45, 30, 15),
    ("income_etf", 55, 45, 30, 15),
    ("sector_etf", 60, 50, 35, 20),
    ("general", 55, 40, 25, 10),
    ("other_etf", 55, 40, 25, 10),
]


@pytest.mark.parametrize("strategy,strong,opportunity,watch,neutral", _STRATEGY_BOUNDARIES)
def test_strategy_boundaries(strategy, strong, opportunity, watch, neutral):
    # AVOID: neutral 미만
    assert _decide(neutral - 1, strategy=strategy) == Decision.AVOID
    # NEUTRAL: neutral 이상, watch 미만
    assert _decide(neutral, strategy=strategy) == Decision.NEUTRAL
    assert _decide(watch - 1, strategy=strategy) == Decision.NEUTRAL
    # WATCH: watch 이상, opportunity 미만
    assert _decide(watch, strategy=strategy) == Decision.WATCH
    assert _decide(opportunity - 1, strategy=strategy) == Decision.WATCH
    # OPPORTUNITY: opportunity 이상, strong 미만 (리스크 LOW, 신뢰도 충분)
    assert _decide(opportunity, strategy=strategy) == Decision.OPPORTUNITY
    assert _decide(strong - 1, strategy=strategy) == Decision.OPPORTUNITY
    # STRONG_OPPORTUNITY: strong 이상 + 리스크 LOW/MEDIUM + 신뢰도 충분
    assert _decide(strong, strategy=strategy) == Decision.STRONG_OPPORTUNITY


def test_very_high_risk_blocks_opportunity():
    # VERY_HIGH 리스크는 모든 전략에서 OPPORTUNITY 이상을 차단한다.
    for strategy, _, _, _, _ in _STRATEGY_BOUNDARIES:
        assert _decide(100, risk="VERY_HIGH", strategy=strategy) == Decision.WATCH


def test_high_risk_allows_opportunity_but_not_strong():
    # HIGH 리스크: OPPORTUNITY는 허용되지만 STRONG_OPPORTUNITY는 차단된다.
    assert _decide(80, risk="HIGH", strategy="quality") == Decision.OPPORTUNITY
    assert _decide(80, risk="HIGH", strategy="speculative") == Decision.OPPORTUNITY
    assert _decide(80, risk="HIGH", strategy="broad_market_etf") == Decision.OPPORTUNITY
    # general은 STRONG 조건이 risk≠VERY_HIGH이므로 HIGH에서도 STRONG이 허용된다.
    assert _decide(80, risk="HIGH", strategy="general") == Decision.STRONG_OPPORTUNITY


def test_medium_risk_allows_strong():
    assert _decide(80, risk="MEDIUM", strategy="quality") == Decision.STRONG_OPPORTUNITY
    assert _decide(80, risk="MEDIUM", strategy="general") == Decision.STRONG_OPPORTUNITY


def test_signal_confidence_below_min_caps_at_watch():
    # 신호 신뢰도 < 0.5면 OPPORTUNITY 이상 불가 (STRONG 조건이 없는 전략 포함).
    assert _decide(80, conf=0.49, strategy="quality") == Decision.WATCH
    assert _decide(80, conf=0.49, strategy="general") == Decision.WATCH
    assert _decide(80, conf=0.49, strategy="broad_market_etf") == Decision.WATCH
    # 이미 WATCH 이하인 판단은 유지된다.
    assert _decide(45, conf=0.49, strategy="quality") == Decision.WATCH
    assert _decide(10, conf=0.49, strategy="quality") == Decision.AVOID


def test_signal_confidence_at_min_not_capped():
    # 신호 신뢰도가 정확히 0.5면 캡이 적용되지 않는다.
    assert _decide(80, conf=0.5, strategy="general") == Decision.STRONG_OPPORTUNITY
    # quality는 STRONG에 0.7이 필요하므로 0.5에서는 OPPORTUNITY.
    assert _decide(80, conf=0.5, strategy="quality") == Decision.OPPORTUNITY


def test_classification_confidence_below_min_caps_at_watch():
    assert _decide(80, class_conf=0.39, strategy="quality") == Decision.WATCH
    assert _decide(80, class_conf=0.39, strategy="general") == Decision.WATCH
    assert _decide(45, class_conf=0.39, strategy="quality") == Decision.WATCH
    assert _decide(10, class_conf=0.39, strategy="quality") == Decision.AVOID


def test_classification_confidence_at_min_not_capped():
    assert _decide(80, class_conf=0.4, strategy="quality") == Decision.STRONG_OPPORTUNITY


def test_strong_confidence_requirement():
    # quality/established_growth: STRONG에 conf ≥ 0.7 필요.
    assert _decide(80, conf=0.69, strategy="quality") == Decision.OPPORTUNITY
    assert _decide(80, conf=0.7, strategy="quality") == Decision.STRONG_OPPORTUNITY
    assert _decide(80, conf=0.69, strategy="established_growth") == Decision.OPPORTUNITY
    assert _decide(80, conf=0.7, strategy="established_growth") == Decision.STRONG_OPPORTUNITY
    # speculative: STRONG에 conf ≥ 0.8 필요.
    assert _decide(80, conf=0.79, strategy="speculative") == Decision.OPPORTUNITY
    assert _decide(80, conf=0.8, strategy="speculative") == Decision.STRONG_OPPORTUNITY
    # ETF 전략: STRONG에 신뢰도 요구 없음 (0.5 이상이면 충분).
    assert _decide(80, conf=0.5, strategy="broad_market_etf") == Decision.STRONG_OPPORTUNITY


def test_unknown_strategy_falls_back_to_general():
    assert _decide(60, strategy="unknown") == Decision.STRONG_OPPORTUNITY
    assert _decide(30, strategy="unknown") == Decision.WATCH
    assert _decide(5, strategy="unknown") == Decision.AVOID


def test_decision_constants_are_distinct():
    grades = {Decision.STRONG_OPPORTUNITY, Decision.OPPORTUNITY, Decision.WATCH,
              Decision.NEUTRAL, Decision.AVOID}
    assert len(grades) == 5