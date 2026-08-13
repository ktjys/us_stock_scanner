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
- 65점 이상 signals 저장
- 미완료 신호(return_20d 미갱신)의 5/10/20일 수익률 업데이트
- 매수 후보 Telegram 발송 (같은 종목은 5일 이내 재알림 없음)
- yfinance 조회는 3회 재시도(지수 백오프), 7일 이상 지난 데이터(거래정지 등)는 스킵
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
- 기본값: `watchlist.csv` (저장소에 커밋된 파일)
- Supabase `watchlist` 테이블(ticker, name, active)에 종목이 있으면 그 테이블을
  우선 사용합니다. `active = false`로 두면 스캔에서 제외됩니다.
- 테이블이 없거나 비어 있으면 `watchlist.csv` 내용으로 자동 시드 후 사용합니다.

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

# 직접 실행도 가능
python run_local.py --no-db --no-telegram
python run_local.py --date 2026-08-13        # 잘못된 날짜 형식은 실행 전 거부
python report.py             # 주간 리포트를 콘솔로 확인
python report.py --weeks 4   # 최근 4주만 집계 (기본: 전체)
python -m pytest tests/     # 테스트 실행 (pip install -r requirements-dev.txt)
```

참고: `--no-db` 모드는 Supabase를 아예 사용하지 않아 자격증명이 필요 없습니다.
`--no-db` 없이 실행하면 로컬에서도 실제 DB에 데이터가 쌓이므로, 프로덕션
데이터를 보호하려면 테스트 전용 Supabase 프로젝트를 사용하세요.
처리 실패 종목이 있으면 exit code 1로 종료됩니다 (CI에서 실패로 감지 가능).

## 7. 주의
기술적 신호는 매수 추천이나 수익을 보장하지 않습니다.
