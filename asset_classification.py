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
    """Yahoo Finance metadata 형태의 dict를 받아 종목군을 자동 분류한다.

    이 함수는 네트워크를 사용하지 않는 순수 함수다. 따라서 실제 Yahoo 조회는
    scanner/integration 계층에서 수행하고, 이 함수는 테스트 가능하게 유지한다.
    """
    ticker = ticker.strip().upper()
    if not ticker:
        raise ValueError("ticker가 비어 있습니다.")

    if _is_etf(info):
        strategy, confidence, reason = _classify_etf(info)
        return AssetClassification(ticker, "etf", strategy, confidence, "auto", reason)

    sector = _text(info.get("sector"))
    industry = _text(info.get("industry"))
    quote_type = _text(info.get("quoteType"))
    text = " ".join(
        _text(info.get(k))
        for k in ("longName", "shortName", "industry", "sector", "description")
    )

    beta = _num(info.get("beta"))
    market_cap = _num(info.get("marketCap"))
    revenue_growth = _num(info.get("revenueGrowth"))
    earnings_growth = _num(info.get("earningsGrowth"))

    # 핵심 아이디어: "고변동"은 단순 성장 여부와 분리해서 판단한다.
    # beta가 매우 높거나 성장성이 높으면서 변동성 특성이 강하면 별도 전략으로 보낸다.
    high_volatility = (
        (beta is not None and beta >= 1.8)
        or any(x in text for x in ("biotechnology", "biotech", "speculative"))
    )

    growth_signal = (
        (revenue_growth is not None and revenue_growth >= 0.20)
        or (earnings_growth is not None and earnings_growth >= 0.25)
        or any(x in text for x in (
            "growth", "software", "semiconductor", "internet", "ai ",
            "artificial intelligence", "nuclear", "clean energy",
        ))
    )

    # 매우 큰 시총 + 성장/기술 업종은 Quality/Blue Chip과 Growth의 경계가 있다.
    # 여기서는 장기 추매 엔진의 보수적 기본값을 위해 대형주를 quality 쪽으로 둔다.
    mega_cap = market_cap is not None and market_cap >= 100_000_000_000

    if high_volatility and growth_signal:
        return AssetClassification(
            ticker, "equity", "high_volatility_growth", 0.88, "auto",
            "높은 변동성 특성과 성장주 특성이 함께 감지됨",
        )

    if growth_signal:
        return AssetClassification(
            ticker, "equity", "growth", 0.82, "auto",
            "성장성 또는 성장 산업 특성이 감지됨",
        )

    quality_sectors = {
        "technology", "healthcare", "consumer defensive", "financial services",
        "industrials", "communication services",
    }
    quality_industries = {
        "software - infrastructure", "software - application",
        "credit services", "insurance - diversified",
        "medical care facilities", "packaged foods",
    }

    if mega_cap or sector in quality_sectors or industry in quality_industries:
        return AssetClassification(
            ticker, "equity", "quality_blue_chip", 0.72, "auto",
            "대형주 또는 장기 보유에 적합한 우량 업종 특성이 감지됨",
        )

    # quoteType이 EQUITY가 아니거나 메타데이터가 부족해도 스캐너가 멈추지 않도록
    # 가장 보수적인 일반 주식 전략으로 fallback한다.
    if quote_type in ("equity", "stock", ""):
        return AssetClassification(
            ticker, "equity", "general_equity", 0.55, "auto",
            "명확한 ETF/성장/우량주 분류 근거가 부족함",
        )

    return AssetClassification(
        ticker, "other", "general_equity", 0.40, "auto",
        "지원되지 않는 자산 유형이므로 보수적으로 일반 전략 적용",
    )
