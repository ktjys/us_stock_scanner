"""데이터 품질 로깅 테스트 (V8 스펙 §16).

log_data_quality()가 data_quality_log 테이블에 기록하고,
scan()의 API 실패와 analyze()의 오래된 데이터/가격 누락이 로깅되는지 검증한다.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd

from stock_scanner import analyze, log_data_quality, scan


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