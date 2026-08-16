-- V6 점수 버전 / 상대강도 컬럼
alter table daily_data add column if not exists score_version integer not null default 1;
alter table daily_data add column if not exists relative_strength_5d double precision;
alter table signals add column if not exists score_version integer not null default 1;

-- V6 백필 전에 기존 V1/V5 데이터를 백업하고, 필요하면 V6만 별도 비교한다.
