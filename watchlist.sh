#!/usr/bin/env bash
# watchlist 관리 편의 스크립트 (env 체크 + .env 자동 로드)
#
# 사용법:
#   ./watchlist.sh list                        # 목록 조회
#   ./watchlist.sh add TSLA AMD Tesla          # 종목 추가 (코드/이름 자동 검증·조회)
#   ./watchlist.sh remove VOO QQQ              # 스캔에서 제외 (비활성화)
#   ./watchlist.sh activate TSLA               # 다시 활성화
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

exec python3 manage_watchlist.py "$@"
