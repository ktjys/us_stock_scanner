-- V9: Opportunity Score table for ALL watchlist tickers (not just candidates)
-- V8 Opportunity Engine evaluation results storage
-- References: supabase_v8_phase2_migration.sql style (Korean comments, idempotent)

create table if not exists opportunity_scores (
  date date not null,
  ticker text not null,
  strategy_type text,
  opportunity_score integer,
  risk_level text,
  risk_score double precision,
  signal_confidence double precision,
  classification_confidence double precision,
  technical_score integer,
  momentum_score integer,
  fundamental_score integer,
  valuation_score integer,
  components jsonb,
  primary key (date, ticker),
  foreign key (ticker) references watchlist(ticker) on delete cascade
);

-- 인덱스: 상세 조회용 (ticker, date)
create index if not exists idx_opportunity_scores_ticker_date
  on opportunity_scores(ticker, date);

-- 인덱스: 날짜별 조회용
create index if not exists idx_opportunity_scores_date
  on opportunity_scores(date);