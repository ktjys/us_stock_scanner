#!/usr/bin/env bash
# 로컬 스캐너 실행 편의 스크립트 (env 체크 + .env 자동 로드)
#
# 사용법:
#   ./run_local.sh                          # 전체 실행 (DB + 텔레그램)
#   ./run_local.sh --no-db                  # DB 저장 스킵 (Supabase 불필요)
#   ./run_local.sh --no-telegram            # 콘솔 출력 모드
#   ./run_local.sh --no-db --no-telegram    # 순수 분석 (env 변수 불필요)
#   ./run_local.sh --date 2026-08-13        # 기준 날짜 지정
set -euo pipefail
cd "$(dirname "$0")"

# .env 파일이 있으면 로드
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# DB 저장 모드면 Supabase 자격증명 필수
if [[ "$*" != *"--no-db"* ]]; then
  : "${SUPABASE_URL:?❌ SUPABASE_URL 미설정. .env 파일을 만들거나 export 하세요}"
  : "${SUPABASE_KEY:?❌ SUPABASE_KEY 미설정}"
fi

exec python3 run_local.py "$@"
