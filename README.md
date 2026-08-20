# 미국주식 매수신호 스캐너 V8

## 구조
GitHub Actions → Yahoo Finance → Supabase PostgreSQL → Telegram

## 1. Supabase
Supabase 프로젝트를 만든 후 SQL Editor에서 `supabase_schema.sql`을 실행합니다.
(`watchlist` 테이블이 포함되어 있으며, 스캐너는 이 테이블을 우선 사용합니다.)

## 2. GitHub Secrets
Repository → Settings → Secrets and variables → Actions에 등록:
- SUPABASE_URL
- SUPABASE_KEY
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID

## 3. 실행
평일 매일 스캔 (22:30 UTC, 20분 타임아웃):
- daily_data 저장
- 55점 이상 signals 저장
- 미완료 신호(return_20d 미갱신)의 5/10/20일 수익률 업데이트
- 매수 후보 Telegram 발송 (같은 종목은 5일 이내 재알림 없음)
- yfinance 조회는 3회 재시도(지수 백오프), 7일 이상 지난 데이터(거래정지 등)는 스킵
- 미국 시장 휴일(굿 프라이데이 포함 NYSE 휴장일)에는 스캔을 건너뜀
- 스캔 실패 시 exit code 1 → 워크플로우 실패 + 실패 원인 Telegram 발송

매주 월요일:
- 최근 12주 누적 평균수익률/승률 Telegram 발송 (`--weeks`로 조정 가능)
- 365일 초과 daily_data 자동 정리 (보존 정책)

## 4. V8 Opportunity Score (전략 인식 스코어링)

핵심 엔진은 `opportunity_engine.py`이며, V8 Scanner(`compute_signal_v8`)가
`analyze()` → `scan()` 파이프라인에서 이를 사용합니다.

### 점수의 의미 통일 (0~100)

- `opportunity_score = round(100 * Σ(w·c) / Σ(w·max))`
- 전략별 가중치 `w`가 각 컴포넌트의 중요도를 결정하고, **컴포넌트가 없으면 자동 재정규화**됩니다.
  백테스트처럼 펀더멘털 정보가 없으면 기술 컴포넌트만으로 점수가 다시 정규화됩니다.
- 컴포넌트 14종: rsi_state(20) rsi_rebound(15) price_rebound(15) drawdown(15) ma20(15)
  trend(5) relative_strength(10) volume(5) momentum_20d(10) breakout(10)
  valuation(10) profitability(10) dividend(10) earnings(10)
  (괄호는 컴포넌트 만점; 마지막 4개는 Yahoo Finance info 기반)

### 전략군별 가중치 (핵심 설계)

| 전략 | rsi_state | rsi_rebound | price_rebound | drawdown | ma20 | trend | rel_str | volume | mom_20d | breakout | valuation | profit | dividend | earnings |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| general / other_etf | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| quality | 2 | 2 | 1 | 2 | 2 | 3 | 1 | 0 | 2 | 0 | 3 | 3 | 1 | 2 |
| established_growth | 1 | 2 | 1 | 1 | 2 | 3 | 2 | 1 | 3 | 1 | 2 | 2 | 0 | 3 |
| speculative | 1 | 3 | 3 | 1 | 1 | 2 | 3 | 2 | 2 | 2 | 0 | 0 | 0 | 1 |
| broad_market_etf | 2 | 2 | 2 | 2 | 3 | 3 | 1 | 1 | 1 | 0 | 1 | 0 | 1 | 0 |
| growth_etf | 1 | 2 | 1 | 1 | 2 | 3 | 3 | 1 | 3 | 2 | 0 | 0 | 0 | 0 |
| sector_etf | 1 | 1 | 1 | 1 | 2 | 2 | 2 | 1 | 3 | 2 | 0 | 0 | 0 | 0 |
| dividend_etf | 1 | 1 | 0 | 2 | 2 | 3 | 1 | 0 | 1 | 0 | 1 | 1 | 3 | 0 |
| income_etf | 1 | 1 | 0 | 2 | 2 | 2 | 1 | 0 | 0 | 0 | 1 | 2 | 3 | 0 |

