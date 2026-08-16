-- V8 Phase 1: 장기 투자용 종목 자동 분류
-- 기존 V7 Scanner 테이블/컬럼은 삭제하거나 변경하지 않는다.

create table if not exists asset_classification (
  ticker text primary key references watchlist(ticker) on delete cascade,
  asset_type text not null,
  strategy_type text not null,
  confidence double precision not null default 0.0,
  classification_source text not null default 'auto',
  reason text,
  classified_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint asset_classification_confidence_check
    check (confidence >= 0 and confidence <= 1),
  constraint asset_classification_source_check
    check (classification_source in ('auto', 'manual')),
  constraint asset_classification_asset_type_check
    check (asset_type in ('etf', 'equity', 'other')),
  constraint asset_classification_strategy_check
    check (strategy_type in (
      'broad_market_etf',
      'growth_etf',
      'dividend_etf',
      'sector_etf',
      'income_etf',
      'other_etf',
      'quality_blue_chip',
      'growth',
      'high_volatility_growth',
      'general_equity'
    ))
);

create index if not exists idx_asset_classification_strategy
  on asset_classification(strategy_type);

create index if not exists idx_asset_classification_source
  on asset_classification(classification_source);
