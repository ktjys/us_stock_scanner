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
        "profitMargins": 0.30,
    })
    assert result.strategy_type == "quality"
    assert result.confidence == 0.84


def test_established_growth():
    result = classify_asset("NVDA", {
        "quoteType": "EQUITY",
        "longName": "NVIDIA Corporation",
        "sector": "Technology",
        "industry": "Semiconductors",
        "marketCap": 3_000_000_000_000,
        "revenueGrowth": 0.30,
        "beta": 1.5,
    })
    assert result.strategy_type == "established_growth"
    assert result.confidence == 0.84


def test_speculative():
    # 미래사업(원자력) + 음수 수익성 + 고변동 → speculative (근거 3개 → 0.86)
    result = classify_asset("OKLO", {
        "quoteType": "EQUITY",
        "longName": "Oklo Inc.",
        "sector": "Industrials",
        "industry": "Specialty Industrial Machinery",
        "description": "Advanced nuclear power and small modular reactor company",
        "marketCap": 10_000_000_000,
        "revenueGrowth": 0.40,
        "beta": 2.2,
        "profitMargins": -0.5,
    })
    assert result.strategy_type == "speculative"
    assert result.confidence == 0.86


def test_general_equity():
    result = classify_asset("TEST", {
        "quoteType": "EQUITY",
        "longName": "Test Company",
        "sector": "",
        "industry": "",
    })
    assert result.strategy_type == "general"
    assert result.confidence == 0.55


def test_empty_ticker_rejected():
    try:
        classify_asset("  ", {})
    except ValueError:
        return
    raise AssertionError("빈 ticker는 ValueError여야 합니다.")
