"""데이터 품질 로깅 테스트 (V8 스펙 §16).

log_data_quality()가 data_quality_log 테이블에 기록하고,
scan()의 API 실패와 analyze()의 오래된 데이터/가격 누락이 로깅되는지 검증한다.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd

from stock_scanner import (analyze, compute_signal_v8, evaluate_opportunities,
                           log_data_quality, scan)


class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, db, table):
        self._db = db
        self._table = table

    def insert(self, payload):
        self._db.calls.append(("insert", self._table, payload))
        return self

    def execute(self):
        return _FakeResponse([])


class _FakeDb:
    def __init__(self):
        self.calls = []

    def table(self, name):
        return _FakeQuery(self, name)


# ---------------------------------------------------------------------------
# log_data_quality
# ---------------------------------------------------------------------------


def test_log_data_quality_inserts_row(monkeypatch):
    db = _FakeDb()
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)
    log_data_quality("AAPL", "api_failure", {"error": "network"})
    assert db.calls == [("insert", "data_quality_log", {
        "ticker": "AAPL",
        "issue_type": "api_failure",
        "details": {"error": "network"},
        "logged_at": "now()",
    })]


# ---------------------------------------------------------------------------
# scan() API 실패 로깅
# ---------------------------------------------------------------------------


def test_scan_logs_api_failure(monkeypatch):
    logged = []

    def fake_analyze(t, date=None, db=None):
        raise RuntimeError("network")

    monkeypatch.setattr("stock_scanner.analyze", fake_analyze)
    monkeypatch.setattr("stock_scanner.load_watchlist", lambda db=None: ["BAD"])
    monkeypatch.setattr("stock_scanner.get_db", lambda: _FakeDb())
    monkeypatch.setattr("stock_scanner.update_returns", lambda: None)
    monkeypatch.setattr("stock_scanner.is_us_market_holiday", lambda d: False)
    monkeypatch.setattr("stock_scanner.log_data_quality",
                        lambda t, issue, details=None: logged.append((t, issue, details)))

    cands, failures = scan(persist=True, notify=False)
    assert failures == ["BAD"]
    assert logged == [("BAD", "api_failure", {"error": "network"})]


# ---------------------------------------------------------------------------
# analyze() 오래된 데이터 감지
# ---------------------------------------------------------------------------


def test_analyze_logs_stale_data(monkeypatch):
    old = datetime.now(timezone.utc).date() - timedelta(days=30)
    idx = pd.DatetimeIndex([old])
    df = pd.DataFrame({"Close": [100.0], "High": [102.0], "Volume": [1_000_000]},
                      index=idx)
    monkeypatch.setattr("stock_scanner.fetch_history", lambda t: df)
    db = _FakeDb()
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)
    assert analyze("AAPL", db=db) is None
    inserts = [c for c in db.calls if c[0] == "insert"]
    assert len(inserts) == 1
    _, table, payload = inserts[0]
    assert table == "data_quality_log"
    assert payload["ticker"] == "AAPL"
    assert payload["issue_type"] == "stale_data"
    assert payload["details"]["age_days"] >= 30


# ---------------------------------------------------------------------------
# analyze() 가격 데이터 누락
# ---------------------------------------------------------------------------


def test_analyze_logs_missing_price(monkeypatch):
    monkeypatch.setattr("stock_scanner.fetch_history", lambda t: pd.DataFrame())
    db = _FakeDb()
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)
    assert analyze("AAPL", db=db) is None
    inserts = [c for c in db.calls if c[0] == "insert"]
    assert len(inserts) == 1
    _, table, payload = inserts[0]
    assert table == "data_quality_log"
    assert payload["ticker"] == "AAPL"
    assert payload["issue_type"] == "missing_price"


# ---------------------------------------------------------------------------
# fetch_info()가 None이면 fundamental_null 로깅
# ---------------------------------------------------------------------------


def _recent_df():
    today = datetime.now(timezone.utc).date()
    idx = pd.DatetimeIndex([today])
    return pd.DataFrame({"Close": [100.0], "High": [102.0], "Volume": [1_000_000]},
                        index=idx)


def test_analyze_logs_fundamental_null(monkeypatch):
    monkeypatch.setattr("stock_scanner.fetch_history", lambda t: _recent_df())
    monkeypatch.setattr("stock_scanner.fetch_info", lambda t: None)
    monkeypatch.setattr("stock_scanner.resolve_strategy",
                        lambda t, db=None, info=None: ("general", 0.5))
    monkeypatch.setattr("stock_scanner.compute_signal_v8",
                        lambda t, d, m, s, i, c: {"ticker": t, "score": 0})
    db = _FakeDb()
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)
    analyze("AAPL", db=db)
    inserts = [c for c in db.calls if c[0] == "insert"]
    assert len(inserts) == 1
    _, table, payload = inserts[0]
    assert table == "data_quality_log"
    assert payload["ticker"] == "AAPL"
    assert payload["issue_type"] == "fundamental_null"
    assert payload["details"] == {"reason": "info_is_none"}


def test_evaluate_opportunities_logs_fundamental_null(monkeypatch):
    db = _FakeDb()
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)
    monkeypatch.setattr("stock_scanner.load_watchlist", lambda db=None: ["AAPL"])
    monkeypatch.setattr("stock_scanner.fetch_history",
                        lambda t: pd.DataFrame({"Close": [1.0]}))
    monkeypatch.setattr("stock_scanner.fetch_info", lambda t: None)
    monkeypatch.setattr("stock_scanner.resolve_strategy",
                        lambda t, db=None, info=None: ("general", 0.5))
    monkeypatch.setattr("stock_scanner.compute_signal_v8",
                        lambda t, d, m, s, i, c: None)
    monkeypatch.setattr("stock_scanner._market_history",
                        lambda: pd.DataFrame({"Close": [1.0]}))
    evaluate_opportunities()
    inserts = [c for c in db.calls if c[0] == "insert"]
    assert len(inserts) == 1
    _, table, payload = inserts[0]
    assert table == "data_quality_log"
    assert payload["ticker"] == "AAPL"
    assert payload["issue_type"] == "fundamental_null"
    assert payload["details"] == {"reason": "info_is_none"}


# ---------------------------------------------------------------------------
# 핵심 펀더멘털 필드 결측 → fundamental_incomplete 로깅
# ---------------------------------------------------------------------------


def _signal_df():
    closes = list(range(100, 180))
    return pd.DataFrame({"Close": closes, "High": [c * 1.01 for c in closes],
                         "Volume": [1_000_000] * len(closes)})


def test_compute_signal_v8_logs_fundamental_incomplete(monkeypatch):
    logged = []

    def fake_log(t, issue, details=None):
        logged.append((t, issue, details))

    monkeypatch.setattr("stock_scanner.log_data_quality", fake_log)
    x = compute_signal_v8("AAPL", _signal_df(), strategy="quality",
                          info={"dividendYield": 0.02, "earningsGrowth": 0.15},
                          classification_confidence=1.0)
    assert x is not None
    assert logged == [("AAPL", "fundamental_incomplete", {
        "missing_fields": ["trailingPE", "profitMargins"],
        "valuation_score": None,
        "profitability_score": None,
    })]


def test_compute_signal_v8_no_fundamental_logging_when_info_none(monkeypatch):
    logged = []
    monkeypatch.setattr("stock_scanner.log_data_quality",
                        lambda t, issue, details=None: logged.append((t, issue, details)))
    x = compute_signal_v8("AAPL", _signal_df(), strategy="general", info=None,
                          classification_confidence=1.0)
    assert x is not None
    assert logged == []
