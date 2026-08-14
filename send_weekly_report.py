"""주간 리포트를 Telegram으로 발송한다 (매주 월요일 실행)."""

import argparse
import os

import requests
from supabase import create_client

from backtest import build_backtest_summary
from stock_scanner import _fetch_all
from weekly_report import build_report_text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weeks", type=int, default=12, help="집계 기간(주). 0이하 또는 생략 시 전체")
    args = parser.parse_args()
    weeks = args.weeks if args.weeks and args.weeks > 0 else None
    bt_weeks = args.weeks if args.weeks and args.weeks > 0 else 26

    db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    rows = _fetch_all(db.table("signals").select("*"))
    backtest_summary = build_backtest_summary(weeks=bt_weeks)
    text = build_report_text(rows, weeks, backtest_summary)

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text}, timeout=15)
    r.raise_for_status()
    print(text)


if __name__ == "__main__":
    main()
