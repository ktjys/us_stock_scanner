"""미국 시장 휴일 판정과 scan() 휴일 스킵 동작 테스트."""

import datetime

from stock_scanner import is_us_market_holiday, scan


def _d(y, m, day):
    return datetime.date(y, m, day)


# ---------------------------------------------------------------------------
# 평일 정상일 / 주말
# ---------------------------------------------------------------------------


def test_normal_weekday_is_not_holiday():
    assert not is_us_market_holiday(_d(2026, 8, 13))  # 목요일
    assert not is_us_market_holiday(_d(2026, 4, 6))   # 굿 프라이데이 다음 월요일


def test_weekend_is_not_holiday():
    assert not is_us_market_holiday(_d(2026, 8, 15))  # 토요일
    assert not is_us_market_holiday(_d(2026, 8, 16))  # 일요일


# ---------------------------------------------------------------------------
# 고정 휴일
# ---------------------------------------------------------------------------


def test_fixed_holidays():
    assert is_us_market_holiday(_d(2026, 1, 1))    # 신정
    assert is_us_market_holiday(_d(2026, 6, 19))   # 준틴스
    assert is_us_market_holiday(_d(2026, 12, 25))  # 크리스마스


# ---------------------------------------------------------------------------
# 이동 휴일 (2026년 기준)
# ---------------------------------------------------------------------------


def test_moving_holidays_2026():
    assert is_us_market_holiday(_d(2026, 1, 19))   # MLK 데이 (1월 셋째 월요일)
    assert is_us_market_holiday(_d(2026, 2, 16))   # 대통령의 날 (2월 셋째 월요일)
    assert is_us_market_holiday(_d(2026, 5, 25))   # 메모리얼 데이 (5월 마지막 월요일)
    assert is_us_market_holiday(_d(2026, 9, 7))    # 노동절 (9월 첫째 월요일)
    assert is_us_market_holiday(_d(2026, 11, 26))  # 추수감사절 (11월 넷째 목요일)


def test_good_friday():
    assert is_us_market_holiday(_d(2026, 4, 3))  # 부활절(4/5) 전 금요일


# ---------------------------------------------------------------------------
# NYSE 미휴장일 (USFederalHolidayCalendar와의 차이)
# ---------------------------------------------------------------------------


def test_columbus_and_veterans_day_are_not_holidays():
    # NYSE는 콜럼버스 데이/재향군인의 날에 정상 개장한다.
    assert not is_us_market_holiday(_d(2026, 10, 12))
    assert not is_us_market_holiday(_d(2026, 11, 11))


def test_observed_holiday_when_fixed_date_on_weekend():
    # 독립기념일(7/4)이 토요일인 2026년은 7/3(금)로 대체 휴장.
    assert is_us_market_holiday(_d(2026, 7, 3))
    assert not is_us_market_holiday(_d(2026, 7, 4))


# ---------------------------------------------------------------------------
# scan() 휴일 스킵
# ---------------------------------------------------------------------------


def test_scan_skips_on_holiday_before_any_logic(capsys, monkeypatch):
    """휴일이면 watchlist 조회/분석/DB 클라이언트 생성 없이 빈 결과를 반환한다."""
    def boom(*args, **kwargs):
        raise AssertionError("휴일에는 스캔 로직이 실행되면 안 됨")

    monkeypatch.setattr("stock_scanner.is_us_market_holiday", lambda d: True)
    monkeypatch.setattr("stock_scanner.load_watchlist", boom)
    monkeypatch.setattr("stock_scanner.analyze", boom)
    monkeypatch.setattr("stock_scanner.get_db", boom)

    cands, failures = scan(persist=True, notify=True)
    assert cands == []
    assert failures == []
    assert "미국 시장 휴일 — 스캔 생략" in capsys.readouterr().out


def test_scan_skips_on_holiday_with_date(capsys, monkeypatch):
    monkeypatch.setattr("stock_scanner.is_us_market_holiday", lambda d: True)
    monkeypatch.setattr("stock_scanner.load_watchlist", lambda db=None: ["AAPL"])
    monkeypatch.setattr("stock_scanner.analyze", lambda t, date=None: None)

    cands, failures = scan(persist=False, notify=False)
    assert cands == []
    assert failures == []
    out = capsys.readouterr().out
    assert "미국 시장 휴일 — 스캔 생략" in out
    assert "후보 0건" not in out


def test_scan_proceeds_on_normal_weekday(monkeypatch):
    fake = {"ticker": "AAPL", "score": 80, "price": 100.0, "rsi": 30.0,
            "drawdown": -12.0, "conditions": ["RSI<35 과매도"]}
    monkeypatch.setattr("stock_scanner.is_us_market_holiday", lambda d: False)
    monkeypatch.setattr("stock_scanner.analyze", lambda t, date=None: fake)
    monkeypatch.setattr("stock_scanner.load_watchlist", lambda db=None: ["AAPL"])

    cands, failures = scan(persist=False, notify=False)
    assert [c["ticker"] for c in cands] == ["AAPL"]
    assert failures == []
