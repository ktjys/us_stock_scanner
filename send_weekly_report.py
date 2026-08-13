import os
import requests
from supabase import create_client

db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

def main():
    rows = db.table("signals").select("*").execute().data or []
    if not rows:
        text = "📈 미국주식 주간 리포트\n\n아직 신호 데이터가 없습니다."
    else:
        lines = ["📈 미국주식 매수신호 주간 리포트", "",
                 f"누적 신호: {len(rows)}개", ""]
        for n, key in [(5,"return_5d"),(10,"return_10d"),(20,"return_20d")]:
            vals = [r[key] for r in rows if r.get(key) is not None]
            if vals:
                avg = sum(vals)/len(vals)
                win = sum(v > 0 for v in vals)/len(vals)*100
                lines.append(f"{n}일: 평균 {avg:+.2f}% | 승률 {win:.1f}% | 표본 {len(vals)}")
            else:
                lines.append(f"{n}일: 데이터 부족")
        text = "\n".join(lines)

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text}, timeout=15)
    r.raise_for_status()
    print(text)

if __name__ == "__main__":
    main()
