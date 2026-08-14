// dashboard/config.example.js
// ---------------------------------------------------------------------------
// Supabase anon(public) 키 설정 방법
//   1) Supabase 콘솔 → 프로젝트 선택 → 좌측 "Settings" → "API" 메뉴 진입
//   2) "Project URL" 값을 복사해 supabaseUrl 에 입력
//   3) "Project API keys" 섹션의 "anon public" 키를 복사해 supabaseAnonKey 에 입력
//      ⚠️ service_role 키가 아니라 반드시 anon public 키만 사용할 것
//         (anon 키는 쓰기 권한이 없어 안전 — 대시보드는 읽기 전용)
//   4) 이 파일을 복사해 config.js 로 저장:  cp config.example.js config.js
//      (config.js 는 실제 키가 들어가므로 .gitignore 에 추가 권장)
// ---------------------------------------------------------------------------
window.DASHBOARD_CONFIG = {
  supabaseUrl: "https://YOUR-PROJECT.supabase.co",
  supabaseAnonKey: "YOUR-ANON-PUBLIC-KEY",
};
