"""V8 Decision Engine 통합 테스트 (A2/A3/A4): decision 필드 흐름.

- compute_signal_v8이 make_decision() 결과를 decision 필드로 반환한다.
- evaluate_opportunities가 OPPORTUNITY/STRONG_OPPORTUNITY일 때만 Signal을 생성한다.
- format_signal_message가 V8 spec §12 형식으로 메시지를 만든다.
"""

import pandas as pd

from stock_scanner import (compute_signal_v8, evaluate_opportunities,
                           format_signal_message, save_opportunity_score,
                           save_signal)
from decision_engine import make_decision


class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, db, table):
        self._db = db
        self._table = table

    def upsert(self, payload, on_conflict=None):
        self._db.calls.append(("upsert", self._table, payload, on_conflict))
        return self

    def execute(self):
        return _FakeResponse([])


class _FakeDb:
    def __init__(self):
        self.calls = []

    def table(self, name):
        return _FakeQuery(self, name)


# ---------------------------------------------------------------------------
# compute_signal_v8 → decision 필드
# ---------------------------------------------------------------------------

def _indicator_df(n=120):
    """완만한 상승 추세 합성 df (지표 컬럼은 compute_signal_v8이 계산)."""
    closes = list(range(100, 100 + n))
    return pd.DataFrame({
        "Close": closes,
        "High": [c * 1.01 for c in closes],
        "Volume": [1_000_000] * n,
    })


def test_compute_signal_v8_returns_decision_field():
    x = compute_signal_v8("TEST", _indicator_df(), strategy="general",
                          info=None, classification_confidence=1.0)
    assert x is not None
    assert "decision" in x
    assert x["decision"] == make_decision(
        x["opportunity_score"], x["risk_level"], x["signal_confidence"],
        x["strategy_type"], x["classification_confidence"])


def test_compute_signal_v8_decision_in_expected_grades():
    x = compute_signal_v8("TEST", _indicator_df(), strategy="general",
                          info=None, classification_confidence=1.0)
    assert x["decision"] in {"STRONG_OPPORTUNITY", "OPPORTUNITY",
                             "WATCH", "NEUTRAL", "AVOID"}


# ---------------------------------------------------------------------------
# evaluate_opportunities → Decision 기반 Signal 게이트
# ---------------------------------------------------------------------------

def _fake_result(ticker, decision, score):
    return {"ticker": ticker, "decision": decision, "score": score,
            "price": 100.0, "rsi": 30.0, "drawdown": -12.0,
            "data_date": "2026-08-13"}


def test_evaluate_opportunities_saves_all_scores_and_signals_only_on_opportunity(monkeypatch):
    db = _FakeDb()
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)
    monkeypatch.setattr("stock_scanner.load_watchlist", lambda db=None: ["SO", "OP", "WA", "AV"])

    results = {
        "SO": _fake_result("SO", "STRONG_OPPORTUNITY", 85),
        "OP": _fake_result("OP", "OPPORTUNITY", 70),
        "WA": _fake_result("WA", "WATCH", 60),
        "AV": _fake_result("AV", "AVOID", 20),
    }
    monkeypatch.setattr("stock_scanner.compute_signal_v8",
                        lambda t, df, m, s, i, c: results[t])
    monkeypatch.setattr("stock_scanner.fetch_history",
                        lambda t: pd.DataFrame({"Close": [1.0]}))
    monkeypatch.setattr("stock_scanner.fetch_info", lambda t: None)
    monkeypatch.setattr("stock_scanner.resolve_strategy",
                        lambda t, db=None, info=None: ("general", 0.5))
    monkeypatch.setattr("stock_scanner._market_history",
                        lambda: pd.DataFrame({"Close": [1.0]}))

    evaluate_opportunities()

    upserts = [c for c in db.calls if c[0] == "upsert"]
    opp_saved = [c for c in upserts if c[1] == "opportunity_scores"]
    signal_saved = [c for c in upserts if c[1] == "signals"]
    # opportunity_scores는 전 종목 저장
    assert sorted(c[2]["ticker"] for c in opp_saved) == ["AV", "OP", "SO", "WA"]
    # signals는 OPPORTUNITY/STRONG_OPPORTUNITY만 (WATCH 60점도 제외 — Decision이 게이트)
    assert sorted(c[2]["ticker"] for c in signal_saved) == ["OP", "SO"]
    assert all(c[2]["decision"] in ("OPPORTUNITY", "STRONG_OPPORTUNITY")
               for c in signal_saved)


