"""scan() 파이프라인과 알림 메시지 테스트."""

from stock_scanner import build_alert_message, scan


def test_build_alert_message_sorted_and_formatted():
    cands = [
        {"ticker": "B", "score": 70, "price": 99.5, "rsi": 32.0,
         "drawdown": -11.0, "conditions": ["RSI<35 과매도", "고점대비-10%"]},
        {"ticker": "A", "score": 85, "price": 123.45, "rsi": 28.0,
         "drawdown": -20.0, "conditions": ["RSI<35 과매도"]},
    ]
    msg = build_alert_message(cands, "2026-08-13")
    assert "📅 2026-08-13" in msg
    assert "🔥 A 85점" in msg
    assert "🟢 B 70점" in msg
    assert "가격 $123.45" in msg
    assert "고점대비 -20.0%" in msg
    assert "조건: RSI<35 과매도, 고점대비-10%" in msg
    assert msg.index("🔥 A") < msg.index("🟢 B")


def test_scan_no_db_no_notify_returns_candidates(monkeypatch):
    fake = {"ticker": "AAPL", "score": 80, "price": 100.0, "rsi": 30.0,
            "drawdown": -12.0, "conditions": ["RSI<35 과매도"]}
    monkeypatch.setattr("stock_scanner.analyze", lambda t, date=None: fake)
    monkeypatch.setattr("stock_scanner.load_watchlist", lambda db=None: ["AAPL"])
    monkeypatch.setattr("stock_scanner.time.sleep", lambda s: None)

    cands, failures = scan(persist=False, notify=False)
    assert [c["ticker"] for c in cands] == ["AAPL"]
    assert failures == []


def test_scan_filters_below_alert_score(capsys, monkeypatch):
    fake = {"ticker": "MSFT", "score": 40, "price": 100.0, "rsi": 50.0,
            "drawdown": -5.0, "conditions": []}
    monkeypatch.setattr("stock_scanner.analyze", lambda t, date=None: fake)
    monkeypatch.setattr("stock_scanner.load_watchlist", lambda db=None: ["MSFT"])
    monkeypatch.setattr("stock_scanner.time.sleep", lambda s: None)

    cands, failures = scan(persist=False, notify=False)
    assert cands == []
    assert failures == []
    # 0건이어도 텔레그램 대신 로그로는 남겨야 한다
    assert "후보 0건" in capsys.readouterr().out


def test_scan_collects_failures(capsys, monkeypatch):
    def fake_analyze(t, date=None):
        if t == "BAD":
            raise RuntimeError("network")
        return {"ticker": t, "score": 80, "price": 100.0, "rsi": 30.0,
                "drawdown": -12.0, "conditions": ["RSI<35 과매도"]}

    monkeypatch.setattr("stock_scanner.analyze", fake_analyze)
    monkeypatch.setattr("stock_scanner.load_watchlist", lambda db=None: ["BAD", "GOOD"])
    monkeypatch.setattr("stock_scanner.time.sleep", lambda s: None)

    cands, failures = scan(persist=False, notify=False)
    assert [c["ticker"] for c in cands] == ["GOOD"]
    assert failures == ["BAD"]
    assert "BAD 오류: network" in capsys.readouterr().out


def test_scan_threshold_excludes_below(monkeypatch):
    """임계값 이하 점수는 후보에서 제외되어야 한다."""
    fake = {"ticker": "LOW", "score": 30, "price": 100.0, "rsi": 50.0,
            "drawdown": -5.0, "conditions": []}
    monkeypatch.setattr("stock_scanner.analyze", lambda t, date=None: fake)
    monkeypatch.setattr("stock_scanner.load_watchlist", lambda db=None: ["LOW"])
    monkeypatch.setattr("stock_scanner.time.sleep", lambda s: None)

    cands, failures = scan(persist=False, notify=False, threshold=80)
    assert [c["ticker"] for c in cands] == []
    assert failures == []


def test_scan_threshold_includes_above(monkeypatch):
    """임계값 이상 점수는 후보에 포함되어야 한다."""
    fake = {"ticker": "HIGH", "score": 90, "price": 100.0, "rsi": 30.0,
            "drawdown": -12.0, "conditions": ["RSI<35 과매도"]}
    monkeypatch.setattr("stock_scanner.analyze", lambda t, date=None: fake)
    monkeypatch.setattr("stock_scanner.load_watchlist", lambda db=None: ["HIGH"])
    monkeypatch.setattr("stock_scanner.time.sleep", lambda s: None)

    cands, failures = scan(persist=False, notify=False, threshold=80)
    assert [c["ticker"] for c in cands] == ["HIGH"]
    assert failures == []