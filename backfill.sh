#!/usr/bin/env bash
# daily_data/신호 백필 편의 스크립트 (env 체크 + .env 자동 로드)
#
# 사용법:
#   ./backfill.sh                              # 기본 52주 백필 + 신호 승격 (55점 이상)
#   ./backfill.sh --weeks 52                   # 기간 지정 (기본 52주)
#   ./backfill.sh --weeks 104                  # 2년치 백필
#   ./backfill.sh --threshold 60               # 신호 임계값 조정 (기본 55)
#   ./backfill.sh --no-signals                 # 신호 승격 끄기 (daily_data만)
#   ./backfill.sh --tickers AAPL,MSFT          # 특정 종목만
set -euo pipefail
cd "$(dirname "$0")"

# .env 파일이 있으면 로드
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

: "${SUPABASE_URL:?❌ SUPABASE_URL 미설정. .env 파일을 만들거나 export 하세요}"
: "${SUPABASE_KEY:?❌ SUPABASE_KEY 미설정}"

exec python3 backfill_daily.py "$@"
