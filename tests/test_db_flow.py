"""DB 흐름 (저장/조회/삭제)과 페이지네이션 헬퍼 테스트."""

from datetime import datetime, timedelta, timezone

import pandas as pd

from stock_scanner import (_fetch_all, analyze, load_watchlist, prune_daily_data,
                           save_daily, save_signal, telegram, update_returns)


class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, db, table, rows):
        self._db = db
        self._table = table
        self._rows = rows
        self._filters = []
        self._range = None
        self._pending_delete = False

    def select(self, *cols):
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, vals))
        return self

    def gte(self, col, val):
        self._filters.append(("gte", col, val))
        return self

    def lt(self, col, val):
        self._filters.append(("lt", col, val))
        return self

    def order(self, col):
        self._filters.append(("order", col))
        return self

    def is_(self, col, val):
        self._filters.append(("is", col, val))
        return self

    def range(self, start, end):
        self._range = (start, end)
        self._db.calls.append(("range", start, end))
        return self

    def upsert(self, payload, on_conflict=None):
        self._db.calls.append(("upsert", self._table, payload, on_conflict))
        return self

    def delete(self):
        self._pending_delete = True
        return self

    def execute(self):
        if self._pending_delete:
            cutoff = next((v for f, c, v in self._filters if f == "lt"), None)
            deleted = [r for r in self._rows if r["date"] < cutoff]
            self._db.calls.append(("delete", self._table, cutoff, len(deleted)))
            return _FakeResponse(deleted)
        rows = self._rows
        for f in self._filters:
            if f[0] == "is" and f[2] is None:
                rows = [r for r in rows if r.get(f[1]) is None]
        if self._range is not None:
            start, end = self._range
            rows = rows[start:end + 1]
        return _FakeResponse(rows)


class _FakeDb:
    def __init__(self, tables=None):
        self._tables = tables or {}
        self.calls = []

    def table(self, name):
        return _FakeQuery(self, name, self._tables.get(name, []))


# ---------------------------------------------------------------------------
# _fetch_all 페이지네이션
# ---------------------------------------------------------------------------


def test_fetch_all_single_page():
    rows = [{"id": i} for i in range(100)]
    db = _FakeDb({"signals": rows})
    out = _fetch_all(db.table("signals").select("*"))
    assert len(out) == 100
    assert db.calls == [("range", 0, 999)]


def test_fetch_all_paginates_2500_rows():
    rows = [{"id": i} for i in range(2500)]
    db = _FakeDb({"signals": rows})
    out = _fetch_all(db.table("signals").select("*"))
    assert len(out) == 2500
    assert db.calls == [("range", 0, 999), ("range", 1000, 1999),
                        ("range", 2000, 2999)]


# ---------------------------------------------------------------------------
# update_returns
# ---------------------------------------------------------------------------


def test_update_returns_computes_returns(monkeypatch):
    signals = [{"id": 1, "ticker": "AAPL", "signal_date": "2026-08-01",
                "signal_price": 100.0}]
    daily = [{"date": f"2026-08-{d:02d}", "ticker": "AAPL", "price": p}
             for d, p in zip(range(2, 11), range(101, 110))]
    db = _FakeDb({"signals": signals, "daily_data": daily})
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)
    update_returns()
    upserts = [c for c in db.calls if c[0] == "upsert"]
    assert len(upserts) == 1
    _, table, payload, on_conflict = upserts[0]
    assert table == "signals"
    assert on_conflict == "id"
    assert payload == [{"id": 1, "return_5d": (105.0 / 100.0 - 1) * 100}]


def test_update_returns_skips_completed_signals(monkeypatch):
    signals = [
        {"id": 1, "ticker": "AAPL", "signal_date": "2026-08-01",
         "signal_price": 100.0, "return_20d": 3.0},
        {"id": 2, "ticker": "MSFT", "signal_date": "2026-08-01",
         "signal_price": 200.0},
    ]
    daily = ([{"date": f"2026-08-{d:02d}", "ticker": "AAPL", "price": 101.0}
              for d in range(2, 11)]
             + [{"date": f"2026-08-{d:02d}", "ticker": "MSFT", "price": p}
                for d, p in zip(range(2, 11), range(201, 210))])
    db = _FakeDb({"signals": signals, "daily_data": daily})
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)
    update_returns()
    upserts = [c for c in db.calls if c[0] == "upsert"]
    assert len(upserts) == 1
    _, table, payload, on_conflict = upserts[0]
    assert table == "signals"
    assert on_conflict == "id"
    assert payload == [{"id": 2, "return_5d": (205.0 / 200.0 - 1) * 100}]


def test_update_returns_skips_when_insufficient_data(monkeypatch):
    signals = [{"id": 1, "ticker": "AAPL", "signal_date": "2026-08-01",
                "signal_price": 100.0}]
    daily = [{"date": f"2026-08-{d:02d}", "ticker": "AAPL", "price": p}
             for d, p in zip(range(2, 5), range(101, 104))]
    db = _FakeDb({"signals": signals, "daily_data": daily})
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)
    update_returns()
    assert [c for c in db.calls if c[0] == "upsert"] == []


# ---------------------------------------------------------------------------
# save_daily / save_signal
# ---------------------------------------------------------------------------


