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
      'quality',
      'established_growth',
      'speculative',
      'general'
    ))
);

create index if not exists idx_asset_classification_strategy
  on asset_classification(strategy_type);

create index if not exists idx_asset_classification_source
  on asset_classification(classification_source);

-- ---------------------------------------------------------------------------
-- 기존 DB (테이블/제약조건이 이미 존재하는 경우) 전략명 체계 교체
--
-- 분류기 출력이 speculative / established_growth / quality / general로
-- 확정됨에 따라, 이전 명명(quality_blue_chip / growth /
-- high_volatility_growth / general_equity)으로 저장된 자동 분류 행을 새 명명으로
-- 매핑한 뒤 CHECK 제약조건을 교체한다. 아래 블록은 idempotent하다.
-- ---------------------------------------------------------------------------

update asset_classification
   set strategy_type = case strategy_type
         when 'quality_blue_chip'      then 'quality'
         when 'growth'                 then 'established_growth'
         when 'high_volatility_growth' then 'speculative'
         when 'general_equity'         then 'general'
         else strategy_type
       end
 where classification_source = 'auto'
   and strategy_type in (
         'quality_blue_chip', 'growth',
         'high_volatility_growth', 'general_equity'
       );

alter table asset_classification
  drop constraint if exists asset_classification_strategy_check;

alter table asset_classification
  add constraint asset_classification_strategy_check
  check (strategy_type in (
    'broad_market_etf',
    'growth_etf',
    'dividend_etf',
    'sector_etf',
    'income_etf',
    'other_etf',
    'quality',
    'established_growth',
    'speculative',
    'general'
  ));
