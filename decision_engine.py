"""V8 의사결정(Decision) 엔진.

Opportunity Score + Risk Level + Confidence를 조합해 전략(strategy_type)별
5등급 행동 판단을 만든다 (V8 spec §7).

- 기회 점수와 리스크는 독립 차원이다. 같은 기회 점수라도 전략이 다르면
  판단이 달라질 수 있다 (예: speculative는 OPPORTUNITY 임계값이 더 높다).
- STRONG_OPPORTUNITY는 전략별로 리스크/신뢰도 조건이 추가로 요구된다.
- 신호 신뢰도 < 0.5 또는 분류 신뢰도 < 0.4면 어떤 전략에서도 최대 WATCH로
  제한된다 (확신이 낮은 높은 점수는 기회로 취급하지 않는다).
"""

from __future__ import annotations

from typing import TypedDict


class Decision:
    """5등급 행동 판단 상수."""

    STRONG_OPPORTUNITY = "STRONG_OPPORTUNITY"
    OPPORTUNITY = "OPPORTUNITY"
    WATCH = "WATCH"
    NEUTRAL = "NEUTRAL"
    AVOID = "AVOID"


class _StrategyRule(TypedDict):
    """전략별 임계값 (V8 spec §7).

    strong/opportunity/watch/neutral: 각 등급의 기회 점수 하한.
    strong_conf: STRONG_OPPORTUNITY에 요구되는 signal_confidence (None이면 요구 없음).
    strong_risk: STRONG_OPPORTUNITY에 허용되는 risk_level 집합
                 (None이면 VERY_HIGH 차단 외 추가 제약 없음).
    """

    strong: int
    opportunity: int
    watch: int
    neutral: int
    strong_conf: float | None
    strong_risk: tuple[str, ...] | None


_STRATEGY_RULES: dict[str, _StrategyRule] = {
    "quality":            {"strong": 65, "opportunity": 55, "watch": 40, "neutral": 25,
                           "strong_conf": 0.7, "strong_risk": ("LOW", "MEDIUM")},
    "established_growth": {"strong": 65, "opportunity": 55, "watch": 40, "neutral": 25,
                           "strong_conf": 0.7, "strong_risk": ("LOW", "MEDIUM")},
    "speculative":        {"strong": 70, "opportunity": 65, "watch": 50, "neutral": 30,
                           "strong_conf": 0.8, "strong_risk": ("LOW", "MEDIUM")},
    "broad_market_etf":   {"strong": 60, "opportunity": 50, "watch": 35, "neutral": 20,
                           "strong_conf": None, "strong_risk": ("LOW", "MEDIUM")},
    "growth_etf":         {"strong": 60, "opportunity": 50, "watch": 35, "neutral": 20,
                           "strong_conf": None, "strong_risk": ("LOW", "MEDIUM")},
    "dividend_etf":       {"strong": 55, "opportunity": 45, "watch": 30, "neutral": 15,
                           "strong_conf": None, "strong_risk": ("LOW", "MEDIUM")},
    "income_etf":         {"strong": 55, "opportunity": 45, "watch": 30, "neutral": 15,
                           "strong_conf": None, "strong_risk": ("LOW", "MEDIUM")},
    "sector_etf":         {"strong": 60, "opportunity": 50, "watch": 35, "neutral": 20,
                           "strong_conf": None, "strong_risk": ("LOW", "MEDIUM")},
    "general":            {"strong": 55, "opportunity": 40, "watch": 25, "neutral": 10,
                           "strong_conf": None, "strong_risk": None},
    "other_etf":          {"strong": 55, "opportunity": 40, "watch": 25, "neutral": 10,
                           "strong_conf": None, "strong_risk": None},
}

SIGNAL_CONFIDENCE_MIN = 0.5
CLASSIFICATION_CONFIDENCE_MIN = 0.4


def make_decision(opportunity_score: int, risk_level: str, signal_confidence: float,
                  strategy_type: str, classification_confidence: float) -> str:
    """전략별 5등급 행동 판단을 반환한다.

    opportunity_score(0~100)와 risk_level(LOW/MEDIUM/HIGH/VERY_HIGH)로
    전략 임계값 표(V8 spec §7)에 따라 기본 등급을 정한 뒤 신뢰도 필터를 적용한다.

    - signal_confidence < 0.5 또는 classification_confidence < 0.4면
      최대 WATCH로 제한된다 (OPPORTUNITY 이상 불가).
    - 미지의 strategy_type은 general 규칙으로 폴백한다 (opportunity_engine과 동일).
    """
    rules = _STRATEGY_RULES.get(strategy_type, _STRATEGY_RULES["general"])

    if opportunity_score < rules["neutral"]:
        decision = Decision.AVOID
    elif opportunity_score < rules["watch"]:
        decision = Decision.NEUTRAL
    elif opportunity_score < rules["opportunity"]:
        decision = Decision.WATCH
    elif risk_level == "VERY_HIGH":
        decision = Decision.WATCH
    elif (opportunity_score >= rules["strong"]
          and (rules["strong_risk"] is None or risk_level in rules["strong_risk"])
          and (rules["strong_conf"] is None
               or signal_confidence >= rules["strong_conf"])):
        decision = Decision.STRONG_OPPORTUNITY
    else:
        decision = Decision.OPPORTUNITY

    if (signal_confidence < SIGNAL_CONFIDENCE_MIN
            or classification_confidence < CLASSIFICATION_CONFIDENCE_MIN):
        if decision in (Decision.STRONG_OPPORTUNITY, Decision.OPPORTUNITY):
            decision = Decision.WATCH

    return decision