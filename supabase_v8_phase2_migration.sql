-- V8 Phase 2: 전략 인식 신호 (Opportunity Engine)
-- 기존 컬럼은 삭제/변경하지 않고, V8 전략 신호 컬럼을 추가한다.

-- signals: V8 신호 메타데이터
alter table signals
  add column if not exists strategy_type text,
  add column if not exists opportunity_score integer,
  add column if not exists risk_level text,
  add column if not exists risk_score double precision,
  add column if not exists signal_confidence double precision,
  add column if not exists classification_confidence double precision,
  add column if not exists technical_score integer,
  add column if not exists momentum_score integer,
  add column if not exists fundamental_score integer,
  add column if not exists valuation_score integer,
  add column if not exists components jsonb;

-- daily_data: 대시보드 점수판에서 전략/위험 표시용
alter table daily_data
  add column if not exists strategy_type text,
  add column if not exists opportunity_score integer,
  add column if not exists risk_level text,
  add column if not exists technical_score integer,
  add column if not exists momentum_score integer,
  add column if not exists fundamental_score integer,
  add column if not exists valuation_score integer,
  add column if not exists components jsonb;

-- 전략별 신호 집계 인덱스
create index if not exists idx_signals_strategy
  on signals(strategy_type);

create index if not exists idx_daily_data_strategy
  on daily_data(strategy_type);
