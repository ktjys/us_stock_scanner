"""watchlist 관리 CLI 테스트."""

from types import SimpleNamespace

from manage_watchlist import (_name_from_ticker, _ticker_from_name, cmd_add,
                              cmd_list, cmd_set_active, resolve_symbol)


class _FakeTicker:
    def __init__(self, info_or_exc):
        self._info = info_or_exc

    @property
    def info(self):
        if isinstance(self._info, Exception):
            raise self._info
        return self._info


class _FakeSearch:
    def __init__(self, quotes):
        self.quotes = quotes


def test_name_from_ticker_dot_falls_back_to_dash(monkeypatch):
    calls = []

    def fake_ticker(sym):
        calls.append(sym)
        if sym == "BRK.B":
            return _FakeTicker({})
        return _FakeTicker({"longName": "Berkshire Hathaway Inc."})

    monkeypatch.setattr("manage_watchlist.yf", SimpleNamespace(Ticker=fake_ticker))
    assert _name_from_ticker("BRK.B") == "Berkshire Hathaway Inc."
    assert calls == ["BRK.B", "BRK-B"]


def test_name_from_ticker_invalid_returns_none(monkeypatch):
    def fake_ticker(sym):
        return _FakeTicker(RuntimeError("HTTP Error 404"))

    monkeypatch.setattr("manage_watchlist.yf", SimpleNamespace(Ticker=fake_ticker))
    assert _name_from_ticker("XXXXX") is None


def test_ticker_from_name_picks_first_equity(monkeypatch):
    quotes = [
        {"symbol": "TL0.F", "longname": "Tesla, Inc.", "quoteType": "EQUITY"},
        {"symbol": "YTSL.NE", "longname": "Tesla (TSLA) Yield Shares", "quoteType": "ETF"},
    ]
    monkeypatch.setattr("manage_watchlist.yf",
                        SimpleNamespace(Search=lambda query, max_results: _FakeSearch(quotes)))
    assert _ticker_from_name("Tesla") == ("TL0.F", "Tesla, Inc.")


def test_ticker_from_name_no_quotes_returns_none(monkeypatch):
    monkeypatch.setattr("manage_watchlist.yf",
                        SimpleNamespace(Search=lambda query, max_results: _FakeSearch([])))
    assert _ticker_from_name("테슬라") is None


def test_ticker_from_name_exception_returns_none(monkeypatch):
    def fake_search(query, max_results):
        raise RuntimeError("network")

    monkeypatch.setattr("manage_watchlist.yf", SimpleNamespace(Search=fake_search))
    assert _ticker_from_name("Tesla") is None


def test_resolve_ticker_route(monkeypatch):
    monkeypatch.setattr("manage_watchlist._name_from_ticker", lambda code: "Tesla, Inc.")
    assert resolve_symbol("tsla") == ("TSLA", "Tesla, Inc.")


def test_resolve_name_route(monkeypatch):
    monkeypatch.setattr("manage_watchlist._name_from_ticker", lambda code: None)
    monkeypatch.setattr("manage_watchlist._ticker_from_name", lambda query: ("TSLA", "Tesla, Inc."))
    assert resolve_symbol("Tesla") == ("TSLA", "Tesla, Inc.")


def test_resolve_failure_returns_none(monkeypatch):
    monkeypatch.setattr("manage_watchlist._name_from_ticker", lambda code: None)
    monkeypatch.setattr("manage_watchlist._ticker_from_name", lambda query: None)
    assert resolve_symbol("XXXXX") is None


class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, rows, calls):
        self._rows = rows
        self._calls = calls
        self._eq = None
        self._pending_update = None

    def select(self, *cols):
        return self

    def order(self, col):
        return self

    def limit(self, n):
        return self

    def eq(self, col, val):
        self._eq = (col, val)
        return self

    def upsert(self, payload, on_conflict=None):
        self._calls.append(("upsert", payload, on_conflict))
        return self

    def update(self, payload):
        self._pending_update = payload
        return self

    def execute(self):
        if self._pending_update is not None:
            self._calls.append(("update", self._pending_update, self._eq))
        return _FakeResponse(self._rows)


class _FakeDb:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.calls = []

    def table(self, name):
        return _FakeQuery(self._rows, self.calls)


