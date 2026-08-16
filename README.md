# 미국주식 매수신호 스캐너 V4

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

## 4. 스코어링 (최대 90점)
| 조건 | 점수 |
|---|---|
| RSI < 35 (과매도) | +20 |
| 35 ≤ RSI < 40 (과매도) | +10 |
| RSI 반등 (전일 대비 상승) | +15 |
| 60일 고점 대비 -10% 이상 | +20 |
| 20일선 ±3% 근접 | +15 |
| 50일선 위 | +10 |
| 거래량 20일 평균 1.2배 이상 | +10 |

RSI 구간은 상호배타적이라 중복 가산되지 않습니다. RSI<40과 RSI<35가 동시에
가산되던 기존 방식과 달리, RSI 하나로 점수가 과도하게 치우치지 않습니다.

## 5. watchlist 관리
- 기본값: `watchlist.csv` (저장소에 커밋된 파일) - Supabase 테이블이 비어 있을 때 시드되는 "시드 전용" 파일
- 실제 스캔 대상은 Supabase `watchlist` 테이블(ticker, name, active)이 기준이며,
  `active = false`로 두면 스캔에서 제외됩니다.
- 테이블이 없거나 비어 있으면 `watchlist.csv` 내용으로 자동 시드 후 사용합니다.
- CLI로 종목을 추가/제거한 뒤 CSV를 최신 상태로 유지하려면 `./watchlist.sh sync-csv` 실행
- backtest.py도 또한 Supabase 테이블을 우선으로 함

### CLI로 관리 (권장 - SQL 불필요)
```bash
./watchlist.sh list                             # 목록 조회
./watchlist.sh add TSLA AMD Tesla               # 종목 추가 (코드 또는 영문 이름)
./watchlist.sh add BRK.B Berkshire              # 코드/이름 혼용 가능 (여러 개)
./watchlist.sh remove VOO QQQ                   # 스캔 제외 (비활성화)
./watchlist.sh activate TSLA                    # 다시 활성화
```
- `./watchlist.sh`는 `python manage_watchlist.py`를 감싸는 편의 래퍼로,
  `.env`를 자동 로드하고 자격증명이 없으면 바로 알려줍니다
- `add`는 Yahoo Finance로 종목을 **검증한 뒤에만** 저장합니다. 잘못된 코드는
  "추가 안 됨"으로 거부됩니다.
- 종목코드(`TSLA`) 또는 영문 회사명(`Tesla`)을 입력하면 회사명/코드를 자동으로
  조회합니다. 회사명 검색은 영문만 지원합니다 (한글 미지원).
- 이미 있는 종목을 add하면 중복 없이 `active = true`로 재활성화됩니다.
- `add` 시 테이블이 비어 있으면 `watchlist.csv`의 종목을 먼저 자동 시드합니다
- `remove`는 행 삭제가 아닌 `active = false` 비활성화입니다 (행 삭제 시
  테이블이 비워져 CSV가 다시 시드되는 문제 방지)

## 6. 로컬 실행

