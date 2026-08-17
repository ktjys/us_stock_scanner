-- V8 Phase 3: Return Tracking 강화
-- 신호 이후 수익률 추적을 위한 추가 컬럼

-- signals: Return Tracking 강화
alter table signals
  add column if not exists exit_price double precision,
  add column if not exists holding_days integer,
  add column if not exists benchmark_return double precision,
  add column if not exists excess_return double precision,
  add column if not exists max_drawdown_after double precision,
  add column if not exists max_runup_after double precision;

-- 인덱스: 수익률 분석용
create index if not exists idx_signals_return_5d
  on signals(return_5d) where return_5d is not null;

create index if not exists idx_signals_return_20d
  on signals(return_20d) where return_20d is not null;
