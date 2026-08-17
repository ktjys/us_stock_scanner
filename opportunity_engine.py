"""V8 기회 점수(Opportunity Score) 엔진.

전략(strategy_type)별 가중치로 투자 기회 점수(0~100)를 계산하고,
리스크를 독립 차원으로 평가하는 V8 핵심 엔진.

- opportunity_score(): 전략별 가중치로 기술/펀더멘털 컴포넌트를 합산한다.
  components dict에 존재하는 키만 순회하므로, 펀더멘털 키가 없으면
  기술 컴포넌트만으로 자동 재정규화된다 (설계 원칙 3.2-③).
- risk_score(): 실현변동성/beta/고점대비 눌림/거래량 이상/수익성·밸류에이션을
  가중합산해 리스크 점수와 등급(LOW~VERY_HIGH)을 반환한다.
- 기회 점수와 리스크는 독립 차원이다. 좋은 기회 ≠ 안전한 투자.

순환 임포트 방지: stock_scanner(추후 이 모듈을 모듈 레벨에서 임포트 예정)는
반드시 함수 내부에서만 임포트한다.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# 컴포넌트 정의
# ---------------------------------------------------------------------------

COMPONENT_MAXES = {
    "rsi_state": 20, "rsi_rebound": 15, "price_rebound": 15, "drawdown": 15,
    "ma20": 15, "trend": 5, "relative_strength": 10, "volume": 5,
    "momentum_20d": 10, "breakout": 10,
    "valuation": 10, "profitability": 10, "dividend": 10, "earnings": 10,
}

TECH_COMPONENTS = ["rsi_state", "rsi_rebound", "price_rebound", "drawdown", "ma20",
                   "trend", "relative_strength", "volume", "momentum_20d", "breakout"]

FUND_COMPONENTS = ["valuation", "profitability", "dividend", "earnings"]

# 전략별 컴포넌트 가중치 (0~3, 확정된 설계안).
# speculative는 52주 백테스트(2026-08) 후 momentum/breakout 추격 과대평가가
# 확인되어 momentum 3→2, breakout 3→2 하향, rsi_rebound 2→3, price_rebound 2→3 상향 조정.
# 'general'과 'other_etf'는 동일한 내용의 별도 dict (기술적으로 독립된 객체).
STRATEGY_WEIGHTS = {
    "general":            {"rsi_state": 1, "rsi_rebound": 1, "price_rebound": 1, "drawdown": 1, "ma20": 1, "trend": 1, "relative_strength": 1, "volume": 1, "momentum_20d": 0, "breakout": 0, "valuation": 0, "profitability": 0, "dividend": 0, "earnings": 0},
    "other_etf":          {"rsi_state": 1, "rsi_rebound": 1, "price_rebound": 1, "drawdown": 1, "ma20": 1, "trend": 1, "relative_strength": 1, "volume": 1, "momentum_20d": 0, "breakout": 0, "valuation": 0, "profitability": 0, "dividend": 0, "earnings": 0},
    "quality":            {"rsi_state": 2, "rsi_rebound": 2, "price_rebound": 1, "drawdown": 2, "ma20": 2, "trend": 3, "relative_strength": 1, "volume": 0, "momentum_20d": 2, "breakout": 0, "valuation": 3, "profitability": 3, "dividend": 1, "earnings": 2},
    "established_growth": {"rsi_state": 1, "rsi_rebound": 2, "price_rebound": 1, "drawdown": 1, "ma20": 2, "trend": 3, "relative_strength": 2, "volume": 1, "momentum_20d": 3, "breakout": 1, "valuation": 2, "profitability": 2, "dividend": 0, "earnings": 3},
    "speculative":        {"rsi_state": 1, "rsi_rebound": 3, "price_rebound": 3, "drawdown": 1, "ma20": 1, "trend": 2, "relative_strength": 3, "volume": 2, "momentum_20d": 2, "breakout": 2, "valuation": 0, "profitability": 0, "dividend": 0, "earnings": 1},
    "broad_market_etf":   {"rsi_state": 2, "rsi_rebound": 2, "price_rebound": 2, "drawdown": 2, "ma20": 3, "trend": 3, "relative_strength": 1, "volume": 1, "momentum_20d": 1, "breakout": 0, "valuation": 1, "profitability": 0, "dividend": 1, "earnings": 0},
    "growth_etf":         {"rsi_state": 1, "rsi_rebound": 2, "price_rebound": 1, "drawdown": 1, "ma20": 2, "trend": 3, "relative_strength": 3, "volume": 1, "momentum_20d": 3, "breakout": 2, "valuation": 0, "profitability": 0, "dividend": 0, "earnings": 0},
    "sector_etf":         {"rsi_state": 1, "rsi_rebound": 1, "price_rebound": 1, "drawdown": 1, "ma20": 2, "trend": 2, "relative_strength": 2, "volume": 1, "momentum_20d": 3, "breakout": 2, "valuation": 0, "profitability": 0, "dividend": 0, "earnings": 0},
    "dividend_etf":       {"rsi_state": 1, "rsi_rebound": 1, "price_rebound": 0, "drawdown": 2, "ma20": 2, "trend": 3, "relative_strength": 1, "volume": 0, "momentum_20d": 1, "breakout": 0, "valuation": 1, "profitability": 1, "dividend": 3, "earnings": 0},
    "income_etf":         {"rsi_state": 1, "rsi_rebound": 1, "price_rebound": 0, "drawdown": 2, "ma20": 2, "trend": 2, "relative_strength": 1, "volume": 0, "momentum_20d": 0, "breakout": 0, "valuation": 1, "profitability": 2, "dividend": 3, "earnings": 0},
}

_V7_COMPONENTS = ["rsi_state", "rsi_rebound", "price_rebound", "drawdown",
                  "ma20", "trend", "relative_strength", "volume"]


def _num(value: Any) -> float | None:
    """float 변환 헬퍼. 변환 실패/None이면 None (컴포넌트 점수 0 처리용)."""
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_technical_components(df: pd.DataFrame, i: int,
                                 market_df: pd.DataFrame | None = None,
                                 rs_series: pd.Series | None = None) -> dict[str, int] | None:
    """행 i의 V8 기술 컴포넌트 10개 점수를 계산한다.

    df는 지표 컬럼(rsi, ma20, ma50, high60, avgvol)이 이미 계산된 프레임이다
    (호출자가 보장). i는 정수 행 위치.

    - 8개 V7 컴포넌트는 stock_scanner.score_signal의 details를 그대로 사용한다.
    - momentum_20d/breakout은 20일·60일 고점 기준 신규 컴포넌트.
    - 필수 값이 하나라도 NaN이거나 i < 2이면 None (백테스트 루프가 행을 건너뜀).
    - rs_series가 제공되면 _relative_strength_series 재계산을 건너뛰어 O(n²)→O(n)으로 개선된다.
    """
    # 순환 임포트 방지: stock_scanner는 함수 내부에서만 임포트한다.
    from stock_scanner import _relative_strength_series, score_signal

    if i < 2 or i >= len(df):
        return None

    row = df.iloc[i]
    prev_row = df.iloc[i - 1]
    prev2_row = df.iloc[i - 2]

    price = float(row["Close"])
    rv = float(row["rsi"])
    prev = float(prev_row["rsi"])
    ma20 = float(row["ma20"])
    ma50 = float(row["ma50"])
    ma50_prev = float(prev_row["ma50"])
    ma20_prev = float(prev_row["ma20"])
    prev_price = float(prev_row["Close"])
    prev2_rsi = float(prev2_row["rsi"])

    high60 = float(row["high60"])
    dd = (price / high60 - 1) * 100 if high60 else float("nan")
    vr = float(row["Volume"]) / float(row["avgvol"]) if float(row["avgvol"]) else float("nan")

    # 필수 값 NaN 검사 (백테스트 루프가 이 행을 건너뛰도록 None 반환)
    required = (price, rv, prev, ma20, ma50, dd, vr,
                ma50_prev, prev_price, ma20_prev, prev2_rsi)
    if any(pd.isna(v) for v in required):
        return None

    # QQQ 대비 5일 상대강도
    # rs_series가 제공되면 재계산 생략 (O(n²) → O(n) 개선)
    # 그렇지 않으면 market_df가 있으면 내부에서 계산한다.
    relative_strength_5d: float | None = None
    if rs_series is not None:
        rs = rs_series.iloc[i]
        rs_float = float(rs) if not pd.isna(rs) else None
        relative_strength_5d = rs_float
    elif market_df is not None:
        rs = _relative_strength_series(df, market_df).iloc[i]
        rs_float = float(rs) if not pd.isna(rs) else None
        relative_strength_5d = rs_float

    score, cond, details = score_signal(
        price, rv, prev, ma20, ma50, dd, vr,
        ma50_prev=ma50_prev, prev_price=prev_price,
        ma20_prev=ma20_prev, relative_strength_5d=relative_strength_5d,
        prev2_rsi=prev2_rsi,
    )
    del score, cond  # details의 8개 V7 컴포넌트만 사용

    # 20일 모멘텀: Close[i]/Close[i-20] - 1 (기간 부족/NaN이면 0)
    momentum_20d = 0
    if i >= 20:
        ret20 = float(row["Close"]) / float(df.iloc[i - 20]["Close"]) - 1
        if pd.isna(ret20):
            momentum_20d = 0
        elif ret20 >= 0.08:
            momentum_20d = 10
        elif ret20 >= 0.05:
            momentum_20d = 7
        elif ret20 >= 0.03:
            momentum_20d = 4
        elif ret20 >= 0.01:
            momentum_20d = 2

    # 고점 대비 거리: Close[i]/high60[i] - 1 (신고가 근접 = 상방 돌파 시도)
    breakout = 0
    bdd = float(row["Close"]) / float(row["high60"]) - 1
    if not pd.isna(bdd):
        if bdd >= -0.02:
            breakout = 10
        elif bdd >= -0.05:
            breakout = 7
        elif bdd >= -0.10:
            breakout = 4

    return {
        "rsi_state": int(details["rsi_state"]),
        "rsi_rebound": int(details["rsi_rebound"]),
        "price_rebound": int(details["price_rebound"]),
        "drawdown": int(details["drawdown"]),
        "ma20": int(details["ma20"]),
        "trend": int(details["trend"]),
        "relative_strength": int(details["relative_strength"]),
        "volume": int(details["volume"]),
        "momentum_20d": momentum_20d,
        "breakout": breakout,
    }


def compute_fundamental_components(info: dict | None) -> dict[str, int]:
    """Yahoo Finance info dict에서 V8 펀더멘털 컴포넌트 4개 점수를 계산한다.

    필드가 없거나 float 변환에 실패하면 해당 컴포넌트는 0점이다.
    """
    out = {"valuation": 0, "profitability": 0, "dividend": 0, "earnings": 0}
    if not info:
        return out

    # valuation: 적정 PER 선호, P/S >= 20은 과대평가로 0점.
    # 둘 다 있으면 더 낮은 점수를 취한다.
    pe_score: int | None = None
    pe = _num(info.get("trailingPE"))
    if pe is not None:
        if 10 <= pe < 25:
            pe_score = 10
        elif 25 <= pe < 40:
            pe_score = 7
        elif 40 <= pe <= 80:
            pe_score = 4
        elif pe > 80:
            pe_score = 2
        elif pe <= 0:
            pe_score = 1

    ps_score: int | None = None
    ps = _num(info.get("priceToSalesTrailing12Months"))
    if ps is not None and ps >= 20:
        ps_score = 0

    if pe_score is not None and ps_score is not None:
        out["valuation"] = min(pe_score, ps_score)
    elif pe_score is not None:
        out["valuation"] = pe_score
    elif ps_score is not None:
        out["valuation"] = ps_score

    # profitability: profitMargins (비율, 예: 15% = 0.15)
    pm = _num(info.get("profitMargins"))
    if pm is not None:
        if pm >= 0.15:
            out["profitability"] = 10
        elif pm >= 0.05:
            out["profitability"] = 7
        elif pm > 0:
            out["profitability"] = 4
        else:
            out["profitability"] = 0

    # dividend: Yahoo dividendYield는 비율 값 (예: 2% = 0.02)
    dy = _num(info.get("dividendYield"))
    if dy is not None:
        if dy >= 0.03:
            out["dividend"] = 10
        elif dy >= 0.02:
            out["dividend"] = 7
        elif dy >= 0.01:
            out["dividend"] = 4
        else:
            out["dividend"] = 0

    # earnings: earningsGrowth (비율 값)
    eg = _num(info.get("earningsGrowth"))
    if eg is not None:
        if eg >= 0.20:
            out["earnings"] = 10
        elif eg >= 0.10:
            out["earnings"] = 7
        elif eg >= 0:
            out["earnings"] = 4
        else:
            out["earnings"] = 0

    return out


def opportunity_score(components: dict[str, int], strategy: str) -> int:
    """전략별 가중치로 컴포넌트 점수를 0~100 기회 점수로 정규화한다.

    components dict에 존재하는 키만 순회하므로, 펀더멘털 키가 없는
    백테스트 호출에서는 기술 컴포넌트만으로 자동 재정규화된다.
    """
    weights = STRATEGY_WEIGHTS.get(strategy, STRATEGY_WEIGHTS["general"])
    num = 0
    den = 0
    for key, value in components.items():
        w = weights.get(key, 0)
        if w <= 0:
            continue
        num += w * value
        den += w * COMPONENT_MAXES[key]
    if den == 0:
        return 0
    return round(100 * num / den)


# 대시보드 설명용 4축 집계 (전략 가중치와 무관한 동일 가중치 재정규화).
# technical: 기술 상태, momentum: 추진력, fundamental: 펀더멘털, valuation: 밸류에이션.
SUB_SCORE_GROUPS = {
    "technical_score": ["rsi_state", "rsi_rebound", "price_rebound", "drawdown",
                        "ma20", "trend", "volume"],
    "momentum_score": ["relative_strength", "momentum_20d", "breakout"],
    "fundamental_score": ["profitability", "earnings", "dividend"],
    "valuation_score": ["valuation"],
}


def component_sub_scores(components: dict[str, int]) -> dict[str, int | None]:
    """14개 컴포넌트를 4개 설명 축 점수(0~100)로 집계한다.

    각 축은 존재하는 컴포넌트만 동일 가중치로 재정규화한다 (전략과 무관한
    설명용 수치). 축에 컴포넌트가 하나도 없으면 None을 반환해 '데이터 없음'과
    0점(나쁨)을 구분한다. 백테스트(펀더멘털 없음)에서는 fundamental/valuation이
    None이 된다.
    """
    out: dict[str, int | None] = {}
    for name, keys in SUB_SCORE_GROUPS.items():
        present = [k for k in keys if k in components]
        if not present:
            out[name] = None
            continue
        num = sum(components[k] for k in present)
        den = sum(COMPONENT_MAXES[k] for k in present)
        out[name] = round(100 * num / den)
    return out


def risk_score(df: pd.DataFrame, i: int, info: dict | None = None) -> tuple[int, str]:
    """리스크를 독립 차원으로 평가해 (점수, 등급)을 반환한다.

    가중치 합 100: 실현변동성 30, beta 20, 고점대비 눌림 20,
    거래량 이상 15, 수익성/밸류에이션 15.

    등급: 0~34 LOW, 35~54 MEDIUM, 55~74 HIGH, 75~100 VERY_HIGH
    """
    if i < 0 or i >= len(df):
        return 0, "LOW"

    row = df.iloc[i]

    # 1) 실현변동성: 20일 일별 수익률의 연율화 표준편차 (%) [×0.30]
    window = df["Close"].iloc[max(0, i - 19): i + 1].astype(float)
    ann_vol = window.pct_change().std() * math.sqrt(252) * 100
    if pd.isna(ann_vol):
        vol_points = 0
    elif ann_vol < 20:
        vol_points = 0
    elif ann_vol < 30:
        vol_points = 20
    elif ann_vol < 40:
        vol_points = 40
    elif ann_vol < 60:
        vol_points = 60
    else:
        vol_points = 80

    # 2) beta: 커버리지 없으면 중간값 30점 [×0.20]
    beta = _num(info.get("beta")) if info else None
    if beta is None:
        beta_points = 30
    elif beta < 0.8:
        beta_points = 0
    elif beta < 1.2:
        beta_points = 20
    elif beta < 1.8:
        beta_points = 50
    elif beta < 2.5:
        beta_points = 75
    else:
        beta_points = 100

    # 3) 고점대비 눌림: dd = (Close/high60 - 1) * 100 (%) [×0.20]
    high60 = float(row["high60"]) if not pd.isna(row["high60"]) else float("nan")
    dd = (float(row["Close"]) / high60 - 1) * 100 if high60 else float("nan")
    if pd.isna(dd):
        dd_points = 0
    elif dd >= -5:
        dd_points = 10
    elif dd >= -15:
        dd_points = 30
    elif dd >= -25:
        dd_points = 60
    elif dd >= -35:
        dd_points = 80
    else:
        dd_points = 100

    # 4) 거래량 이상: vr = Volume/avgvol [×0.15]
    avgvol = float(row["avgvol"]) if not pd.isna(row["avgvol"]) else float("nan")
    vr = float(row["Volume"]) / avgvol if avgvol else float("nan")
    if pd.isna(vr):
        vr_points = 0
    elif 0.5 <= vr <= 2:
        vr_points = 0
    elif 2 < vr <= 3:
        vr_points = 40
    elif vr > 3:
        vr_points = 70
    elif vr < 0.3:
        vr_points = 80
    else:
        vr_points = 0  # 0.3 <= vr < 0.5: 이상치 아님

    # 5) 수익성/밸류에이션 [×0.15]
    if info:
        pm = _num(info.get("profitMargins"))
        pe = _num(info.get("trailingPE"))
        ps = _num(info.get("priceToSalesTrailing12Months"))
        pm_negative = pm is not None and pm < 0
        val_extreme = (pe is not None and pe > 80) or (ps is not None and ps >= 20)
        if pm_negative and val_extreme:
            val_points = 100
        elif pm_negative:
            val_points = 60
        elif val_extreme:
            val_points = 40
        else:
            val_points = 0
    else:
        val_points = 0

    total = round(
        vol_points * 0.30
        + beta_points * 0.20
        + dd_points * 0.20
        + vr_points * 0.15
        + val_points * 0.15
    )
    if total <= 34:
        level = "LOW"
    elif total <= 54:
        level = "MEDIUM"
    elif total <= 74:
        level = "HIGH"
    else:
        level = "VERY_HIGH"
    return total, level


def signal_confidence(score: int) -> float:
    """기회 점수(0~100)를 신호 신뢰도(0.4~1.0)로 변환한다."""
    return round(0.40 + 0.60 * (score / 100), 3)
