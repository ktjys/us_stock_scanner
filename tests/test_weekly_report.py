"""주간 리포트 텍스트 생성 테스트."""

from weekly_report import build_report_text


def test_empty_rows():
    assert build_report_text([]) == (
        "📈 미국주식 매수신호 주간 리포트\n\n아직 신호 데이터가 없습니다."
    )


def test_report_stats_and_missing_data():
    rows = [
        {"return_5d": 3.0, "return_10d": 5.0, "return_20d": None},
        {"return_5d": -1.0, "return_10d": 2.0, "return_20d": None},
    ]
    text = build_report_text(rows)
    assert "누적 신호: 2개" in text
    assert "5일: 평균 +1.00% | 승률 50.0% | 표본 2" in text
    assert "10일: 평균 +3.50% | 승률 100.0% | 표본 2" in text
    assert "20일: 데이터 부족" in text


def test_weeks_filter_excludes_old_rows():
    rows = [
        {"signal_date": "2026-01-05", "return_5d": 10.0, "return_10d": 10.0, "return_20d": 10.0},
        {"signal_date": "2026-08-01", "return_5d": 2.0, "return_10d": 2.0, "return_20d": 2.0},
    ]
    text = build_report_text(rows, weeks=4)
    assert "누적 신호: 1개" in text
    assert "5일: 평균 +2.00% | 승률 100.0% | 표본 1" in text


def test_weeks_none_includes_all_rows():
    rows = [
        {"signal_date": "2026-01-05", "return_5d": 10.0, "return_10d": 10.0, "return_20d": 10.0},
        {"signal_date": "2026-08-01", "return_5d": 2.0, "return_10d": 2.0, "return_20d": 2.0},
    ]
    text = build_report_text(rows, weeks=None)
    assert "누적 신호: 2개" in text
    assert "5일: 평균 +6.00% | 승률 100.0% | 표본 2" in text


def test_weeks_filter_excludes_rows_without_signal_date():
    rows = [
        {"signal_date": "2026-08-01", "return_5d": 2.0, "return_10d": 2.0, "return_20d": 2.0},
        {"return_5d": 5.0, "return_10d": 5.0, "return_20d": 5.0},
    ]
    text = build_report_text(rows, weeks=4)
    assert "누적 신호: 1개" in text
    assert "5일: 평균 +2.00% | 승률 100.0% | 표본 1" in text


def test_report_appends_backtest_summary():
    rows = [{"return_5d": 3.0, "return_10d": 5.0, "return_20d": None}]
    text = build_report_text(rows, backtest_summary="📊 백테스트 (최근 26주, 21종목)")
    assert text.endswith("\n\n📊 백테스트 (최근 26주, 21종목)")


def test_report_skips_backtest_section_when_empty():
    rows = [{"return_5d": 3.0, "return_10d": 5.0, "return_20d": None}]
    text = build_report_text(rows, backtest_summary="")
    assert "백테스트" not in text


def test_report_empty_rows_with_backtest_summary():
    text = build_report_text([], backtest_summary="📊 백테스트 (최근 26주, 21종목)")
    assert "아직 신호 데이터가 없습니다." in text
    assert text.endswith("📊 백테스트 (최근 26주, 21종목)")
