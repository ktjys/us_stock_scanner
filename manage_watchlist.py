"""watchlist 종목 관리 스크립트.

사용법:
  python manage_watchlist.py list                        # 목록 조회
  python manage_watchlist.py add TSLA AMD Tesla          # 추가 (코드/이름 자동 검증·조회)
  python manage_watchlist.py remove VOO QQQ              # 스캔에서 제외 (비활성화)
  python manage_watchlist.py activate TSLA               # 다시 활성화

add는 Yahoo Finance로 종목을 검증한 뒤에만 저장한다. 유효하지 않은 코드는
추가되지 않는다. 회사명 검색은 영문만 지원한다 (한글 미지원).

주의: remove는 행 삭제가 아니라 active=false 비활성화다.
행을 삭제하면 테이블이 비어 watchlist.csv가 다시 시드되는 문제가 생긴다.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf
from supabase import create_client

WATCHLIST_FILE = "watchlist.csv"


def _load_dotenv() -> None:
    """스크립트와 같은 디렉토리의 .env를 로드 (이미 설정된 환경변수 우선)."""
    path = Path(__file__).with_name(".env")
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _get_db() -> Any:
    _load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        sys.exit("❌ SUPABASE_URL/SUPABASE_KEY가 필요합니다. .env 파일을 만들거나 export 하세요.")
    return create_client(url, key)


def _ensure_table(db: Any) -> None:
    try:
        db.table("watchlist").select("ticker").limit(1).execute()
    except Exception as e:  # noqa: BLE001
        if "PGRST205" in str(e):
            sys.exit("❌ watchlist 테이블이 없습니다. Supabase SQL Editor에서 supabase_schema.sql을 실행하세요.")
        raise


# ---------------------------------------------------------------------------
# 종목 검증/조회 (Yahoo Finance)
# ---------------------------------------------------------------------------


def _name_from_ticker(code: str) -> str | None:
    """종목코드 → 회사명. 무효 코드면 None.

    Yahoo가 점(.) 형식(BRK.B)에서 이름을 안 주는 경우가 있어
    대시 형식(BRK-B)으로 한 번 더 시도한다.
    """
    alt = code.replace(".", "-")
    for cand in (code, alt) if alt != code else (code,):
        try:
            info = yf.Ticker(cand).info
        except Exception:  # noqa: BLE001 - 무효 코드는 404 예외 발생
            continue
        name = info.get("longName") or info.get("shortName")
        if name:
            return name
    return None


def _ticker_from_name(query: str) -> tuple[str, str] | None:
    try:
        quotes = yf.Search(query, max_results=5).quotes
    except Exception:  # noqa: BLE001
        return None
    for q in quotes:
        if q.get("quoteType") in ("EQUITY", "ETF"):
            symbol = q.get("symbol")
            name = q.get("longname") or q.get("shortname")
            if symbol and name:
                return symbol, name
    return None


def resolve_symbol(item: str) -> tuple[str, str] | None:
    code = item.strip().upper()
    name = _name_from_ticker(code)
    if name:
        return code, name
    return _ticker_from_name(item.strip())


def _seed_csv(db: Any) -> None:
    """테이블이 비어 있을 때 watchlist.csv의 종목을 시드한다 (스캐너와 동일 동작)."""
    try:
        df = pd.read_csv(WATCHLIST_FILE)
    except FileNotFoundError:
        print(f"⚠️ {WATCHLIST_FILE} 파일이 없어 시드를 건너뜁니다.")
        return
    payload = [{"ticker": r["ticker"], "name": r["name"], "active": True}
               for _, r in df.iterrows()]
    db.table("watchlist").upsert(payload, on_conflict="ticker").execute()
    print(f"✅ 테이블이 비어 있어 {WATCHLIST_FILE}의 {len(payload)}종목을 시드했습니다.")


def cmd_list(db: Any) -> int:
    rows = (db.table("watchlist")
            .select("ticker,name,active")
            .order("ticker")
            .execute().data or [])
    if not rows:
        print("watchlist가 비어 있습니다.")
        return 0
    for r in rows:
        mark = "✅" if r.get("active") else "🚫"
        print(f"{mark} {r['ticker']:<6} {r.get('name', '')}")
    return 0


def cmd_add(db: Any, items: list[str]) -> int:
    if not (db.table("watchlist").select("ticker").limit(1).execute().data or []):
        _seed_csv(db)

    existing = {r["ticker"] for r in db.table("watchlist").select("ticker").execute().data or []}
    resolved: list[tuple[str, str]] = []
    for item in items:
        result = resolve_symbol(item)
        if result is None:
            print(f"❌ {item}: 종목을 찾을 수 없습니다 (코드/영문 이름 확인) - 추가 안 됨")
            continue
        resolved.append(result)

    if not resolved:
        print("추가된 종목이 없습니다.")
        return 1

    payload = [{"ticker": t, "name": n, "active": True} for t, n in resolved]
    db.table("watchlist").upsert(payload, on_conflict="ticker").execute()
    for t, n in resolved:
        if t in existing:
            print(f"ℹ️ {t} ({n}): 이미 있음 → 활성화")
        else:
            print(f"✅ {t} ({n}) 추가됨")
    print("다음 스캔(./run_local.sh)부터 반영됩니다.")
    return 0


def cmd_set_active(db: Any, tickers: list[str], active: bool) -> int:
    for t in tickers:
        found = (db.table("watchlist")
                 .select("ticker")
                 .eq("ticker", t)
                 .execute().data or [])
        if not found:
            print(f"⚠️ {t}: watchlist에 없는 종목입니다 (add로 추가하세요)")
            continue
        db.table("watchlist").update({"active": active}).eq("ticker", t).execute()
        print(f"{'✅' if active else '🚫'} {t} {'활성화됨' if active else '제외됨 (스캔에서 빠짐)'}")
    return 0


def cmd_sync_csv(db: Any) -> int:
    rows = (db.table("watchlist")
            .select("ticker,name,active")
            .eq("active", True)
            .order("ticker")
            .execute().data or [])

    if not rows:
        print("📭 watchlist 테이블에 active=True 종목이 없어 CSV를 쓰지 않습니다.")
        print("   종목을 add하거나 activate로 활성화한 뒤 다시 sync-csv를 실행하세요.")
        return 0

    payload = [(r["ticker"], r.get("name") or "") for r in rows]
    df = pd.DataFrame(payload, columns=["ticker", "name"])
    df.to_csv(WATCHLIST_FILE, index=False)
    print(f"watchlist.csv 갱신 완료 ({len(rows)}종목)")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="watchlist 종목 관리")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="현재 목록 조회")

    p_add = sub.add_parser("add", help="종목 추가 (코드/이름 자동 검증·조회)")
    p_add.add_argument("items", nargs="+", help="종목코드 또는 영문 회사명 (여러 개 가능)")

    for name, help_ in [("remove", "스캔에서 제외 (비활성화)"),
                        ("activate", "다시 활성화")]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("tickers", nargs="+")

    p_sync = sub.add_parser("sync-csv", help="DB 기준으로 watchlist.csv 동기화")

    args = parser.parse_args()
    db = _get_db()
    _ensure_table(db)

    ret = 0
    if args.command == "list":
        ret = cmd_list(db)
    elif args.command == "add":
        ret = cmd_add(db, args.items)
    elif args.command == "remove":
        ret = cmd_set_active(db, args.tickers, active=False)
    elif args.command == "sync-csv":
        ret = cmd_sync_csv(db)
    else:  # activate
        ret = cmd_set_active(db, args.tickers, active=True)
    sys.exit(ret)


if __name__ == "__main__":
    main()