```bash
pip install -r requirements.txt

# 편의 스크립트 (env 체크 + .env 자동 로드)
cp .env.example .env        # SUPABASE_URL/KEY, 텔레그램 토큰 입력
./run_local.sh                              # 전체 실행 (DB + 텔레그램)
./run_local.sh --no-db                      # DB 저장 스킵 (Supabase 불필요)
./run_local.sh --no-telegram                # 텔레그램 대신 콘솔 출력
./run_local.sh --no-db --no-telegram        # 순수 분석 (env 변수 불필요)
./run_local.sh --date 2026-08-13            # 기준 날짜 지정
./run_local.sh --threshold 60               # 신호 임계값 조정 (기본 55)

# 직접 실행도 가능
python run_local.py --no-db --no-telegram
python run_local.py --date 2026-08-13        # 잘못된 날짜 형식은 실행 전 거부
python run_local.py --threshold 60           # 60점 이상을 신호로 저장/발송
python report.py             # 주간 리포트를 콘솔로 확인
python report.py --weeks 4   # 최근 4주만 집계 (기본: 전체)
python -m pytest tests/     # 테스트 실행 (pip install -r requirements-dev.txt)

# 과거 daily_data/신호 백필 (env 체크 + .env 자동 로드)
./backfill.sh                              # 기본 52주 백필 + 신호 승격 (55점 이상)
./backfill.sh --weeks 104                  # 2년치 백필
./backfill.sh --threshold 60               # 신호 임계값 조정 (기본 55)
./backfill.sh --no-signals                 # 신호 승격 끄기 (daily_data만)
./backfill.sh --tickers AAPL,MSFT          # 특정 종목만
# 주의: backfill_daily.py를 직접 실행하면 .env가 로드되지 않아 DB 저장이 생략되므로
# 반드시 ./backfill.sh 로 실행할 것 (watchlist.sh와 동일한 .env 자동 로드 래퍼)

# 임계값별 백테스트 (fetch 1회 + 지표 선계산, lookahead bias 없음)
# 종목은 스캐너와 동일하게 Supabase watchlist 테이블 우선(env 미설정 시 watchlist.csv 폴백)
python backtest.py                              # 전체 watchlist, 기본 6개월, 55/60/65점 비교
python backtest.py --thresholds 50,55,60,65     # 임계값 조합 지정
python backtest.py --weeks 12                   # 기간 지정 (기본 26주)
python backtest.py --tickers AAPL,MSFT          # 특정 종목만
```

참고: `--no-db` 모드는 Supabase를 아예 사용하지 않아 자격증명이 필요 없습니다.
`--no-db` 없이 실행하면 로컬에서도 실제 DB에 데이터가 쌓이므로, 프로덕션
데이터를 보호하려면 테스트 전용 Supabase 프로젝트를 사용하세요.
처리 실패 종목이 있으면 exit code 1로 종료됩니다 (CI에서 실패로 감지 가능).

## 7. 대시보드
`dashboard/` 폴더의 정적 페이지를 GitHub Pages로 배포해 Supabase 데이터를 웹에서 확인합니다.
읽기 전용이며, 종목 관리(watchlist)는 섹션 5의 CLI를 사용합니다.

### 설정 (최초 1회)
1. Supabase SQL Editor에서 `supabase_dashboard_rls.sql` 실행 — anon 키를 읽기 전용으로 제한
   (스캐너가 쓰는 `SUPABASE_KEY`는 **service_role 키**여야 합니다. anon 키라면
   RLS 활성화로 쓰기가 거부됩니다)
2. `dashboard/config.example.js`를 `dashboard/config.js`로 복사 후 키 입력:
   Supabase 콘솔 → Settings → API → Project URL / anon public key
   (`config.js`는 .gitignore로 커밋에서 제외됩니다)
3. GitHub → Settings → Pages → Source를 **GitHub Actions**로 변경 (최초 1회)
4. 이후 `dashboard/` 파일이 커밋되면 워크플로우가 자동 배포됩니다
   (URL: `https://<사용자명>.github.io/us_stock_scanner/`)

### 화면 (6개 탭, V8 기준)

대시보드는 V8 Opportunity Score(score_version=8) 데이터를 표시합니다.
V8 판단 규칙: 기회점수 55점 이상이고 리스크가 VERY_HIGH가 아니면 **BUY**, 그 외 **WATCH**.

- **현황**: 최신 스캔 날짜, 당일 후보(기회점수 ≥55) 수, 활성 watchlist 수 + 후보 상위 3종목 카드
  (전략/리스크/판단 배지, 점수·RSI·고점대비) + 근접 후보(50~54점) 상위 3종목.
  마지막 스캔이 2~3일 이상 지났으면 "스캔 중단" 경고 배너 표시.
- **점수판**: 날짜(최근 10영업일)별 종목 테이블 — 종목/전략/리스크/점수/RSI/전일RSI/고점대비/MA20/MA50/거래량비,
  RSI·50일선·거래량비 필터, 컬럼 클릭 정렬. 55점 이상 행은 강조 표시.
- **상세**: 종목별 동기화 멀티패널 차트 (1/3/6개월). 패널 조합: 기본(가격/MA + 거래량 + RSI + 점수),
  가격+점수 / 가격+RSI / 가격+거래량 / 전체 / 사용자 정의(체크박스). 모든 패널은 날짜 선택선을 공유하며,
  선택 시 가격·MA·점수·RSI·거래량비·상대강도·전략·리스크·4축 점수(기술/모멘텀/펀더멘털/밸류) 요약 표시.
