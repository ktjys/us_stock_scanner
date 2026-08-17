-- V10: Decision engine migration
-- Add decision column to core tables and indexes for decision tracking
-- PostgreSQL syntax: COMMENT ON COLUMN (not inline COMMENT)

-- ---------------------------------------------------------------------------
-- opportunity_scores: V8 기회점수 기반 판단(decision) 저장
-- ---------------------------------------------------------------------------

alter table opportunity_scores
  add column if not exists decision text;

comment on column opportunity_scores.decision is 'V8 판단: STRONG_OPPORTUNITY, OPPORTUNITY, WATCH, NEUTRAL, AVOID';

create index if not exists idx_opportunity_scores_decision
  on opportunity_scores(decision);

-- ---------------------------------------------------------------------------
-- signals: V8 신호에 대한 판단(decision) 저장
-- ---------------------------------------------------------------------------

alter table signals
  add column if not exists decision text;

comment on column signals.decision is 'V8 신호 판단: STRONG_OPPORTUNITY, OPPORTUNITY, WATCH, NEUTRAL, AVOID';

create index if not exists idx_signals_decision
  on signals(decision);

-- ---------------------------------------------------------------------------
-- daily_data: 대시보드 일별 데이터에 판단(decision) 저장
-- ---------------------------------------------------------------------------

alter table daily_data
  add column if not exists decision text;

comment on column daily_data.decision is 'V8 일일 판단: STRONG_OPPORTUNITY, OPPORTUNITY, WATCH, NEUTRAL, AVOID';

create index if not exists idx_daily_data_decision
  on daily_data(decision);
