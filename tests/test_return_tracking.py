"""Return Tracking 강화 기능 테스트."""

from collections import defaultdict
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from stock_scanner import _date_key, update_returns


class FakeTable:
    def __init__(self, rows):
        self._rows = rows
        self._updates = []
        self._filters = {}

    def select(self, *args):
        return self

    def is_(self, col, val):
        self._filters[col] = ("is", val)
        return self

    def in_(self, col, vals):
        self._filters[col] = ("in", vals)
        return self

    def gte(self, col, val):
        self._filters[col] = ("gte", val)
        return self

    def order(self, col, desc=False):
        return self

    def eq(self, col, val):
        self._filters[col] = ("eq", val)
        return self

    def execute(self):
        filtered = self._rows
        for col, (op, val) in self._filters.items():
            if op == "eq":
                filtered = [r for r in filtered if r.get(col) == val]
            elif op == "in":
                filtered = [r for r in filtered if r.get(col) in val]
            elif op == "gte":
                filtered = [r for r in filtered if r.get(col) >= val]
            elif op == "lt":
                filtered = [r for r in filtered if r.get(col) < val]
            elif op == "is":
                if val is None:
                    filtered = [r for r in filtered if r.get(col) is None]
                else:
                    filtered = [r for r in filtered if r.get(col) == val]
        return MagicMock(data=filtered)

    def update(self, data):
        self._updates.append(data)
        return self


class FakeDb:
    def __init__(self, signals, daily_data, spy_data=None):
        self._signals = FakeTable(signals)
        all_daily = list(daily_data) + list(spy_data or [])
        self._daily_rows = all_daily

    def table(self, name):
        if name == "daily_data":
            return FakeTable(self._daily_rows)
        if name == "signals":
            return self._signals
        return FakeTable([])


def test_update_returns_calculates_exit_price():
    signals = [
        {"id": 1, "ticker": "AAPL", "signal_date": "2026-08-01",
         "signal_price": 100.0, "return_20d": None}
    ]
    daily_data = [
        {"date": "2026-08-04", "ticker": "AAPL", "price": 102.0},
        {"date": "2026-08-05", "ticker": "AAPL", "price": 104.0},
        {"date": "2026-08-06", "ticker": "AAPL", "price": 103.0},
        {"date": "2026-08-07", "ticker": "AAPL", "price": 105.0},
        {"date": "2026-08-08", "ticker": "AAPL", "price": 106.0},
        {"date": "2026-08-11", "ticker": "AAPL", "price": 108.0},
        {"date": "2026-08-12", "ticker": "AAPL", "price": 107.0},
        {"date": "2026-08-13", "ticker": "AAPL", "price": 109.0},
        {"date": "2026-08-14", "ticker": "AAPL", "price": 110.0},
    ]
    spy_data = [
        {"date": "2026-08-01", "ticker": "SPY", "price": 500.0},
        {"date": "2026-08-04", "ticker": "SPY", "price": 502.0},
        {"date": "2026-08-05", "ticker": "SPY", "price": 504.0},
        {"date": "2026-08-06", "ticker": "SPY", "price": 503.0},
        {"date": "2026-08-07", "ticker": "SPY", "price": 505.0},
        {"date": "2026-08-08", "ticker": "SPY", "price": 508.0},
    ]

    db = FakeDb(signals, daily_data, spy_data)

    with patch("stock_scanner.get_db", return_value=db):
        with patch("stock_scanner._fetch_all") as mock_fetch:
            def side_effect(query):
                return query.execute().data
            mock_fetch.side_effect = side_effect
            update_returns()

    updates = db._signals._updates
    assert len(updates) == 1
    u = updates[0]
    assert u["exit_price"] == 110.0
    assert u["holding_days"] == 9
    assert u["return_5d"] == pytest.approx(6.0, abs=0.1)
    assert u["benchmark_return"] == pytest.approx(1.6, abs=0.1)
    assert u["excess_return"] == pytest.approx(4.4, abs=0.1)


def test_update_returns_calculates_max_drawdown():
    signals = [
        {"id": 1, "ticker": "AAPL", "signal_date": "2026-08-01",
         "signal_price": 100.0, "return_20d": None}
    ]
    daily_data = [
        {"date": "2026-08-04", "ticker": "AAPL", "price": 95.0},
        {"date": "2026-08-05", "ticker": "AAPL", "price": 90.0},
        {"date": "2026-08-06", "ticker": "AAPL", "price": 92.0},
        {"date": "2026-08-07", "ticker": "AAPL", "price": 105.0},
        {"date": "2026-08-08", "ticker": "AAPL", "price": 108.0},
    ]

    db = FakeDb(signals, daily_data)

    with patch("stock_scanner.get_db", return_value=db):
        with patch("stock_scanner._fetch_all") as mock_fetch:
            def side_effect(query):
                return query.execute().data
            mock_fetch.side_effect = side_effect
            update_returns()

    updates = db._signals._updates
    assert len(updates) == 1
    u = updates[0]
    assert u["max_drawdown_after"] == pytest.approx(-10.0, abs=0.1)
    assert u["max_runup_after"] == pytest.approx(8.0, abs=0.1)


def test_update_returns_skips_signals_without_price():
    signals = [
        {"id": 1, "ticker": "AAPL", "signal_date": "2026-08-01",
         "signal_price": None, "return_20d": None}
    ]

    db = FakeDb(signals, [])

    with patch("stock_scanner.get_db", return_value=db):
        with patch("stock_scanner._fetch_all") as mock_fetch:
            def side_effect(query):
                return query.execute().data
            mock_fetch.side_effect = side_effect
            update_returns()

    assert len(db._signals._updates) == 0
