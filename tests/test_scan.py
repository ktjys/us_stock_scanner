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

    cands, failures = scan(persist=False, notify=False)
    assert [c["ticker"] for c in cands] == ["AAPL"]
    assert failures == []


def test_scan_filters_below_alert_score(capsys, monkeypatch):
    fake = {"ticker": "MSFT", "score": 40, "price": 100.0, "rsi": 50.0,
            "drawdown": -5.0, "conditions": []}
    monkeypatch.setattr("stock_scanner.analyze", lambda t, date=None: fake)
    monkeypatch.setattr("stock_scanner.load_watchlist", lambda db=None: ["MSFT"])

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

    cands, failures = scan(persist=False, notify=False, threshold=80)
    assert [c["ticker"] for c in cands] == []
    assert failures == []


def test_scan_threshold_includes_above(monkeypatch):
    """임계값 이상 점수는 후보에 포함되어야 한다."""
    fake = {"ticker": "HIGH", "score": 90, "price": 100.0, "rsi": 30.0,
            "drawdown": -12.0, "conditions": ["RSI<35 과매도"]}
    monkeypatch.setattr("stock_scanner.analyze", lambda t, date=None: fake)
    monkeypatch.setattr("stock_scanner.load_watchlist", lambda db=None: ["HIGH"])

    cands, failures = scan(persist=False, notify=False, threshold=80)
    assert [c["ticker"] for c in cands] == ["HIGH"]
    assert failures == []


def test_scan_parallel_processes_all_and_collects_failures(capsys, monkeypatch):
    """병렬 스캔이 전 종목을 처리하고 실패 ticker를 failures에 수집한다."""
    def fake_analyze(t, date=None):
        if t == "BAD":
            raise RuntimeError("network")
        return {"ticker": t, "score": 80, "price": 100.0, "rsi": 30.0,
                "drawdown": -12.0, "conditions": ["RSI<35 과매도"]}

    monkeypatch.setattr("stock_scanner.analyze", fake_analyze)
    monkeypatch.setattr("stock_scanner.load_watchlist",
                        lambda db=None: ["AAPL", "BAD", "MSFT", "NVDA"])

    cands, failures = scan(persist=False, notify=False)
    assert [c["ticker"] for c in cands] == ["AAPL", "MSFT", "NVDA"]
    assert failures == ["BAD"]
    assert "BAD 오류: network" in capsys.readouterr().out


def test_scan_analyzes_concurrently(monkeypatch):
    """analyze가 병렬 스레드에서 동시에 실행되는지 검증한다."""
    import threading
    import time

    lock = threading.Lock()
    active, peak = 0, 0

    def fake_analyze(t, date=None):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return {"ticker": t, "score": 80, "price": 100.0, "rsi": 30.0,
                "drawdown": -12.0, "conditions": ["RSI<35 과매도"]}

    monkeypatch.setattr("stock_scanner.analyze", fake_analyze)
    monkeypatch.setattr("stock_scanner.load_watchlist",
                        lambda db=None: ["AAPL", "MSFT", "NVDA"])

    cands, failures = scan(persist=False, notify=False)
    assert [c["ticker"] for c in cands] == ["AAPL", "MSFT", "NVDA"]
    assert failures == []
    assert peak >= 2  # 순차 실행이었다면 peak는 항상 1
