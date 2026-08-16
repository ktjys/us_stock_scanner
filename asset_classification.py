"""종목 자동 분류 엔진.

기존 V7 Scanner의 scoring/signal 로직과 독립적으로 동작한다.
Yahoo Finance에서 얻은 메타데이터를 표준화한 뒤, 장기 투자용 전략 그룹을 결정한다.

분류 우선순위:
  1. ETF
  2. 고변동 성장주
  3. 성장주
  4. Quality / Blue Chip
  5. 일반 주식

자동 분류 결과는 "기본값"이며, DB의 사용자 override가 있으면 애플리케이션 계층에서
override를 우선 적용할 수 있도록 classification_source를 함께 반환한다.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


ETF_STRATEGIES = {
    "broad_market_etf",
    "growth_etf",
    "dividend_etf",
    "sector_etf",
    "income_etf",
    "other_etf",
}

EQUITY_STRATEGIES = {
    "quality_blue_chip",
    "growth",
    "high_volatility_growth",
    "general_equity",
}


@dataclass(frozen=True)
class AssetClassification:
    ticker: str
    asset_type: str
    strategy_type: str
    confidence: float
    classification_source: str = "auto"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_etf(info: dict[str, Any]) -> bool:
    quote_type = _text(info.get("quoteType"))
    return quote_type == "etf" or bool(info.get("fundFamily"))


def _classify_etf(info: dict[str, Any]) -> tuple[str, float, str]:
    text = " ".join(
        _text(info.get(k))
        for k in ("longName", "shortName", "category", "fundCategory", "description")
    )

    if any(x in text for x in ("dividend", "dividends", "high dividend")):
        return "dividend_etf", 0.90, "배당 중심 ETF"

    if any(x in text for x in ("income", "covered call", "option income", "enhanced income")):
        return "income_etf", 0.88, "인컴/커버드콜 중심 ETF"

    if any(x in text for x in ("technology", "semiconductor", "semiconductors", "financial",
                               "healthcare", "energy", "industrial", "real estate",
                               "sector")):
        return "sector_etf", 0.84, "특정 섹터 중심 ETF"

    if any(x in text for x in ("growth", "nasdaq", "innovation")):
        return "growth_etf", 0.86, "성장주/성장지수 중심 ETF"

    if any(x in text for x in ("s&p 500", "total market", "total stock", "large blend",
                               "large-cap blend", "broad market", "russell 3000")):
        return "broad_market_etf", 0.95, "광범위한 시장지수 ETF"

    return "other_etf", 0.65, "ETF이나 세부 전략 자동 식별 정보 부족"


def classify_asset(ticker: str, info: dict[str, Any]) -> AssetClassification:
    """Yahoo Finance metadata를 기반으로 장기투자용 전략군을 자동 분류한다.

    전략군은 투자 역할을 나타내며, 세부 위험/변동성 특성은
    이후 Opportunity Engine에서 별도로 평가한다.
    """

    ticker = ticker.strip().upper()
    if not ticker:
        raise ValueError("ticker가 비어 있습니다.")

    # ---------------------------------------------------------
    # 1. ETF
    # ---------------------------------------------------------
    if _is_etf(info):
        strategy, confidence, reason = _classify_etf(info)
        return AssetClassification(
            ticker,
            "etf",
            strategy,
            confidence,
            "auto",
            reason,
        )

    # ---------------------------------------------------------
    # 2. 기본 metadata
    # ---------------------------------------------------------
    sector = _text(info.get("sector"))
    industry = _text(info.get("industry"))
    quote_type = _text(info.get("quoteType"))

    beta = _num(info.get("beta"))
    market_cap = _num(info.get("marketCap"))
    revenue_growth = _num(info.get("revenueGrowth"))
    earnings_growth = _num(info.get("earningsGrowth"))

    # ---------------------------------------------------------
    # 3. 데이터 상태
    # ---------------------------------------------------------
    mega_cap = (
        market_cap is not None
        and market_cap >= 100_000_000_000
    )

    large_cap = (
        market_cap is not None
        and market_cap >= 50_000_000_000
    )

    high_volatility = (
        beta is not None
        and beta >= 1.8
    )

    # ---------------------------------------------------------
    # 4. 강한 성장 신호
    #
    # 산업명이 Technology / Software / Semiconductor라는 이유만으로
    # Growth로 분류하지 않는다.
    #
    # 실제 성장률을 우선한다.
    # ---------------------------------------------------------
    strong_revenue_growth = (
        revenue_growth is not None
        and revenue_growth >= 0.20
    )

    strong_earnings_growth = (
        earnings_growth is not None
        and earnings_growth >= 0.50
    )

    established_growth_signal = (
        large_cap
        and (
            strong_revenue_growth
            or strong_earnings_growth
        )
    )

    # ---------------------------------------------------------
    # 5. Speculative / Emerging
    #
    # 현재 실적이 충분히 확인되지 않고 미래 사업에 대한
    # 기대 의존도가 높은 종목을 별도로 분리한다.
    # ---------------------------------------------------------
    speculative_industries = {
        "utilities - independent power producers",
    }

    speculative_keywords = (
        "nuclear",
        "small modular reactor",
        "advanced reactor",
        "pre-revenue",
        "development stage",
    )

    missing_growth_data = (
        revenue_growth is None
        and earnings_growth is None
    )

    speculative_signal = (
        industry in speculative_industries
        or any(
            keyword in " ".join(
                _text(info.get(k)).lower()
                for k in (
                    "longName",
                    "shortName",
                    "industry",
                    "sector",
                    "description",
                )
            )
            for keyword in speculative_keywords
        )
        or (
            market_cap is not None
            and market_cap < 20_000_000_000
            and missing_growth_data
            and high_volatility
        )
    )

    if speculative_signal:
        return AssetClassification(
            ticker,
            "equity",
            "speculative",
            0.78,
            "auto",
            "사업 성숙도 또는 실적 가시성이 낮아 미래 성장 기대에 대한 의존도가 높은 종목으로 판단됨",
        )

    # ---------------------------------------------------------
    # 6. Established Growth
    #
    # Quality보다 성장성이 투자 논리에서 더 중요한
    # 대형/성숙 성장주.
    #
    # 반드시 Quality보다 먼저 판단한다.
    # ---------------------------------------------------------
    if established_growth_signal:
        confidence = 0.84

        # beta가 매우 높으면 분류는 Established Growth를 유지하되
        # confidence를 조금 낮춘다.
        if high_volatility:
            confidence = 0.80

        return AssetClassification(
            ticker,
            "equity",
            "established_growth",
            confidence,
            "auto",
            "충분한 사업 규모와 뚜렷한 매출 또는 이익 성장성이 확인됨",
        )

    # ---------------------------------------------------------
    # 7. Quality
    #
    # 성장률이 특별히 높지는 않지만 규모와 사업 성숙도가
    # 장기 보유에 적합한 대형/우량주를 분류한다.
    # ---------------------------------------------------------
    quality_sectors = {
        "technology",
        "healthcare",
        "consumer defensive",
        "financial services",
        "industrials",
        "communication services",
    }

    quality_industries = {
        "software - infrastructure",
        "software - application",
        "credit services",
        "insurance - diversified",
        "medical care facilities",
        "packaged foods",
        "information technology services",
        "consumer electronics",
    }

    quality_signal = (
        mega_cap
        and (
            sector in quality_sectors
            or industry in quality_industries
        )
    )

    if quality_signal:
        return AssetClassification(
            ticker,
            "equity",
            "quality",
            0.84,
            "auto",
            "대형 규모와 성숙한 사업 기반을 갖춘 장기 보유 우량주 특성이 감지됨",
        )

    # ---------------------------------------------------------
    # 8. General Equity
    # ---------------------------------------------------------
    if quote_type in ("equity", "stock", ""):
        return AssetClassification(
            ticker,
            "equity",
            "general",
            0.55,
            "auto",
            "명확한 Quality/Growth/Speculative 분류 근거가 부족함",
        )

    # ---------------------------------------------------------
    # 9. Unsupported asset
    # ---------------------------------------------------------
    return AssetClassification(
        ticker,
        "other",
        "general",
        0.40,
        "auto",
        "지원되지 않는 자산 유형이므로 보수적으로 일반 전략 적용",
    )
