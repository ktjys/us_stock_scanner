"""주간 리포트를 콘솔에 출력한다 (로컬 확인용)."""

import argparse
import os
import sys

from supabase import create_client

from backtest import build_backtest_summary
from stock_scanner import SCORE_VERSION, _fetch_all
from weekly_report import build_report_text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weeks", type=int, default=None, help="집계 기간(주). 생략 시 전체")
    args = parser.parse_args()
    weeks = args.weeks if args.weeks and args.weeks > 0 else None
    bt_weeks = args.weeks if args.weeks and args.weeks > 0 else 26

    # env가 없으면 신호 섹션만 생략하고 백테스트 요약은 CSV 폴백으로 계속 동작
    rows: list[dict] = []
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
        try:
            db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
            rows = _fetch_all(
                db.table("signals").select("*").eq("score_version", SCORE_VERSION)
            )
        except Exception as e:  # noqa: BLE001
            print(f"경고: 신호 조회 실패, 신호 섹션 생략 - {e}", file=sys.stderr)
    else:
        print("경고: SUPABASE_URL/KEY 없음, 신호 섹션 생략", file=sys.stderr)

    backtest_summary = build_backtest_summary(weeks=bt_weeks, mode="v8")
    print(build_report_text(rows, weeks, backtest_summary))


if __name__ == "__main__":
    main()
