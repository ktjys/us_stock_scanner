-- V5 점수 버전 컬럼 추가
alter table daily_data add column if not exists score_version integer not null default 1;
alter table signals add column if not exists score_version integer not null default 1;

-- 앞으로 V5 스캐너가 저장하는 행은 score_version=5를 사용합니다.
-- 기존 데이터는 1로 남겨 두어 V1/V5 성과를 섞지 않도록 합니다.


-- 백필/스캐너가 명시적으로 score_version=5를 기록합니다.
-- 기존 V1 데이터는 자동으로 변경하지 않습니다.
-- V5 백필 전에 기존 daily_data/signals를 별도 백업하는 것을 권장합니다.