- **신호·성과**: 신호 히스토리(신호일/종목/전략/리스크/기회점수/판단/신호가/점수/신뢰도 + 5·10·20일 수익률) +
  기간별(전체/4주/12주) 5·10·20일 평균수익률·승률(양수 비율) 카드. 주간 리포트와 동일 계산 로직.
- **히트맵**: 행=종목, 열=날짜(최근 10거래일) 점수 그리드. 색상: 회색(≤0) · 짙은 파랑(<35) · 파랑(<60) ·
  주황(<65, 근접) · 빨강(≥65, 신호). 셀 클릭 시 해당 종목 상세 탭으로 이동.
- **백테스트**: 점수구간(40-44~80+)별 신호수·5일 승률·20일 평균수익률 차트 + 구간별 요약 표
  (신호수/승률/평균 5·10·20일/MAE/MFE/표본수) + 최근 신호 목록.
  backtest.json이 `modes.v7/v8` both 형식이면 **V8/V7 버전 토글**이 표시됩니다 (기본 V8).
  (`.github/workflows/backtest.yml`이 매주 월요일 23:00 UTC에 `backtest.py --mode both --json`으로
  `dashboard/data/backtest.json`을 갱신·커밋 → 자동 재배포. GitHub Actions 탭에서 수동 실행도 가능)

## 8. 주의
기술적 신호는 매수 추천이나 수익을 보장하지 않습니다.


## V7 점수식 변경

V7는 "많이 떨어진 종목"보다 **눌림 후 실제 반등이 시작된 종목**을 찾는 방향으로 변경했습니다.

- RSI 상태: 20점
- RSI 반등: 15점
- 가격 반등: 15점
- 적정 눌림폭: 15점
- MA20 회복/접근: 15점
- 중기 추세: 5점
- QQQ 대비 5일 상대강도: 10점
- 반등+거래량 확인: 5점
- 총 100점

백테스트는 누적 임계값(≥65 등)만 보지 않고 40~44, 45~49 ... 80+ 점수구간으로 성과를 확인하며, 동일 종목의 5일 이내 반복 신호는 최고점 1건으로 줄입니다.

### V7 상세 차트

상세 화면은 동기화된 멀티패널 구조입니다.

- 기본: 가격/MA + 거래량 + RSI + V7 점수
- 가격+점수
- 가격+RSI
- 가격+거래량
- 전체
- 사용자 정의

모든 패널은 동일 날짜의 선택선과 선택값을 공유합니다.

### V7 Supabase

기존 DB에 `relative_strength_5d` 컬럼이 필요하므로 `supabase_v6_migration.sql`을 SQL Editor에서 한 번 실행하세요. 기존 데이터의 `score_version`은 유지하고 새 스캔/백필은 `score_version=7`으로 저장합니다.

백필 예:
`python backfill_daily.py --weeks 52 --threshold 40`

백테스트 예:
`python backtest.py --weeks 52 --thresholds 80,75,70,65,60,55,50,45,40 --json dashboard/data/backtest.json`

## V8 Phase 1 — 종목 자동 분류 (기존 Scanner 유지)

V8의 첫 단계는 기존 V7 Scanner를 변경하지 않고 `asset_classification.py`를 추가하는 것입니다.
Yahoo Finance 메타데이터를 표준화한 뒤 ETF/우량주/성장주/고변동 성장주 등의 장기 투자 전략군을
자동 분류합니다. Supabase에는 `supabase_v8_phase1_migration.sql`로 분류 결과를 별도 저장합니다.

- 기존 `daily_data`, `signals`, V7 scoring은 그대로 유지
- 자동 분류 결과에는 confidence와 reason을 함께 저장
- 이후 Phase 2에서 전략군별 Opportunity Score를 연결할 예정

## V8 Phase 2 — Opportunity Score (전략 인식 스코어링)

