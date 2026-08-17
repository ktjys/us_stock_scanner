-- V8 Phase 4: 날짜/시간 필드 통일 (signal_date / data_as_of / scanned_at)
-- 신호 생성 시각(scanned_at)과 시장 데이터 기준일(data_as_of)을
-- signal_date(신호 생성일)와 구분해 저장한다. (idempotent, 재실행 가능)

alter table signals
  add column if not exists scanned_at timestamptz,
  add column if not exists data_as_of date;
