"""신호 생성/쿨다운/DB 저장/알림 준비를 통합하는 SignalEngine.

stock_scanner.py에 흩어져 있던 신호 생성(save_signal 호출), 쿨다운
(recent_alert_tickers/filter_recent_alerts), 알림(build_alert_message) 로직을
하나의 클래스로 묶는다. scan() 통합은 별도 작업으로 진행된다.

순환 임포트 방지: stock_scanner가 이 모듈을 모듈 레벨에서 임포트할 예정이므로
(opportunity_engine.py와 동일한 상황) stock_scanner의 함수/상수는 반드시
메서드 내부에서만 임포트한다.
"""

from __future__ import annotations

from typing import Any

from decision_engine import Decision

# Decision 우선순위 (높을수록 좋음)
_DECISION_RANK: dict[str, int] = {
    Decision.STRONG_OPPORTUNITY: 3,
    Decision.OPPORTUNITY: 2,
    Decision.WATCH: 1,
    Decision.NEUTRAL: 0,
    Decision.AVOID: -1,
}

# 점수 상승 임계값: 이만큼 상승하면 재알림 허용
SCORE_IMPROVEMENT_THRESHOLD = 15

# 신호 생성이 허용되는 Decision 등급 (OPPORTUNITY 이상)
_SIGNAL_DECISIONS = (Decision.OPPORTUNITY, Decision.STRONG_OPPORTUNITY)


class SignalEngine:
    """평가 결과 → 신호 생성/쿨다운 → DB 저장 → 알림 메시지 통합 엔진.

    db: Supabase 클라이언트. None이면 신호 저장을 생략한다 (분석 전용 모드).
    """

    def __init__(self, db: Any | None = None) -> None:
        self.db = db

    def should_generate_signal(
        self,
        evaluation: dict,
        previous_signal: dict | None,
    ) -> tuple[bool, str]:
        """쿨다운 규칙에 따라 신호 생성 여부와 사유를 반환한다.

        decision이 OPPORTUNITY 미만이면 신호를 만들지 않는다.
        쿨다운(ALERT_COOLDOWN_DAYS) 내 신호가 있어도 Decision 상승 또는
        점수가 SCORE_IMPROVEMENT_THRESHOLD 이상 상승하면 재알림한다.
        """
        decision = evaluation["decision"]
        opportunity = evaluation["opportunity_score"]

        # Decision 게이트
        if decision not in _SIGNAL_DECISIONS:
            return False, "decision_below_threshold"

        # 재알림 확인 (Decision 상승 또는 점수 +15)
        if previous_signal:
            prev_decision = previous_signal.get("decision", Decision.WATCH)
            prev_score = previous_signal.get(
                "opportunity_score", previous_signal.get("score", 0))

            if _DECISION_RANK.get(decision, 0) > _DECISION_RANK.get(prev_decision, 0):
                return True, "decision_improved"

            if opportunity - prev_score >= SCORE_IMPROVEMENT_THRESHOLD:
                return True, "score_improved"

            return False, "no_improvement"

        return True, "new_signal"

    def generate_signal(self, evaluation: dict, as_of_date: str) -> dict:
        """평가 결과를 signals 테이블 스키마의 dict로 변환한다.

        db가 설정되어 있으면 signals 테이블에 upsert한다
        (on_conflict="signal_date,ticker", save_signal과 동일 스키마).
        """
        from stock_scanner import SCORE_VERSION

        signal = {
            "signal_date": as_of_date,
            "ticker": evaluation["ticker"],
            "signal_price": evaluation["price"],
            "score": evaluation.get("score", evaluation["opportunity_score"]),
            "score_version": SCORE_VERSION,
            "rsi": evaluation.get("rsi"),
            "drawdown": evaluation.get("drawdown"),
            "strategy_type": evaluation.get("strategy_type"),
            "opportunity_score": evaluation["opportunity_score"],
            "risk_level": evaluation.get("risk_level"),
            "risk_score": evaluation.get("risk_score"),
            "signal_confidence": evaluation.get("signal_confidence"),
            "classification_confidence": evaluation.get("classification_confidence"),
            "decision": evaluation["decision"],
            "technical_score": evaluation.get("technical_score"),
            "momentum_score": evaluation.get("momentum_score"),
            "fundamental_score": evaluation.get("fundamental_score"),
            "valuation_score": evaluation.get("valuation_score"),
            "components": evaluation.get("components"),
        }
        if self.db is not None:
            (self.db.table("signals")
             .upsert(signal, on_conflict="signal_date,ticker").execute())
        return signal

    def prepare_notification(self, candidates: list[dict], as_of_date: str) -> str:
        """후보 목록을 텔레그램 알림 메시지로 포맷한다.

        V8 메시지 포맷은 stock_scanner.build_alert_message가 단일 진실 원천이다.
        """
        from stock_scanner import build_alert_message

        return build_alert_message(candidates, as_of_date)