def test_save_daily_upsert_payload(monkeypatch):
    x = {"ticker": "AAPL", "price": 100.0, "rsi": 30.0, "prev_rsi": 35.0,
         "ma20": 99.0, "ma50": 98.0, "drawdown": -12.0, "volume_ratio": 1.5,
         "score": 80, "relative_strength_5d": 1.5}
    db = _FakeDb()
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)
    save_daily(x, "2026-08-13")
    assert db.calls == [("upsert", "daily_data", {
        "date": "2026-08-13", "ticker": "AAPL", "price": 100.0,
        "rsi": 30.0, "prev_rsi": 35.0, "ma20": 99.0, "ma50": 98.0,
        "drawdown": -12.0, "volume_ratio": 1.5, "score": 80, "relative_strength_5d": 1.5,
        "score_version": 6,
    }, None)]


def test_save_signal_below_threshold_no_upsert(monkeypatch):
    x = {"ticker": "AAPL", "score": 40, "price": 100.0, "rsi": 50.0,
         "drawdown": -5.0}
    db = _FakeDb()
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)
    save_signal(x, "2026-08-13")
    assert db.calls == []


def test_save_signal_above_threshold_upsert(monkeypatch):
    x = {"ticker": "AAPL", "score": 70, "price": 100.0, "rsi": 30.0,
         "drawdown": -12.0}
    db = _FakeDb()
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)
    save_signal(x, "2026-08-13")
    assert db.calls == [("upsert", "signals", {
        "signal_date": "2026-08-13", "ticker": "AAPL",
        "signal_price": 100.0, "score": 70, "score_version": 6,
        "rsi": 30.0, "drawdown": -12.0,
    }, "signal_date,ticker")]


# ---------------------------------------------------------------------------
# load_watchlist
# ---------------------------------------------------------------------------


def test_load_watchlist_active_only():
    db = _FakeDb({"watchlist": [
        {"ticker": "AAPL", "active": True},
        {"ticker": "VOO", "active": False},
    ]})
    assert load_watchlist(db) == ["AAPL"]


def test_load_watchlist_all_inactive_returns_empty():
    db = _FakeDb({"watchlist": [
        {"ticker": "AAPL", "active": False},
        {"ticker": "VOO", "active": False},
    ]})
    assert load_watchlist(db) == []


def test_load_watchlist_empty_table_seeds_csv(monkeypatch, tmp_path):
    csv_file = tmp_path / "watchlist.csv"
    csv_file.write_text("ticker,name\nAAPL,Apple\nMSFT,Microsoft\n")
    monkeypatch.setattr("stock_scanner.WATCHLIST_FILE", str(csv_file))
    db = _FakeDb({"watchlist": []})
    assert load_watchlist(db) == ["AAPL", "MSFT"]
    assert db.calls == [("upsert", "watchlist", [
        {"ticker": "AAPL", "name": "Apple"},
        {"ticker": "MSFT", "name": "Microsoft"},
    ], "ticker")]


def test_load_watchlist_no_db_reads_csv(monkeypatch, tmp_path):
    csv_file = tmp_path / "watchlist.csv"
    csv_file.write_text("ticker,name\nAAPL,Apple\n")
    monkeypatch.setattr("stock_scanner.WATCHLIST_FILE", str(csv_file))
    assert load_watchlist(None) == ["AAPL"]


# ---------------------------------------------------------------------------
# prune_daily_data
# ---------------------------------------------------------------------------


def test_prune_daily_data_deletes_old_rows(monkeypatch):
    today = datetime.now(timezone.utc).date()
    old = (today - timedelta(days=400)).strftime("%Y-%m-%d")
    recent = (today - timedelta(days=10)).strftime("%Y-%m-%d")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
    db = _FakeDb({"daily_data": [
        {"date": old, "ticker": "AAPL"},
        {"date": recent, "ticker": "AAPL"},
    ]})
    monkeypatch.setattr("stock_scanner.get_db", lambda: db)
    assert prune_daily_data() == 1
    deletes = [c for c in db.calls if c[0] == "delete"]
    assert len(deletes) == 1
    assert deletes[0][1] == "daily_data"
    assert deletes[0][2] == cutoff


# ---------------------------------------------------------------------------
# telegram
# ---------------------------------------------------------------------------


def test_telegram_sends_when_env_set(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    calls = []

    class _Resp:
        def raise_for_status(self):
            return None

    def fake_post(url, data, timeout):
        calls.append((url, data, timeout))
        return _Resp()

    monkeypatch.setattr("stock_scanner.requests.post", fake_post)
    telegram("hello")
    assert calls == [("https://api.telegram.org/bottok/sendMessage",
                      {"chat_id": "chat", "text": "hello"}, 15)]


def test_telegram_prints_when_env_missing(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    called = []
    monkeypatch.setattr("stock_scanner.requests.post",
                        lambda *a, **k: called.append(1))
    telegram("hello")
    assert called == []
    assert "hello" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# analyze recency
# ---------------------------------------------------------------------------


def test_analyze_skips_stale_data(monkeypatch, capsys):
    old = datetime.now(timezone.utc).date() - timedelta(days=30)
    idx = pd.DatetimeIndex([old])
    df = pd.DataFrame({"Close": [100.0], "High": [102.0], "Volume": [1_000_000]},
                      index=idx)
    monkeypatch.setattr("stock_scanner.fetch_history", lambda t: df)
    assert analyze("AAPL") is None
    assert "스킵" in capsys.readouterr().out


def test_analyze_recent_data_returns_signal(monkeypatch):
    today = datetime.now(timezone.utc).date()
    idx = pd.DatetimeIndex([today])
    df = pd.DataFrame({"Close": [100.0], "High": [102.0], "Volume": [1_000_000]},
                      index=idx)
    monkeypatch.setattr("stock_scanner.fetch_history", lambda t: df)
    monkeypatch.setattr("stock_scanner.compute_signal",
                        lambda t, d, m: {"ticker": t, "score": 80})
    assert analyze("AAPL") == {"ticker": "AAPL", "score": 80}