"""로컬 스캐너 실행 스크립트.

사용법:
  python run_local.py                          # 전체 실행 (DB 저장 + 텔레그램)
  python run_local.py --no-db                  # DB 저장 스킵 (Supabase 미사용)
  python run_local.py --no-telegram            # 텔레그램 대신 콘솔 출력
  python run_local.py --no-db --no-telegram    # 순수 분석 (env 변수 불필요)
  python run_local.py --date 2026-08-13        # 기준 날짜 지정
"""

import argparse
import os
import sys
from datetime import datetime

from stock_scanner import ALERT_SCORE, scan


def _require_env(name: str) -> None:
    if not os.environ.get(name):
        sys.exit(f"❌ {name} 환경변수가 없습니다. .env 파일 또는 export로 설정하세요.")


def _valid_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"날짜 형식은 YYYY-MM-DD여야 합니다: {value}")


def main() -> None:
    parser = argparse.ArgumentParser(description="미국주식 스캐너 로컬 실행")
    parser.add_argument("--no-db", action="store_true",
                        help="DB 저장/수익률 갱신 생략 (Supabase 미사용)")
    parser.add_argument("--no-telegram", action="store_true",
                        help="텔레그램 발송 대신 콘솔 출력")
    parser.add_argument("--date", type=_valid_date, default=None,
                        help="기준 날짜 YYYY-MM-DD (기본: 오늘 UTC)")
    parser.add_argument("--threshold", type=int, default=ALERT_SCORE,
                        help="신호 임계값 (기본 55)")
    args = parser.parse_args()

    if not args.no_db:
        _require_env("SUPABASE_URL")
        _require_env("SUPABASE_KEY")

    candidates, failures = scan(date=args.date,
                                persist=not args.no_db,
                                notify=not args.no_telegram,
                                threshold=args.threshold)
    print(f"\n📋 후보 {len(candidates)}개 (스캔 완료)")
    if failures:
        sys.exit(f"❌ {len(failures)}개 종목 실패: {', '.join(failures)}")


if __name__ == "__main__":
    main()
