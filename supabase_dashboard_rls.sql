-- 대시보드용 읽기 전용 정책 (브라우저 anon 키가 대시보드에서 조회만 가능하도록)
-- 주의: 스캐너가 쓰는 SUPABASE_KEY는 service_role 키여야 합니다. anon 키면 RLS 활성화로 쓰기가 거부됩니다.
alter table public.watchlist enable row level security;
alter table public.daily_data enable row level security;
alter table public.signals enable row level security;

create policy "dashboard_read_watchlist" on public.watchlist for select to anon using (true);
create policy "dashboard_read_daily_data" on public.daily_data for select to anon using (true);
create policy "dashboard_read_signals" on public.signals for select to anon using (true);