-- 대시보드용 읽기 전용 정책 (브라우저 anon 키가 대시보드에서 조회만 가능하도록)
-- 주의: 스캐너가 쓰는 SUPABASE_KEY는 service_role 키여야 합니다. anon 키면 RLS 활성화로 쓰기가 거부됩니다.
-- 재실행 가능 (drop policy if exists → create policy 순서라 여러 번 실행해도 안전)
alter table public.watchlist enable row level security;
alter table public.daily_data enable row level security;
alter table public.signals enable row level security;
alter table public.asset_classification enable row level security;
alter table public.opportunity_scores enable row level security;

drop policy if exists "dashboard_read_watchlist" on public.watchlist;
drop policy if exists "dashboard_read_daily_data" on public.daily_data;
drop policy if exists "dashboard_read_signals" on public.signals;
drop policy if exists "dashboard_read_asset_classification" on public.asset_classification;
drop policy if exists "dashboard_read_opportunity_scores" on public.opportunity_scores;

create policy "dashboard_read_watchlist" on public.watchlist for select to anon using (true);
create policy "dashboard_read_daily_data" on public.daily_data for select to anon using (true);
create policy "dashboard_read_signals" on public.signals for select to anon using (true);
create policy "dashboard_read_asset_classification" on public.asset_classification for select to anon using (true);
create policy "dashboard_read_opportunity_scores" on public.opportunity_scores for select to anon using (true);