V8 Phase 2는 V7의 단일 공통 점수식(`score_signal`)을 전략(strategy_type)별 가중치 스코어링으로
대체합니다. 핵심 엔진은 `opportunity_engine.py`이며, V8 Scanner(`compute_signal_v8`)가
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

- `general`(= `other_etf`)은 V7과 **수학적으로 동일한 점수**를 냅니다 (V7 8개 컴포넌트 가중치 1,
  신규 6개 가중치 0). V7 대비 비교의 대조군입니다.
- 예: speculative는 상대강도/되돌림 반등에, quality는 밸류/수익성/중기추세에 높은 가중치.
  (speculative의 모멘텀/돌파 가중치는 52주 백테스트에서 고점 추격 과대평가가 확인되어 3→2로 하향)

### Risk / Confidence (독립 차원)

- 기회 점수와 리스크는 **독립 차원**입니다. 좋은 기회 ≠ 안전한 투자.
- `risk_score` (가중치 합 100): 실현변동성 30 + beta 20 + 고점대비 눌림 20 +
  거래량 이상 15 + 수익성/밸류에이션 15 → 0~100점
- `risk_level`: 0~34 LOW, 35~54 MEDIUM, 55~74 HIGH, 75~100 VERY_HIGH
- `signal_confidence = 0.40 + 0.60 × (score/100)` — 점수 기반 신뢰도 (0.4~1.0)
- `classification_confidence`: asset_classification 분류 신뢰도 (별도 저장)

### 실행

```bash
# 스캔 (V8 엔진 자동 사용 — 신호는 score_version=8로 저장)
./run_local.sh --no-db --no-telegram

# 백테스트 (V7 vs V8 비교)
python backtest.py --mode v7 --weeks 26 --json /tmp/bt_v7.json
python backtest.py --mode v8 --weeks 26 --json /tmp/bt_v8.json

# V7+V8 동시 실행 (JSON에 modes.v7/v8 서브리포트 포함 — 대시보드 비교용)
python backtest.py --mode both --weeks 52 --json dashboard/data/backtest.json
```

### V8 Supabase

기존 DB에 신규 컬럼이 필요하므로 `supabase_v8_phase2_migration.sql`을 SQL Editor에서 한 번
실행하세요. signals에 `strategy_type`, `opportunity_score`, `risk_level`, `risk_score`,
`signal_confidence`, `classification_confidence`와 4개 설명 축 점수(`technical_score`,
`momentum_score`, `fundamental_score`, `valuation_score`), 컴포넌트 상세(`components` jsonb),
daily_data에 `strategy_type`, `opportunity_score`, `risk_level`과 동일한 4개 축 점수 +
`components` jsonb 컬럼을 추가합니다 (idempotent, 재실행 가능).
새 스캔/백필은 `score_version=8`로 저장되며, 기존 V7 데이터(`score_version=7`)는 그대로 유지됩니다.

대시보드에서 종목 분류(전략군)도 읽으므로 `supabase_dashboard_rls.sql`을 재실행해
`asset_classification` 읽기 정책을 추가하세요 (이미 실행한 경우에도 idempotent라 재실행 가능).

### V8 대시보드 개편

대시보드는 V8 기준으로 개편되어 다음이 반영됩니다.

- 신호·성과 탭: 전략/리스크/기회점수/판단(BUY·WATCH 배지)/신뢰도 컬럼 + 빈 상태 시 안내(히트맵·현황 이동)
- 점수판/현황: 전략·리스크 배지, 후보 카드에 판단 표시, 근접 후보 50~54점 기준
- 상세 탭: 툴팁에 전략/리스크 + 4축 점수(기술/모멘텀/펀더멘털/밸류) 표시
- 백테스트 탭: `--mode both` 생성 JSON 기준 **V8/V7 버전 토글** (기본 V8, 단일 형식이면 자동 폴백)
- 주간 리포트(`report.py`/`send_weekly_report.py`): `score_version=8` 신호만 집계 + V8 백테스트 요약

대시보드/워크플로우는 `score_version=8` 데이터만 조회하므로, 백필 후에는
`backtest.py --mode both --json dashboard/data/backtest.json`으로 backtest.json을
다시 생성해야 V8 토글이 동작합니다 (워크플로우가 매주 자동 갱신).
