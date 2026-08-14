"""주간 리포트 텍스트 생성 공용 모듈 (send_weekly_report / report 에서 공유)."""

from datetime import datetime, timedelta, timezone
from typing import Any

RETURN_KEYS = [(5, "return_5d"), (10, "return_10d"), (20, "return_20d")]


def build_report_text(rows: list[dict[str, Any]], weeks: int | None = None,
                      backtest_summary: str | None = None) -> str:
    """signals 행 목록을 받아 주간 리포트 텍스트를 생성한다.

    weeks 지정 시 해당 기간(주) 내 signal_date 행만 집계한다.
    backtest_summary가 주어지면 리포트 하단에 추가한다 (빈 문자열이면 생략).
    """
    if weeks is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(weeks=weeks)).date().isoformat()
        rows = [r for r in rows if str(r.get("signal_date", ""))[:10] >= cutoff]
    if not rows:
        text = "📈 미국주식 매수신호 주간 리포트\n\n아직 신호 데이터가 없습니다."
    else:
        lines = ["📈 미국주식 매수신호 주간 리포트", "",
                 f"누적 신호: {len(rows)}개", ""]
        for n, key in RETURN_KEYS:
            vals = [r[key] for r in rows if r.get(key) is not None]
            if vals:
                avg = sum(vals) / len(vals)
                win = sum(v > 0 for v in vals) / len(vals) * 100
                lines.append(f"{n}일: 평균 {avg:+.2f}% | 승률 {win:.1f}% | 표본 {len(vals)}")
            else:
                lines.append(f"{n}일: 데이터 부족")
        text = "\n".join(lines)
    if backtest_summary:
        text += "\n\n" + backtest_summary
    return text