- `general`(= `other_etf`)은 신규 6개 컴포넌트 가중치를 0으로 두고, 기존 8개 컴포넌트만으로 점수를 냅니다.
- 예: speculative는 상대강도/되돌림 반등에, quality는 밸류/수익성/중기추세에 높은 가중치.

### Risk / Confidence (독립 차원)

- 기회 점수와 리스크는 **독립 차원**입니다. 좋은 기회 ≠ 안전한 투자.
- `risk_score` (가중치 합 100): 실현변동성 30 + beta 20 + 고점대비 눌림 20 +
  거래량 이상 15 + 수익성/밸류에이션 15 → 0~100점
- `risk_level`: 0~34 LOW, 35~54 MEDIUM, 55~74 HIGH, 75~100 VERY_HIGH
- `signal_confidence = 0.40 + 0.60 × (score/100)` — 점수 기반 신뢰도 (0.4~1.0)
- `classification_confidence`: asset_classification 분류 신뢰도 (별도 저장)

### 실행

```bash
# 스캔 (V8 엔진 자동 사용 — 신호는 score_version=8으로 저장)
./run_local.sh --no-db --no-telegram

# 백테스트 (V8)
python backtest.py --mode v8 --weeks 26 --json /tmp/bt_v8.json

# 백테스트 JSON 생성 (대시보드용)
python backtest.py --mode v8 --weeks 52 --json dashboard/data/backtest.json
```

### V8 Supabase

기존 DB에 신규 컬럼이 필요하므로 `supabase_v8_phase2_migration.sql`을 SQL Editor에서 한 번
실행하세요. signals에 `strategy_type`, `opportunity_score`, `risk_level`, `risk_score`,
`signal_confidence`, `classification_confidence`와 4개 설명 축 점수(`technical_score`,
`momentum_score`, `fundamental_score`, `valuation_score`), 컴포넌트 상세(`components` jsonb),
daily_data에 `strategy_type`, `opportunity_score`, `risk_level`과 동일한 4개 축 점수 +
`components` jsonb 컬럼을 추가합니다 (idempotent, 재실행 가능).
새 스캔/백필은 `score_version=8`으로 저장됩니다.

대시보드에서 종목 분류(전략군)도 읽으므로 `supabase_dashboard_rls.sql`을 재실행해
`asset_classification` 읽기 정책을 추가하세요 (이미 실행한 경우에도 idempotent라 재실행 가능).

### V8 대시보드 개편

대시보드는 V8 기준으로 개편되어 다음이 반영됩니다.

- 신호·성과 탭: 전략/리스크/기회점수/판단(BUY·WATCH 배지)/신뢰도 컬럼 + 빈 상태 시 안내(히트맵·현황 이동)
- 점수판/현황: 전략·리스크 배지, 후보 카드에 판단 표시, 근접 후보 50~54점 기준
- 상세 탭: 툴팁에 전략/리스크 + 4축 점수(기술/모멘텀/펀더멘털/밸류) 표시
- 백테스트 탭: `--mode v8` 생성 JSON 기준 V8 백테스트 결과 표시
- 주간 리포트(`report.py`/`send_weekly_report.py`): `score_version=8` 신호만 집계 + V8 백테스트 요약

대시보드/워크플로우는 `score_version=8` 데이터만 조회하므로, 백필 후에는
`backtest.py --mode v8 --json dashboard/data/backtest.json`으로 backtest.json을
다시 생성해야 V8 백테스트가 반영됩니다 (워크플로우가 매주 자동 갱신).

## 8. 주의
기술적 신호는 매수 추천이나 수익을 보장하지 않습니다.

---

## Future Roadmap

- **V8 Phase 3**: 실시간 데이터 파이프라인 최적화 및 지연 시간 감소
- **Supabase RLS**: 정책 고도화 및 성능 튜닝
- **대시보드 확장**: 추가 필터링 및 시각화 옵션 도입
- **전략 가중치 자동 최적화**: 백테스트 기반 가중치 재조정 시스템