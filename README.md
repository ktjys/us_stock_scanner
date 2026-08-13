# 미국주식 매수신호 스캐너 V4

## 구조
GitHub Actions → Yahoo Finance → Supabase PostgreSQL → Telegram

## 1. Supabase
Supabase 프로젝트를 만든 후 SQL Editor에서 `supabase_schema.sql`을 실행합니다.

## 2. GitHub Secrets
Repository → Settings → Secrets and variables → Actions에 등록:
- SUPABASE_URL
- SUPABASE_KEY
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID

## 3. 실행
평일 매일 스캔:
- daily_data 저장
- 65점 이상 signals 저장
- 5/10/20일 수익률 업데이트
- 매수 후보 Telegram 발송

매주 월요일:
- 누적 평균수익률/승률 Telegram 발송

## 4. 주의
기술적 신호는 매수 추천이나 수익을 보장하지 않습니다.
