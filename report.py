import os
from supabase import create_client

db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

def main():
    rows = db.table("signals").select("*").execute().data or []
    if not rows:
        print("아직 신호 데이터가 없습니다.")
        return

    msg = ["📈 미국주식 매수신호 주간 리포트", "",
           f"누적 신호: {len(rows)}개", ""]
    for n, key in [(5,"return_5d"),(10,"return_10d"),(20,"return_20d")]:
        vals = [r[key] for r in rows if r.get(key) is not None]
        if vals:
            avg = sum(vals)/len(vals)
            win = sum(v > 0 for v in vals)/len(vals)*100
            msg.append(f"{n}일: 평균 {avg:+.2f}% | 승률 {win:.1f}% | 표본 {len(vals)}")
        else:
            msg.append(f"{n}일: 데이터 부족")

    print("\n".join(msg))

if __name__ == "__main__":
    main()
