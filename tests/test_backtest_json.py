"""backtest.py JSON 리포트 생성 테스트 (네트워크 fetch 없음)."""

from backtest import _build_json_report, build_backtest_summary


def test_json_report_schema_and_values():
    records = [
        {"date": "2026-08-01", "ticker": "AAPL", "score": 70, "price": 100.0,
         "ret5": 2.0, "ret10": 3.0, "ret20": 5.0, "threshold": 65,
         "mfe5": 3.0, "mae5": -1.0},
        {"date": "2026-08-01", "ticker": "AAPL", "score": 70, "price": 100.0,
         "ret5": 2.0, "ret10": 3.0, "ret20": 5.0, "threshold": 60,
         "mfe5": 3.0, "mae5": -1.0},
        {"date": "2026-07-25", "ticker": "MSFT", "score": 62, "price": 50.0,
         "ret5": -1.0, "ret10": None, "ret20": None, "threshold": 60,
         "mfe5": 0.5, "mae5": -2.0},
    ]
    report = _build_json_report(records, [65, 60], ["AAPL", "MSFT"],
                                "2026-02-01", "2026-08-01", 26)

    assert set(report) == {"version", "generated_at", "period_start",
                           "period_end", "weeks", "ticker_count", "tickers",
                           "thresholds", "recent_signals"}
    assert report["version"] == "v5"
    assert isinstance(report["generated_at"], str)
    assert report["period_start"] == "2026-02-01"
    assert report["period_end"] == "2026-08-01"
    assert report["weeks"] == 26
    assert report["ticker_count"] == 2
    assert report["tickers"] == ["AAPL", "MSFT"]

    # threshold 내림차순
    assert [t["threshold"] for t in report["thresholds"]] == [65, 60]
    t65 = report["thresholds"][0]
    assert t65["signals"] == 1
    assert t65["win_rate"] == 100.0
    assert t65["avg_5d"] == 2.0
    assert t65["avg_10d"] == 3.0
    assert t65["avg_20d"] == 5.0
    assert t65["avg_mae_5d"] == -1.0
    assert t65["avg_mfe_5d"] == 3.0
    assert t65["sample_size"] == 1
    t60 = report["thresholds"][1]
    assert t60["signals"] == 2
    assert t60["win_rate"] == 50.0
    assert t60["avg_5d"] == 0.5
    assert t60["avg_10d"] == 3.0
    assert t60["avg_20d"] == 5.0
    assert t60["avg_mae_5d"] == -1.5
    assert t60["avg_mfe_5d"] == 1.75
    assert t60["sample_size"] == 2

    # (date, ticker) 중복 제거, 날짜 내림차순
    assert len(report["recent_signals"]) == 2
    assert [s["date"] for s in report["recent_signals"]] == ["2026-08-01", "2026-07-25"]
    assert report["recent_signals"][0]["ticker"] == "AAPL"
    assert report["recent_signals"][0]["score"] == 70
    assert report["recent_signals"][0]["mae5"] == -1.0
    assert report["recent_signals"][0]["mfe5"] == 3.0
    assert report["recent_signals"][1]["ret10"] is None


def test_json_report_empty_samples_and_dedup_highest_score():
    records = [
        {"date": "2026-08-01", "ticker": "AAPL", "score": 70, "price": 100.0,
         "ret5": None, "ret10": None, "ret20": None, "threshold": 65,
         "mfe5": None, "mae5": None},
        {"date": "2026-08-01", "ticker": "AAPL", "score": 70, "price": 100.0,
         "ret5": None, "ret10": None, "ret20": None, "threshold": 60,
         "mfe5": None, "mae5": None},
        {"date": "2026-08-01", "ticker": "AAPL", "score": 55, "price": 100.0,
         "ret5": 9.0, "ret10": 9.0, "ret20": 9.0, "threshold": 55,
         "mfe5": 10.0, "mae5": -1.0},
        {"date": "2026-08-01", "ticker": "MSFT", "score": 55, "price": 50.0,
         "ret5": 1.0, "ret10": 2.0, "ret20": 3.0, "threshold": 55,
         "mfe5": 3.0, "mae5": -2.0},
    ]
    report = _build_json_report(records, [65, 60, 55], ["AAPL", "MSFT"],
                                "2026-02-01", "2026-08-01", 26)

    t65 = report["thresholds"][0]
    assert t65["signals"] == 1
    assert t65["win_rate"] is None
    assert t65["avg_5d"] is None
    assert t65["avg_10d"] is None
    assert t65["avg_20d"] is None
    assert t65["avg_mae_5d"] is None
    assert t65["avg_mfe_5d"] is None
    assert t65["sample_size"] == 0

    # 같은 (date, ticker)는 가장 높은 score만 남는다
    assert len(report["recent_signals"]) == 2
    aapl = next(s for s in report["recent_signals"] if s["ticker"] == "AAPL")
    assert aapl["score"] == 70
    assert aapl["ret5"] is None
    assert aapl["mae5"] is None


def test_build_backtest_summary_format(monkeypatch):
    fake = {
        "records": [
            {"threshold": 65, "ret5": 1.0, "ret10": 2.0, "ret20": 3.0,
             "mfe5": 3.0, "mae5": -1.0},
            {"threshold": 65, "ret5": -2.0, "ret10": -1.0, "ret20": 0.0,
             "mfe5": 1.0, "mae5": -2.0},
            {"threshold": 60, "ret5": None, "ret10": None, "ret20": None,
             "mfe5": None, "mae5": None},
            {"threshold": 60, "ret5": None, "ret10": None, "ret20": None,
             "mfe5": None, "mae5": None},
        ],
        "tickers": ["AAPL", "MSFT"],
        "start": "2026-02-01", "end": "2026-08-01", "weeks": 26,
    }
    monkeypatch.setattr("backtest._run_backtest",
                        lambda thresholds, weeks, tickers: fake)
    text = build_backtest_summary(weeks=26)
    assert "📊 백테스트 (최근 26주, 2종목)" in text
    # V5 기본 임계값 80/75/70/65/60/55/50/45/40 순서로 출력된다
    assert "80점: 0건 | 데이터 부족" in text
    assert "75점: 0건 | 데이터 부족" in text
    assert "70점: 0건 | 데이터 부족" in text
    assert "65점: 2건 | 승률 50.0%" in text
    assert "60점: 2건 | 데이터 부족" in text
    assert "55점: 0건 | 데이터 부족" in text
    assert "50점: 0건 | 데이터 부족" in text
    assert "45점: 0건 | 데이터 부족" in text
    assert "40점: 0건 | 데이터 부족" in text


def test_build_backtest_summary_no_data(monkeypatch):
    monkeypatch.setattr("backtest._run_backtest",
                        lambda *a, **k: {"records": [], "tickers": [],
                                         "start": "", "end": "", "weeks": 26})
    assert build_backtest_summary() == "백테스트 데이터 없음"


def test_build_backtest_summary_exception_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("backtest._run_backtest", boom)
    assert build_backtest_summary() == ""