def test_cmd_add_resolves_and_upserts(monkeypatch):
    monkeypatch.setattr("manage_watchlist.resolve_symbol",
                        lambda item: ("TSLA", "Tesla, Inc.") if item.upper() == "TSLA" else None)
    db = _FakeDb([{"ticker": "AAPL"}])  # 행이 있어 CSV 시드 생략
    assert cmd_add(db, ["TSLA"]) == 0
    assert db.calls == [("upsert", [{"ticker": "TSLA", "name": "Tesla, Inc.", "active": True}], "ticker")]


def test_cmd_add_skips_invalid(monkeypatch, capsys):
    monkeypatch.setattr("manage_watchlist.resolve_symbol", lambda item: None)
    db = _FakeDb([{"ticker": "AAPL"}])
    assert cmd_add(db, ["XXXXX"]) == 1
    out = capsys.readouterr().out
    assert "추가 안 됨" in out
    assert "추가된 종목이 없습니다" in out
    assert db.calls == []


def test_cmd_add_existing_reactivates(monkeypatch, capsys):
    monkeypatch.setattr("manage_watchlist.resolve_symbol", lambda item: ("VOO", "Vanguard S&P 500 ETF"))
    db = _FakeDb([{"ticker": "VOO"}])
    assert cmd_add(db, ["VOO"]) == 0
    assert db.calls == [("upsert", [{"ticker": "VOO", "name": "Vanguard S&P 500 ETF", "active": True}], "ticker")]
    assert "이미 있음 → 활성화" in capsys.readouterr().out


def test_cmd_add_mixed_valid_invalid(monkeypatch, capsys):
    def fake_resolve(item):
        return {"TSLA": ("TSLA", "Tesla, Inc.")}.get(item.upper())

    monkeypatch.setattr("manage_watchlist.resolve_symbol", fake_resolve)
    db = _FakeDb([{"ticker": "AAPL"}])
    assert cmd_add(db, ["TSLA", "XXXXX"]) == 0
    assert db.calls == [("upsert", [{"ticker": "TSLA", "name": "Tesla, Inc.", "active": True}], "ticker")]
    out = capsys.readouterr().out
    assert "✅ TSLA" in out
    assert "추가 안 됨" in out


def test_cmd_add_seeds_csv_when_table_empty(monkeypatch):
    seeded = []
    monkeypatch.setattr("manage_watchlist._seed_csv", lambda db: seeded.append(True))
    monkeypatch.setattr("manage_watchlist.resolve_symbol", lambda item: ("TSLA", "Tesla, Inc."))
    db = _FakeDb()  # 테이블이 비어 있음
    assert cmd_add(db, ["TSLA"]) == 0
    assert seeded == [True]


def test_cmd_add_skips_seed_when_rows_exist(monkeypatch):
    seeded = []
    monkeypatch.setattr("manage_watchlist._seed_csv", lambda db: seeded.append(True))
    monkeypatch.setattr("manage_watchlist.resolve_symbol", lambda item: ("TSLA", "Tesla, Inc."))
    db = _FakeDb([{"ticker": "AAPL"}])
    cmd_add(db, ["TSLA"])
    assert seeded == []


def test_cmd_set_active_existing(capsys):
    db = _FakeDb([{"ticker": "VOO"}])
    cmd_set_active(db, ["VOO"], active=False)
    assert db.calls == [("update", {"active": False}, ("ticker", "VOO"))]
    assert "제외됨" in capsys.readouterr().out


def test_cmd_set_active_missing_ticker(capsys):
    db = _FakeDb()
    cmd_set_active(db, ["VOO"], active=False)
    assert db.calls == []
    assert "없는 종목" in capsys.readouterr().out


def test_cmd_list_empty(capsys):
    cmd_list(_FakeDb())
    assert "비어 있습니다" in capsys.readouterr().out


def test_cmd_list_shows_rows(capsys):
    db = _FakeDb([
        {"ticker": "AAPL", "name": "Apple", "active": True},
        {"ticker": "VOO", "name": "Vanguard S&P 500 ETF", "active": False},
    ])
    cmd_list(db)
    out = capsys.readouterr().out
    assert "✅ AAPL" in out
    assert "🚫 VOO" in out
