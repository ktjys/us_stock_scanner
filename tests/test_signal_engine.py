"""SignalEngine 통합 테스트 (신호 생성/쿨다운/저장/알림)."""

from signal_engine import (SCORE_IMPROVEMENT_THRESHOLD, SignalEngine,
                           _DECISION_RANK)


def _evaluation(**overrides):
    base = {
        "ticker": "AAPL",
        "price": 100.0,
        "rsi": 30.0,
        "drawdown": -12.0,
        "opportunity_score": 70,
        "score": 70,
        "risk_level": "MEDIUM",
        "risk_score": 40,
        "signal_confidence": 0.82,
        "classification_confidence": 0.9,
        "decision": "OPPORTUNITY",
        "strategy_type": "general",
        "technical_score": 60,
        "momentum_score": 50,
        "fundamental_score": None,
        "valuation_score": None,
        "components": {"rsi_state": 20},
    }
    base.update(overrides)
    return base


class _FakeQuery:
    def __init__(self, db, table):
        self._db = db
        self._table = table

    def upsert(self, payload, on_conflict=None):
        self._db.calls.append(("upsert", self._table, payload, on_conflict))
        return self

    def execute(self):
        return None


class _FakeDb:
    def __init__(self):
        self.calls = []

    def table(self, name):
        return _FakeQuery(self, name)


# ---------------------------------------------------------------------------
# should_generate_signal — 쿨다운
# ---------------------------------------------------------------------------


def test_should_generate_signal_new_signal_when_no_previous():
    engine = SignalEngine()
    ok, reason = engine.should_generate_signal(_evaluation(), None)
    assert ok is True
    assert reason == "new_signal"


def test_should_generate_signal_blocks_below_opportunity():
    engine = SignalEngine()
    ok, reason = engine.should_generate_signal(_evaluation(decision="WATCH"), None)
    assert ok is False
    assert reason == "decision_below_threshold"


def test_should_generate_signal_allows_decision_improvement():
    engine = SignalEngine()
    prev = {"decision": "WATCH", "opportunity_score": 70}
    ok, reason = engine.should_generate_signal(_evaluation(), prev)
    assert ok is True
    assert reason == "decision_improved"


def test_should_generate_signal_allows_score_improvement():
    engine = SignalEngine()
    prev = {"decision": "OPPORTUNITY", "opportunity_score": 70 - SCORE_IMPROVEMENT_THRESHOLD}
    ok, reason = engine.should_generate_signal(_evaluation(), prev)
    assert ok is True
    assert reason == "score_improved"


def test_should_generate_signal_blocks_no_improvement():
    engine = SignalEngine()
    prev = {"decision": "OPPORTUNITY", "opportunity_score": 69}
    ok, reason = engine.should_generate_signal(_evaluation(), prev)
    assert ok is False
    assert reason == "no_improvement"


def test_should_generate_signal_blocks_decision_downgrade():
    engine = SignalEngine()
    prev = {"decision": "STRONG_OPPORTUNITY", "opportunity_score": 60}
    ok, reason = engine.should_generate_signal(_evaluation(), prev)
    assert ok is False
    assert reason == "no_improvement"


def test_decision_rank_ordering():
    assert _DECISION_RANK["STRONG_OPPORTUNITY"] > _DECISION_RANK["OPPORTUNITY"]
    assert _DECISION_RANK["OPPORTUNITY"] > _DECISION_RANK["WATCH"]
    assert _DECISION_RANK["WATCH"] > _DECISION_RANK["NEUTRAL"]
    assert _DECISION_RANK["NEUTRAL"] > _DECISION_RANK["AVOID"]


# ---------------------------------------------------------------------------
# generate_signal — DB 저장
# ---------------------------------------------------------------------------


def test_generate_signal_builds_db_payload():
    engine = SignalEngine()
    signal = engine.generate_signal(_evaluation(), "2026-08-13")
    assert signal["ticker"] == "AAPL"
    assert signal["signal_date"] == "2026-08-13"
    assert signal["signal_price"] == 100.0
    assert signal["opportunity_score"] == 70
    assert signal["score"] == 70
    assert signal["decision"] == "OPPORTUNITY"
    assert signal["strategy_type"] == "general"
    assert signal["score_version"] == 8


def test_generate_signal_upserts_when_db_set():
    db = _FakeDb()
    engine = SignalEngine(db)
    signal = engine.generate_signal(_evaluation(), "2026-08-13")
    assert len(db.calls) == 1
    table, payload, conflict = db.calls[0][1], db.calls[0][2], db.calls[0][3]
    assert db.calls[0][0] == "upsert"
    assert table == "signals"
    assert conflict == "signal_date,ticker"
    assert payload["ticker"] == signal["ticker"]


def test_generate_signal_skips_db_when_db_none():
    db = _FakeDb()
    engine = SignalEngine(db=None)
    engine.generate_signal(_evaluation(), "2026-08-13")
    assert db.calls == []


# ---------------------------------------------------------------------------
# prepare_notification — 알림 메시지
# ---------------------------------------------------------------------------


def test_prepare_notification_builds_message():
    engine = SignalEngine()
    msg = engine.prepare_notification([_evaluation()], "2026-08-13")
    assert "AAPL" in msg
    assert "2026-08-13" in msg
    assert "OPPORTUNITY" in msg
