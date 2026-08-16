"""V6 백테스트 JSON 리포트 테스트."""

from backtest import _build_json_report


def _rec(date, ticker, score, ret5, ret10, ret20, mfe5=3.0, mae5=-1.0):
    return {
        "date": date, "ticker": ticker, "score": score, "price": 100.0,
        "ret5": ret5, "ret10": ret10, "ret20": ret20,
        "mfe5": mfe5, "mae5": mae5,
    }


def test_json_report_has_v6_bands_and_recent_signals():
    records = [
        _rec("2026-08-01", "AAPL", 70, 2.0, 3.0, 5.0),
        _rec("2026-07-25", "MSFT", 62, -1.0, None, None, .5, -2.0),
    ]
    report = _build_json_report(records, [80, 70, 60, 50, 40],
                                ["AAPL", "MSFT"], "2026-02-01", "2026-08-01", 26,
                                raw_records=records)

    assert report["version"] == "v6"
    assert report["period_start"] == "2026-02-01"
    assert report["ticker_count"] == 2
    assert [b["band"] for b in report["bands"]] == [
        "40-44","45-49","50-54","55-59","60-64","65-69","70-74","75-79","80+"
    ]
    b70 = next(b for b in report["bands"] if b["band"] == "70-74")
    assert b70["signals"] == 1
    assert b70["win_rate"] == 100.0
    assert b70["avg_5d"] == 2.0
    assert b70["avg_20d"] == 5.0
    b60 = next(b for b in report["bands"] if b["band"] == "60-64")
    assert b60["signals"] == 1
    assert b60["win_rate"] == 0.0

    assert len(report["recent_signals"]) == 2
    assert report["recent_signals"][0]["ticker"] == "AAPL"
    assert report["recent_signals"][0]["score"] == 70
    assert report["raw_signal_count"] == 2
    assert report["cooldown_signal_count"] == 2


def test_json_report_empty_band_is_none():
    records = [_rec("2026-08-01", "AAPL", 42, None, None, None, None, None)]
    report = _build_json_report(records, [40], ["AAPL"],
                                "2026-02-01", "2026-08-01", 26)
    b40 = next(b for b in report["bands"] if b["band"] == "40-44")
    assert b40["signals"] == 1
    assert b40["win_rate"] is None
    assert b40["avg_5d"] is None
    assert b40["sample_size"] == 0
