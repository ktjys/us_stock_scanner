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
    "quality",
    "established_growth",
    "speculative",
    "general",
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

    분류 우선순위:

        ETF
          ↓
        Speculative / Emerging
          ↓
        Established Growth
          ↓
        Quality
          ↓
        General

    중요한 원칙:
      - ticker별 하드코딩을 하지 않는다.
      - 산업명 하나만으로 Speculative를 결정하지 않는다.
      - 성장률이 높더라도 수익성이 낮고 미래사업 의존도가 높으면
        Speculative로 분류할 수 있다.
      - 세부 위험/변동성은 이후 Opportunity Engine에서 평가한다.
    """

    ticker = ticker.strip().upper()

    if not ticker:
        raise ValueError("ticker가 비어 있습니다.")

    info = info or {}

    # =========================================================
    # 1. ETF
    # =========================================================
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

    # =========================================================
    # 2. 기본 metadata
    # =========================================================
    sector = _text(info.get("sector")).strip().lower()
    industry = _text(info.get("industry")).strip().lower()
    quote_type = _text(info.get("quoteType")).strip().lower()

    long_name = _text(info.get("longName")).strip().lower()
    short_name = _text(info.get("shortName")).strip().lower()
    description = _text(info.get("description")).strip().lower()

    beta = _num(info.get("beta"))
    market_cap = _num(info.get("marketCap"))

    revenue_growth = _num(info.get("revenueGrowth"))
    earnings_growth = _num(info.get("earningsGrowth"))

    # ---------------------------------------------------------
    # 수익성 / 밸류에이션
    #
    # Yahoo metadata가 없는 경우 None으로 처리한다.
    # ---------------------------------------------------------
    profit_margin = _num(info.get("profitMargins"))
    operating_margin = _num(info.get("operatingMargins"))
    return_on_equity = _num(info.get("returnOnEquity"))

    trailing_eps = _num(info.get("trailingEps"))
    forward_eps = _num(info.get("forwardEps"))

    trailing_pe = _num(info.get("trailingPE"))
    forward_pe = _num(info.get("forwardPE"))

    price_to_sales = _num(info.get("priceToSalesTrailing12Months"))
    price_to_book = _num(info.get("priceToBook"))

    # =========================================================
    # 3. 통합 metadata
    # =========================================================
    metadata_text = " ".join(
        value
        for value in (
            long_name,
            short_name,
            industry,
            sector,
            description,
        )
        if value
    )

    # =========================================================
    # 4. 회사 규모
    # =========================================================
    mega_cap = (
        market_cap is not None
        and market_cap >= 100_000_000_000
    )

    large_cap = (
        market_cap is not None
        and market_cap >= 50_000_000_000
    )

    mid_cap = (
        market_cap is not None
        and 2_000_000_000 <= market_cap < 50_000_000_000
    )

    small_cap = (
        market_cap is not None
        and market_cap < 20_000_000_000
    )

    # =========================================================
    # 5. 변동성
    # =========================================================
    high_volatility = (
        beta is not None
        and beta >= 1.8
    )

    very_high_volatility = (
        beta is not None
        and beta >= 2.2
    )

    # =========================================================
    # 6. 성장성
    # =========================================================
    strong_revenue_growth = (
        revenue_growth is not None
        and revenue_growth >= 0.20
    )

    very_strong_revenue_growth = (
        revenue_growth is not None
        and revenue_growth >= 0.50
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

    # =========================================================
    # 7. 수익성 상태
    # =========================================================

    negative_profitability = (
        (
            profit_margin is not None
            and profit_margin < 0
        )
        or (
            operating_margin is not None
            and operating_margin < 0
        )
        or (
            trailing_eps is not None
            and trailing_eps < 0
        )
    )

    weak_profitability = (
        profit_margin is not None
        and profit_margin < 0.05
    )

    strong_profitability = (
        profit_margin is not None
        and profit_margin >= 0.15
    )

    positive_eps = (
        trailing_eps is not None
        and trailing_eps > 0
    )

    # =========================================================
    # 8. 성장 데이터 가시성
    # =========================================================
    missing_growth_data = (
        revenue_growth is None
        and earnings_growth is None
    )

    weak_growth_data = (
        (
            revenue_growth is None
            or revenue_growth < 0.10
        )
        and (
            earnings_growth is None
            or earnings_growth < 0.20
        )
    )

    # =========================================================
    # 9. 미래사업 / Emerging 산업
    #
    # 산업명 하나만으로 판정하지 않는다.
    # =========================================================

    speculative_industries = {
        "utilities - independent power producers",
        "uranium",
        "nuclear",
    }

    speculative_industry_signal = (
        industry in speculative_industries
    )

    # ---------------------------------------------------------
    # 미래사업 관련 키워드
    # ---------------------------------------------------------
    speculative_keywords = (
        # Nuclear / advanced energy
        "nuclear",
        "small modular reactor",
        "advanced reactor",
        "advanced nuclear",
        "fusion energy",
        "fusion reactor",

        # Space
        "space exploration",
        "space technology",
        "space infrastructure",
        "space systems",
        "space transportation",
        "space launch",
        "launch services",
        "satellite internet",
        "satellite constellation",
        "spacecraft",
        "rocket",
        "rockets",

        # Emerging technology
        "quantum computing",
        "quantum technology",

        # Early-stage business
        "pre-revenue",
        "pre revenue",
        "development stage",
        "development-stage",
        "early stage",
        "early-stage",
    )

    speculative_keyword_hits = [
        keyword
        for keyword in speculative_keywords
        if keyword in metadata_text
    ]

    speculative_keyword_signal = bool(
        speculative_keyword_hits
    )

    # =========================================================
    # 10. Aerospace / Space 산업
    #
    # "aerospace & defense" 자체만으로 speculative 처리하지 않는다.
    #
    # 대신 미래사업 산업 + 낮은 수익성/높은 밸류에이션 등의
    # 위험 특성이 결합될 경우 speculative로 판단한다.
    # =========================================================

    aerospace_industry_signal = (
        "aerospace" in industry
        or "space" in industry
    )

    # =========================================================
    # 11. 미래사업 + 낮은 수익성
    #
    # SpaceX 같은 종목을 잡는 핵심 조건.
    #
    # 높은 매출 성장만으로 Established Growth로 보내지 않고,
    # 미래사업 산업에서 아직 수익성이 확보되지 않았다면
    # Speculative를 우선한다.
    # =========================================================

    emerging_unprofitable_signal = (
        (
            speculative_industry_signal
            or speculative_keyword_signal
            or aerospace_industry_signal
        )
        and negative_profitability
    )

    # =========================================================
    # 12. 미래사업 + 매우 높은 성장 + 낮은 수익성
    #
    # 성장률이 높더라도:
    #
    #   미래사업
    #   +
    #   높은 성장
    #   +
    #   낮은 수익성
    #
    # 은 Established Growth보다 Speculative에 가깝다.
    # =========================================================

    emerging_high_growth_low_quality_signal = (
        (
            speculative_industry_signal
            or speculative_keyword_signal
            or aerospace_industry_signal
        )
        and (
            very_strong_revenue_growth
            or strong_earnings_growth
        )
        and (
            negative_profitability
            or weak_profitability
        )
    )

    # =========================================================
    # 13. 미래사업 + 극단적인 밸류에이션
    #
    # P/S가 지나치게 높거나 P/B가 지나치게 높은 경우.
    #
    # 값이 없는 경우에는 판단하지 않는다.
    # =========================================================

    extreme_valuation_signal = (
        (
            price_to_sales is not None
            and price_to_sales >= 20
        )
        or (
            price_to_book is not None
            and price_to_book >= 15
        )
        or (
            forward_pe is not None
            and forward_pe >= 80
        )
    )

    emerging_extreme_valuation_signal = (
        (
            speculative_industry_signal
            or speculative_keyword_signal
            or aerospace_industry_signal
        )
        and extreme_valuation_signal
    )

    # =========================================================
    # 14. 소형/중형 + 실적 가시성 부족 + 높은 변동성
    # =========================================================

    small_cap_speculative_signal = (
        (
            small_cap
            or (
                mid_cap
                and high_volatility
            )
        )
        and (
            missing_growth_data
            or negative_profitability
        )
        and high_volatility
    )

    # =========================================================
    # 15. 최종 Speculative 판정
    # =========================================================
    speculative_signal = (
        emerging_unprofitable_signal
        or emerging_high_growth_low_quality_signal
        or emerging_extreme_valuation_signal
        or small_cap_speculative_signal
    )

    if speculative_signal:

        # -----------------------------------------------------
        # Confidence
        # -----------------------------------------------------
        confidence = 0.72

        evidence_count = 0

        if speculative_industry_signal:
            evidence_count += 1

        if speculative_keyword_signal:
            evidence_count += 1

        if aerospace_industry_signal:
            evidence_count += 1

        if negative_profitability:
            evidence_count += 1

        if extreme_valuation_signal:
            evidence_count += 1

        if high_volatility:
            evidence_count += 1

        if (
            very_strong_revenue_growth
            or strong_earnings_growth
        ):
            evidence_count += 1

        # 근거가 여러 개 겹칠수록 confidence 상승
        if evidence_count >= 4:
            confidence = 0.90
        elif evidence_count == 3:
            confidence = 0.86
        elif evidence_count == 2:
            confidence = 0.82
        elif evidence_count == 1:
            confidence = 0.78

        # -----------------------------------------------------
        # 이유 생성
        # -----------------------------------------------------
        reasons = []

        if speculative_industry_signal:
            reasons.append(
                "미래사업 관련 산업군"
            )

        if aerospace_industry_signal:
            reasons.append(
                "항공우주 산업"
            )

        if speculative_keyword_signal:
            reasons.append(
                "미래사업/신기술 사업 특성"
            )

        if negative_profitability:
            reasons.append(
                "수익성 또는 EPS가 음수"
            )

        elif weak_profitability:
            reasons.append(
                "수익성이 낮음"
            )

        if extreme_valuation_signal:
            reasons.append(
                "높은 밸류에이션"
            )

        if (
            very_strong_revenue_growth
            or strong_earnings_growth
        ):
            reasons.append(
                "높은 성장률"
            )

        if high_volatility:
            reasons.append(
                "높은 변동성"
            )

        reason = (
            " + ".join(reasons)
            if reasons
            else
            "미래 성장 기대 의존도가 높은 종목으로 판단됨"
        )

        return AssetClassification(
            ticker,
            "equity",
            "speculative",
            round(confidence, 2),
            "auto",
            reason,
        )

    # =========================================================
    # 16. Established Growth
    #
    # Speculative에서 탈락한 종목만 여기까지 내려온다.
    # =========================================================

    if established_growth_signal:

        confidence = 0.84

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

    # =========================================================
    # 17. Quality
    # =========================================================

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
        and (
            positive_eps
            or strong_profitability
            or return_on_equity is not None
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

    # =========================================================
    # 18. General Equity
    # =========================================================

    if quote_type in ("equity", "stock", ""):
        return AssetClassification(
            ticker,
            "equity",
            "general",
            0.55,
            "auto",
            "명확한 Quality/Growth/Speculative 분류 근거가 부족함",
        )

    # =========================================================
    # 19. Unsupported asset
    # =========================================================

    return AssetClassification(
        ticker,
        "other",
        "general",
        0.40,
        "auto",
        "지원되지 않는 자산 유형이므로 보수적으로 일반 전략 적용",
    )
