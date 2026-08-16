"""V8 Phase 1 자동 분류 엔진 테스트."""

from asset_classification import classify_asset


def test_broad_market_etf():
    result = classify_asset("VOO", {
        "quoteType": "ETF",
        "fundFamily": "Vanguard",
        "longName": "Vanguard S&P 500 ETF",
        "category": "Large Blend",
    })
    assert result.asset_type == "etf"
    assert result.strategy_type == "broad_market_etf"
    assert result.classification_source == "auto"


def test_growth_etf():
    result = classify_asset("QQQM", {
        "quoteType": "ETF",
        "fundFamily": "Invesco",
        "longName": "Invesco NASDAQ 100 ETF",
        "category": "Large Growth",
    })
    assert result.strategy_type == "growth_etf"


def test_dividend_etf():
    result = classify_asset("SCHD", {
        "quoteType": "ETF",
        "fundFamily": "Schwab",
        "longName": "Schwab U.S. Dividend Equity ETF",
    })
    assert result.strategy_type == "dividend_etf"


def test_income_etf():
    result = classify_asset("JEPQ", {
        "quoteType": "ETF",
        "fundFamily": "JPMorgan",
        "longName": "JPMorgan Nasdaq Equity Premium Income ETF",
        "description": "Covered call income strategy",
    })
    assert result.strategy_type == "income_etf"


def test_quality_large_cap():
    result = classify_asset("MSFT", {
        "quoteType": "EQUITY",
        "longName": "Microsoft Corporation",
        "sector": "Technology",
        "industry": "Software - Infrastructure",
        "marketCap": 3_000_000_000_000,
        "beta": 1.0,
    })
    assert result.strategy_type == "quality_blue_chip"


def test_growth_equity():
    result = classify_asset("NVDA", {
        "quoteType": "EQUITY",
        "longName": "NVIDIA Corporation",
        "sector": "Technology",
        "industry": "Semiconductors",
        "marketCap": 3_000_000_000_000,
        "revenueGrowth": 0.30,
        "beta": 1.5,
    })
    assert result.strategy_type == "growth"


def test_high_volatility_growth():
    result = classify_asset("OKLO", {
        "quoteType": "EQUITY",
        "longName": "Oklo Inc.",
        "sector": "Industrials",
        "industry": "Specialty Industrial Machinery",
        "marketCap": 10_000_000_000,
        "revenueGrowth": 0.40,
        "beta": 2.2,
    })
    assert result.strategy_type == "high_volatility_growth"


def test_fallback_equity():
    result = classify_asset("TEST", {
        "quoteType": "EQUITY",
        "longName": "Test Company",
        "sector": "",
        "industry": "",
    })
    assert result.strategy_type == "general_equity"


def test_empty_ticker_rejected():
    try:
        classify_asset("  ", {})
    except ValueError:
        return
    raise AssertionError("빈 ticker는 ValueError여야 합니다.")