def test_evaluate_opportunities_no_signal_when_persist_false(monkeypatch):
    db = _FakeDb()
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)
    monkeypatch.setattr("stock_scanner.load_watchlist", lambda db=None: ["SO"])
    monkeypatch.setattr("stock_scanner.compute_signal_v8",
                        lambda t, df, m, s, i, c: _fake_result("SO", "STRONG_OPPORTUNITY", 85))
    monkeypatch.setattr("stock_scanner.fetch_history",
                        lambda t: pd.DataFrame({"Close": [1.0]}))
    monkeypatch.setattr("stock_scanner.fetch_info", lambda t: None)
    monkeypatch.setattr("stock_scanner.resolve_strategy",
                        lambda t, db=None, info=None: ("general", 0.5))
    monkeypatch.setattr("stock_scanner._market_history",
                        lambda: pd.DataFrame({"Close": [1.0]}))

    evaluate_opportunities(persist=False)

    assert db.calls == []


def test_save_functions_delegate_decision(monkeypatch):
    """save_signal/save_opportunity_score가 decision을 payload로 전달한다."""
    db = _FakeDb()
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)
    x = {"ticker": "AAPL", "decision": "OPPORTUNITY", "score": 70,
         "price": 100.0, "rsi": 30.0, "drawdown": -12.0}
    save_signal(x, "2026-08-13")
    save_opportunity_score(x, "2026-08-13")
    payloads = {c[1]: c[2] for c in db.calls}
    assert payloads["signals"]["decision"] == "OPPORTUNITY"
    assert payloads["opportunity_scores"]["decision"] == "OPPORTUNITY"


# ---------------------------------------------------------------------------
# format_signal_message (V8 spec §12)
# ---------------------------------------------------------------------------

def _signal_dict():
    return {
        "ticker": "NVDA",
        "strategy_type": "established_growth",
        "decision": "OPPORTUNITY",
        "opportunity_score": 76,
        "risk_level": "MEDIUM",
        "signal_confidence": 0.84,
        "technical_score": 82,
        "momentum_score": 79,
        "fundamental_score": 88,
        "valuation_score": 61,
        "components": {"rsi_state": 20, "rsi_rebound": 15, "price_rebound": 15,
                       "drawdown": 15, "ma20": 15, "trend": 5,
                       "relative_strength": 10, "volume": 5},
    }


def test_format_signal_message_structure():
    msg = format_signal_message(_signal_dict())
    lines = msg.split("\n")
    assert lines[0] == "NVDA"
    assert lines[1] == "Strategy: 성장주"
    assert lines[2] == "Decision: 🟢 OPPORTUNITY"
    assert "" == lines[3]
    assert lines[4] == "Opportunity: 76"
    assert lines[5] == "Risk: MEDIUM"
    assert lines[6] == "Confidence: 0.84"
    assert "" == lines[7]
    assert lines[8] == "Technical: 82"
    assert lines[9] == "Momentum: 79"
    assert lines[10] == "Fundamental: 88"
    assert lines[11] == "Valuation: 61"
    assert "" == lines[12]
    assert lines[13].startswith("Reason: ")


def test_format_signal_message_reason_lists_components_with_scores():
    msg = format_signal_message(_signal_dict())
    reason = msg.split("Reason: ", 1)[1]
    # 점수 내림차순, 동점은 이름 오름차순
    assert reason == ("rsi_state(20), drawdown(15), ma20(15), price_rebound(15), "
                      "rsi_rebound(15), relative_strength(10), trend(5), volume(5)")


def test_format_signal_message_decision_emoji_mapping():
    x = _signal_dict()
    emojis = {
        "STRONG_OPPORTUNITY": "🔥",
        "OPPORTUNITY": "🟢",
        "WATCH": "👀",
        "NEUTRAL": "⚪",
        "AVOID": "🚫",
    }
    for decision, emoji in emojis.items():
        x["decision"] = decision
        assert f"Decision: {emoji} {decision}" in format_signal_message(x)


def test_format_signal_message_handles_missing_axes_and_confidence():
    x = {"ticker": "VOO", "strategy_type": "broad_market_etf",
         "decision": "WATCH", "opportunity_score": 45, "risk_level": "HIGH"}
    msg = format_signal_message(x)
    assert "Technical: -" in msg
    assert "Momentum: -" in msg
    assert "Fundamental: -" in msg
    assert "Valuation: -" in msg
    assert "Confidence: 0.00" in msg
    assert "Reason: -" in msg
