"""scan_runs 실행 이력 추적 테스트 (V8 §15)."""

import sys

from stock_scanner import finish_run, main, start_run


class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, db, table, rows):
        self._db = db
        self._table = table
        self._rows = rows
        self._filters = []
        self._op = None
        self._payload = None

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def execute(self):
        if self._op == "insert":
            row = dict(self._payload)
            row["id"] = len(self._rows) + 1
            self._rows.append(row)
            self._db.calls.append(("insert", self._table, self._payload))
            return _FakeResponse([row])
        if self._op == "update":
            for row in self._rows:
                if all(row.get(c) == v for f, c, v in self._filters):
                    row.update(self._payload)
            self._db.calls.append(("update", self._table, self._payload,
                                   self._filters))
            return _FakeResponse([])
        return _FakeResponse(self._rows)


class _FakeDb:
    def __init__(self, tables=None):
        self._tables = tables or {}
        self.calls = []

    def table(self, name):
        return _FakeQuery(self, name, self._tables.get(name, []))


# ---------------------------------------------------------------------------
# start_run
# ---------------------------------------------------------------------------


def test_start_run_returns_run_id(monkeypatch):
    db = _FakeDb({"scan_runs": []})
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)

    run_id = start_run()

    assert run_id == 1
    assert db.calls == [("insert", "scan_runs", {
        "started_at": "now()", "status": "running",
    })]
    assert db._tables["scan_runs"][0]["status"] == "running"


# ---------------------------------------------------------------------------
# finish_run
# ---------------------------------------------------------------------------


def test_finish_run_updates_scan_runs(monkeypatch):
    db = _FakeDb({"scan_runs": [{"id": 1, "status": "running"}]})
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)

    finish_run(1, {
        "total": 21, "evaluated": 20, "signals": 5, "failed": 1,
        "status": "completed", "error_summary": None,
    })

    row = db._tables["scan_runs"][0]
    assert row["status"] == "completed"
    assert row["finished_at"] == "now()"
    assert row["watchlist_count"] == 21
    assert row["evaluated_count"] == 20
    assert row["signal_count"] == 5
    assert row["failure_count"] == 1
    assert row["error_summary"] is None


def test_finish_run_failure_stores_error_summary(monkeypatch):
    db = _FakeDb({"scan_runs": [{"id": 2, "status": "running"}]})
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)

    finish_run(2, {
        "total": 21, "evaluated": 0, "signals": 0, "failed": 21,
        "status": "failed", "error_summary": "AAPL: network error",
    })

    row = db._tables["scan_runs"][0]
    assert row["status"] == "failed"
    assert row["error_summary"] == "AAPL: network error"
    assert row["failure_count"] == 21


# ---------------------------------------------------------------------------
# main() 통합
# ---------------------------------------------------------------------------


def test_main_records_completed_run(monkeypatch):
    db = _FakeDb({"scan_runs": []})
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)
    monkeypatch.setattr("stock_scanner.scan",
                        lambda **kw: ([{"ticker": "AAPL"}], []))
    monkeypatch.setattr("stock_scanner.evaluate_opportunities", lambda **kw: [])
    monkeypatch.setattr("stock_scanner.load_watchlist", lambda db=None: ["AAPL"])
    monkeypatch.setattr("sys.argv", ["stock_scanner"])

    main()

    row = db._tables["scan_runs"][0]
    assert row["status"] == "completed"
    assert row["watchlist_count"] == 1
    assert row["evaluated_count"] == 1
    assert row["signal_count"] == 1
    assert row["failure_count"] == 0
    assert row["error_summary"] is None


def test_main_records_failed_run_on_exception(monkeypatch):
    db = _FakeDb({"scan_runs": []})
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)

    def boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr("stock_scanner.scan", boom)
    monkeypatch.setattr("sys.argv", ["stock_scanner"])

    try:
        main()
    except RuntimeError:
        pass
    else:
        raise AssertionError("main() should re-raise the scan exception")

    row = db._tables["scan_runs"][0]
    assert row["status"] == "failed"
    assert row["error_summary"] == "db down"
    assert row["failure_count"] == 0