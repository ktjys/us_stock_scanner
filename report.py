"""주간 리포트를 콘솔에 출력한다 (로컬 확인용)."""

import argparse
import os

from supabase import create_client

from stock_scanner import _fetch_all
from weekly_report import build_report_text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weeks", type=int, default=None, help="집계 기간(주). 생략 시 전체")
    args = parser.parse_args()
    weeks = args.weeks if args.weeks and args.weeks > 0 else None

    db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    rows = _fetch_all(db.table("signals").select("*"))
    print(build_report_text(rows, weeks))


if __name__ == "__main__":
    main()
