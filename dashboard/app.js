// dashboard/app.js — 정적 대시보드 SPA (빌드 도구 없음, UMD CDN 사용)
//
// 헬퍼 요약:
//   fetchAllPaged(query)  : Supabase 1000행 제한을 .range() 루프로 우회해 전체 수집
//   computeStats(rows)    : weekly_report.build_report_text 와 동일 계산 (단순평균/양수비율)
//   fmt*()                : 숫자/통화/퍼센트 포맷
//   runLoad(label,fn,...) : 로딩 스피너 + 테이블별 에러 표시 래퍼
//   showConfigNeeded()    : config.js 부재 시 안내

(function () {
  "use strict";

  var SCORE_THRESHOLD = 55; // stock_scanner.ALERT_SCORE 와 동일
  var NEAR_THRESHOLD = 50;  // V8 근접 후보 임계점 (50~54점)
  var PAGE_SIZE = 1000;     // Supabase REST 1회 최대 행 수

  // ---- V8 전략군 한글 라벨 ----
  var STRATEGY_LABELS = {
    general: "일반",
    quality: "우량주",
    established_growth: "성장주",
    speculative: "고변동",
    broad_market_etf: "시장ETF",
    growth_etf: "성장ETF",
    sector_etf: "섹터ETF",
    dividend_etf: "배당ETF",
    income_etf: "소득ETF",
    other_etf: "기타ETF"
  };
  function strategyLabel(t) {
    return STRATEGY_LABELS[t] || t || "-";
  }

  // ---- V8 BUY/WATCH 판단 규칙 (사용자 승인) ----
  // 기회점수 >= 55 이고 리스크가 VERY_HIGH 가 아니면 BUY, 그 외 WATCH
  function judgeSignal(score, riskLevel) {
    if (score != null && score >= SCORE_THRESHOLD && riskLevel !== "VERY_HIGH") return "BUY";
    return "WATCH";
  }

  // V8 행의 점수: opportunity_score 우선, 없으면 레거시 score
  function rowScore(r) {
    return r.opportunity_score != null ? r.opportunity_score : r.score;
  }

  // ---- 배지 HTML 헬퍼 ----
  function strategyBadge(r) {
    return '<span class="badge badge-strategy">' + strategyLabel(r.strategy_type) + "</span>";
  }
  function riskBadge(r) {
    var lvl = r.risk_level || "-";
    return '<span class="badge badge-risk risk-' + String(lvl).toLowerCase() + '">' + lvl + "</span>";
  }
  function judgeBadge(r) {
    var j = judgeSignal(rowScore(r), r.risk_level);
    return '<span class="badge badge-judge judge-' + j.toLowerCase() + '">' + j + "</span>";
  }

  // ---- 설정 로드 (config.js 가 window.DASHBOARD_CONFIG 로 노출) ----
  var CONFIG = window.DASHBOARD_CONFIG || null;
  var sb = null; // supabase 클라이언트

  if (CONFIG && CONFIG.supabaseUrl && CONFIG.supabaseAnonKey) {
    try {
      sb = window.supabase.createClient(CONFIG.supabaseUrl, CONFIG.supabaseAnonKey);
    } catch (e) {
      sb = null;
    }
  }

  // ---- 공유 상태 ----
  var nameMap = {};      // ticker -> name (전체 watchlist)
  var activeTickers = []; // 활성 watchlist ticker 목록
  var charts = { detail: [] };
  var currentScreen = "status";
  var detailView = "default";
  var detailCustom = { price: true, volume: true, rsi: true, score: true };
  var detailRows = [];

  // ---- 포맷터 ----
  function fmtPrice(v) {
    return v == null || isNaN(v) ? "-" : "$" + Number(v).toFixed(2);
  }
  function fmtNum(v, d) {
    if (v == null || isNaN(v)) return "-";
    return Number(v).toFixed(d == null ? 1 : d);
  }
  function fmtPct(v) {
    if (v == null || isNaN(v)) return "-";
    return (v >= 0 ? "+" : "") + Number(v).toFixed(2) + "%";
  }
  function pctClass(v) {
    if (v == null || isNaN(v)) return "neutral";
    return v >= 0 ? "pos" : "neg";
  }
  function todayUTC() {
    return new Date().toISOString().slice(0, 10); // YYYY-MM-DD (UTC)
  }
  function dateMinusDays(days) {
    var d = new Date();
    d.setUTCDate(d.getUTCDate() - days);
    return d.toISOString().slice(0, 10);
  }
  function daysSince(dateStr) {
    var t = new Date(dateStr + "T00:00:00Z").getTime();
    return Math.floor((Date.now() - t) / 86400000);
  }

  // ---- Supabase 1000행 제한 우회 ----
  // query: select() 로 시작된 빌더 (.order 등은 미리 적용). 매 루프 .range() 재설정.
  async function fetchAllPaged(query) {
    var all = [];
    var from = 0;
    while (true) {
      var res = await query.range(from, from + PAGE_SIZE - 1);
      if (res.error) throw res.error;
      var page = res.data || [];
      for (var i = 0; i < page.length; i++) all.push(page[i]);
      if (page.length < PAGE_SIZE) break;
      from += PAGE_SIZE;
    }
    return all;
  }

  // ---- opportunity_scores ↔ daily_data(score_version=8) 조인 ----
  // opportunity_scores 에는 price/rsi/ma20/ma50/close/volume_ratio/
  // relative_strength_5d/drawdown/prev_rsi 가 없으므로, 동일 (date,ticker) 의
  // daily_data(score_version=8) 를 별도 fetch 해 병합한다.
  // oppRows: opportunity_scores 배열 (date, ticker 포함)
  // 반환: daily_data v8 필드가 보강된 병합 행 배열
  async function joinDailyV8(oppRows) {
    if (!oppRows || !oppRows.length) return oppRows || [];
    var dates = {};
    oppRows.forEach(function (r) { if (r.date) dates[r.date] = true; });
    var dateList = Object.keys(dates);
    if (!dateList.length) return oppRows;
    var daily = await fetchAllPaged(
      sb.from("daily_data").select("*").eq("score_version", 8).in("date", dateList)
    );
    var map = {};
    daily.forEach(function (d) { map[d.date + "|" + d.ticker] = d; });
    return oppRows.map(function (o) {
      var d = map[o.date + "|" + o.ticker];
      // daily_data v8 이 우선 채워지고, opportunity_scores 필드(strategy_type/risk_level/
      // opportunity_score/4축 점수 등)가 덮어쓴다.
      return d ? Object.assign({}, d, o) : o;
    });
  }

  // ---- weekly_report.build_report_text 와 동일 통계 ----
  // 평균 = 단순평균, 승률 = 양수비율*100, null 제외
  function computeStats(rows) {
    var keys = [
      [5, "return_5d"],
      [10, "return_10d"],
      [20, "return_20d"],
    ];
    var out = { total: rows.length, byKey: {} };
    keys.forEach(function (pair) {
      var n = pair[0], key = pair[1];
      var vals = rows
        .filter(function (r) { return r[key] != null && !isNaN(r[key]); })
        .map(function (r) { return r[key]; });
      if (vals.length) {
        var sum = vals.reduce(function (a, b) { return a + b; }, 0);
        var win = vals.filter(function (v) { return v > 0; }).length / vals.length * 100;
        out.byKey[key] = { avg: sum / vals.length, win: win, count: vals.length };
      } else {
        out.byKey[key] = null;
      }
    });
    return out;
  }

  // ---- 로딩/에러 래퍼 ----
  function runLoad(label, fn, loadingEl, errorEl) {
    loadingEl.classList.remove("hidden");
    errorEl.classList.add("hidden");
    return Promise.resolve()
      .then(fn)
      .catch(function (e) {
        errorEl.textContent = label + " 조회 실패: " + (e && e.message ? e.message : e);
        errorEl.classList.remove("hidden");
      })
      .then(function () {
        loadingEl.classList.add("hidden");
      });
  }

  function showConfigNeeded(errorEl) {
    errorEl.textContent = "Supabase 설정이 필요합니다. config.example.js 를 config.js 로 복사 후 키를 입력하세요.";
    errorEl.classList.remove("hidden");
  }

  function $(id) { return document.getElementById(id); }

  // =====================================================================
  // 초기화
  // =====================================================================
  function init() {
    if (!sb) {
      $("config-banner").classList.remove("hidden");
    }
    if (window.Chart) {
      Chart.defaults.color = "#98a2b3";
      Chart.defaults.borderColor = "#2a313d";
      Chart.defaults.font.family =
        '-apple-system, BlinkMacSystemFont, "Segoe UI", "Malgun Gothic", "Noto Sans KR", sans-serif';
    }
    bindTabs();
    bindDetailControls();
    bindSignalsControls();
    bindScoreboardControls();
    bindWatchlistControls();

    if (sb) {
      loadWatchlist()
        .then(loadStatus)
        .catch(function (e) {
          $("status-error").textContent =
            "watchlist 조회 실패: " + (e && e.message ? e.message : e);
          $("status-error").classList.remove("hidden");
        });
    } else {
      // 설정 없이도 화면은 렌더링 (빈 상태)
      $("status-content").classList.remove("hidden");
      showConfigNeeded($("status-error"));
    }
  }

  // ---- watchlist (이름 매핑 + 활성 목록) ----
  async function loadWatchlist() {
    var res = await sb.from("watchlist").select("ticker,name,active");
    if (res.error) throw res.error;
    var rows = res.data || [];
    nameMap = {};
    activeTickers = [];
    rows.forEach(function (r) {
      nameMap[r.ticker] = r.name || r.ticker;
      if (r.active) activeTickers.push(r.ticker);
    });
    activeTickers.sort();
  }

  function tickerLabel(tk) {
    var nm = nameMap[tk];
    var container = document.createElement("span");
    if (nm) {
      var text = document.createTextNode(tk);
      container.appendChild(text);
      var nmSpan = document.createElement("span");
      nmSpan.className = "nm";
      nmSpan.textContent = nm;
      container.appendChild(nmSpan);
    } else {
      container.textContent = tk;
    }
    return container;
  }

  // =====================================================================
  // 탭 전환
  // =====================================================================
  function bindTabs() {
    var tabs = $("tabs").querySelectorAll(".tab-btn");
    tabs.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var screen = btn.getAttribute("data-screen");
        if (screen === currentScreen) return;
        switchScreen(screen);
      });
    });
  }

  function switchScreen(screen, arg) {
    // 상세 탭을 떠날 때 차트 정리 (중복 캔버스 버그 방지)
    if (currentScreen === "detail") destroyCharts();
    currentScreen = screen;
    $("tabs").querySelectorAll(".tab-btn").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-screen") === screen);
    });
    ["status", "scoreboard", "detail", "signals", "backtest", "heatmap", "watchlist", "settings"].forEach(function (s) {
      var el = $("screen-" + s);
      if (el) el.classList.toggle("hidden", s !== screen);
    });
    if (screen === "backtest") { loadBacktest(); return; }
    if (!sb) {
      showConfigNeeded($(screen + "-error"));
      return;
    }
    if (screen === "status") loadStatus();
    else if (screen === "scoreboard") loadScoreboardDates();
    else if (screen === "detail") { if (arg) setDetailTicker(arg); loadDetail(); }
    else if (screen === "signals") loadSignals();
    else if (screen === "heatmap") loadHeatmap();
    else if (screen === "watchlist") loadWatchlistScreen();
    else if (screen === "settings") loadSettings();
  }

  function goToDetail(ticker) {
    switchScreen("detail", ticker);
  }

  function setDetailTicker(ticker) {
    var sel = $("detail-ticker");
    if (!sel.options.length) {
      activeTickers.forEach(function (tk) {
        var o = document.createElement("option");
        o.value = tk; o.textContent = tk + (nameMap[tk] ? " · " + nameMap[tk] : "");
        sel.appendChild(o);
      });
    }
    sel.value = ticker;
  }

  function destroyCharts() {
    if (charts.detail && Array.isArray(charts.detail)) {
      charts.detail.forEach(function (c) { if (c) c.destroy(); });
    }
    charts.detail = [];
  }

  // =====================================================================
  // 화면 1: 현황
  // =====================================================================
  async function loadStatus() {
    await runLoad(
      "opportunity_scores",
      async function () {
        // 최신 스캔 날짜 (opportunity_scores 기준)
        var dRes = await sb
          .from("opportunity_scores")
          .select("date")
          .order("date", { ascending: false })
          .limit(1);
        if (dRes.error) throw dRes.error;
        var latest = dRes.data && dRes.data.length ? dRes.data[0].date : null;

        var rows = [];
        if (latest) {
          var rRes = await sb.from("opportunity_scores").select("*").eq("date", latest);
          if (rRes.error) throw rRes.error;
          rows = rRes.data || [];
        // 가격/RSI/MA 등은 daily_data(score_version=8) 와 조인
          rows = await joinDailyV8(rows);
        }

        var candidates = rows
          .filter(function (r) { return rowScore(r) >= SCORE_THRESHOLD; })
          .sort(function (a, b) { return rowScore(b) - rowScore(a); });

        $("status-latest-date").textContent = latest || "데이터 없음";
        // 스캔 중단 감지: 오늘 UTC 기준 3일 초과 시 경고. 주말(토/일)은 미표시,
        // 월요일은 주말 보정(금요일 스캔=3일차까지 정상)하여 오경고 방지.
        var banner = $("status-scan-banner");
        if (!latest) {
          banner.classList.add("hidden");
        } else {
          var diff = daysSince(latest);
          var dow = new Date().getUTCDay();
          var warn = false;
          if (dow !== 0 && dow !== 6) {
            var allowed = dow === 1 ? 3 : 2;
            warn = diff > allowed;
          }
          if (warn) {
            banner.textContent = "⚠️ 스캔 중단됨 — 마지막 스캔: " + latest + " (" + diff + "일 전)";
            banner.classList.remove("hidden");
          } else {
            banner.classList.add("hidden");
          }
        }
        $("status-candidate-count").textContent = candidates.length;
        $("status-active-count").textContent = activeTickers.length;

        var top = candidates.slice(0, 3);
        var box = $("status-top-candidates");
        box.innerHTML = "";
        if (!top.length) {
          box.innerHTML = '<div class="empty">해당 날짜 후보 종목이 없습니다.</div>';
        } else {
          top.forEach(function (r) {
            var card = document.createElement("div");
            card.className = "candidate-card";
            card.innerHTML =
              '<div class="ticker">' + r.ticker + "</div>" +
              '<div class="name">' + (nameMap[r.ticker] || "") + "</div>" +
              '<div class="card-badges">' + strategyBadge(r) + riskBadge(r) + judgeBadge(r) + "</div>" +
              '<div class="metrics">' +
              '<div class="metric"><div class="m-label">점수</div><div class="m-value">' + rowScore(r) + "</div></div>" +
              '<div class="metric"><div class="m-label">RSI</div><div class="m-value">' + fmtNum(r.rsi) + "</div></div>" +
              '<div class="metric"><div class="m-label">고점대비</div><div class="m-value ' + pctClass(r.drawdown) + '">' + fmtNum(r.drawdown) + "%</div></div>" +
              "</div>";
            box.appendChild(card);
          });
        }
        // ---- 근접 후보 (60~64점) 섹션 ----
        var nearCandidates = rows
          .filter(function (r) { return rowScore(r) >= NEAR_THRESHOLD && rowScore(r) < SCORE_THRESHOLD; })
          .sort(function (a, b) { return rowScore(b) - rowScore(a); });

        var nearTitle = $("status-near-title");
        var nearBox = $("status-near-candidates");

        if (nearCandidates.length) {
          nearTitle.classList.remove("hidden");
          nearBox.innerHTML = "";
          nearCandidates.slice(0, 3).forEach(function (r) {
            var card = document.createElement("div");
            card.className = "candidate-card near";
            card.innerHTML =
              '<div class="ticker">' + r.ticker + "</div>" +
              '<div class="name">' + (nameMap[r.ticker] || "") + "</div>" +
              '<div class="card-badges">' + strategyBadge(r) + riskBadge(r) + judgeBadge(r) + "</div>" +
              '<div class="metrics">' +
              '<div class="metric"><div class="m-label">점수</div><div class="m-value">' + rowScore(r) + "</div></div>" +
              '<div class="metric"><div class="m-label">RSI</div><div class="m-value">' + fmtNum(r.rsi) + "</div></div>" +
              '<div class="metric"><div class="m-label">고점대비</div><div class="m-value ' + pctClass(r.drawdown) + '">' + fmtNum(r.drawdown) + "%</div></div>" +
              "</div>";
            nearBox.appendChild(card);
          });
        } else {
          nearTitle.classList.add("hidden");
          nearBox.innerHTML = "";
        }
        $("status-content").classList.remove("hidden");
      },
      $("status-loading"),
      $("status-error")
    );
  }

  // =====================================================================
  // 화면 2: 점수판
  // =====================================================================
  var SB_COLS = [
    { key: "ticker", label: "종목", num: false },
    { key: "strategy_type", label: "전략", num: false },
    { key: "risk_level", label: "리스크", num: false },
    { key: "score", label: "점수", num: true },
    { key: "rsi", label: "RSI", num: true },
    { key: "prev_rsi", label: "전일RSI", num: true },
    { key: "drawdown", label: "고점대비%", num: true },
    { key: "ma20", label: "MA20", num: true },
    { key: "ma50", label: "MA50", num: true },
    { key: "volume_ratio", label: "거래량비", num: true },
  ];
  var sbData = [];
  var sbSort = { key: "score", dir: "desc" };

  function bindScoreboardControls() {
    $("scoreboard-date").addEventListener("change", function () {
      loadScoreboardRows(this.value);
    });
    $("scoreboard-filter-rsi").addEventListener("change", renderScoreboard);
    $("scoreboard-filter-ma50").addEventListener("change", renderScoreboard);
    $("scoreboard-filter-vol").addEventListener("change", renderScoreboard);
    // 헤더 정렬은 행 로드 후 동적 생성
  }

  // 점수판 클라이언트 필터 (서버 재조회 없음)
  function getFilteredSbData() {
    var rsi = $("scoreboard-filter-rsi").value;
    var ma = $("scoreboard-filter-ma50").value;
    var vol = $("scoreboard-filter-vol").value;
    return sbData.filter(function (r) {
      if (rsi === "oversold" && !(r.rsi < 35)) return false;
      if (rsi === "weak" && !(r.rsi >= 35 && r.rsi < 40)) return false;
      if (rsi === "neutral" && !(r.rsi >= 40 && r.rsi < 60)) return false;
      if (rsi === "overheat" && !(r.rsi >= 60)) return false;
      if (ma === "above" && !(r.close > r.ma50)) return false;
      if (ma === "below" && !(r.close < r.ma50)) return false;
      if (vol === "high" && !(r.volume_ratio >= 1.2)) return false;
      if (vol === "low" && !(r.volume_ratio < 1.2)) return false;
      return true;
    });
  }

  async function loadScoreboardDates() {
    await runLoad(
      "opportunity_scores(날짜목록)",
      async function () {
        // 최근 10개 영업일(중복 제거) — date 전용 1000행 조회 후 dedupe
        var res = await sb
          .from("opportunity_scores")
          .select("date")
          .order("date", { ascending: false })
          .limit(1000);
        if (res.error) throw res.error;
        var seen = {};
        var dates = [];
        (res.data || []).forEach(function (r) {
          if (!seen[r.date]) { seen[r.date] = true; dates.push(r.date); }
        });
        dates = dates.slice(0, 10);

        var sel = $("scoreboard-date");
        sel.innerHTML = "";
        if (!dates.length) {
          sel.innerHTML = '<option value="">날짜 없음</option>';
          return;
        }
        dates.forEach(function (d) {
          var o = document.createElement("option");
          o.value = d; o.textContent = d;
          sel.appendChild(o);
        });
        loadScoreboardRows(dates[0]);
      },
      $("scoreboard-loading"),
      $("scoreboard-error")
    );
  }

  async function loadScoreboardRows(date) {
    if (!date) return;
    await runLoad(
      "opportunity_scores(" + date + ")",
      async function () {
        var res = await sb.from("opportunity_scores").select("*").eq("date", date);
        if (res.error) throw res.error;
        sbData = res.data || [];
        // 가격/RSI/MA 등은 daily_data(score_version=8) 와 조인
        sbData = await joinDailyV8(sbData);
        renderScoreboard();
        $("scoreboard-content").classList.remove("hidden");
      },
      $("scoreboard-loading"),
      $("scoreboard-error")
    );
  }

  function renderScoreboard() {
    // 헤더
    var head = $("scoreboard-head");
    head.innerHTML = "";
    SB_COLS.forEach(function (col) {
      var th = document.createElement("th");
      th.textContent = col.label;
      if (sbSort.key === col.key) {
        var arrow = document.createElement("span");
        arrow.className = "arrow";
        arrow.textContent = sbSort.dir === "asc" ? "▲" : "▼";
        th.appendChild(arrow);
      }
      th.addEventListener("click", function () {
        if (sbSort.key === col.key) {
          sbSort.dir = sbSort.dir === "asc" ? "desc" : "asc";
        } else {
          sbSort.key = col.key;
          sbSort.dir = col.num ? "desc" : "asc";
        }
        renderScoreboard();
      });
      head.appendChild(th);
    });

    var filtered = getFilteredSbData();
    var sorted = filtered.slice().sort(function (a, b) {
      var av = a[sbSort.key], bv = b[sbSort.key];
      if (typeof av === "string") {
        return sbSort.dir === "asc" ? av.localeCompare(bv) : bv.localeCompare(av);
      }
      av = av == null ? -Infinity : av;
      bv = bv == null ? -Infinity : bv;
      return sbSort.dir === "asc" ? av - bv : bv - av;
    });

    var body = $("scoreboard-body");
    body.innerHTML = "";
    if (!sbData.length) {
      body.innerHTML = '<tr><td colspan="' + SB_COLS.length + '" class="empty">데이터가 없습니다.</td></tr>';
      return;
    }
    if (!sorted.length) {
      body.innerHTML = '<tr><td colspan="' + SB_COLS.length + '" class="empty">필터 조건에 맞는 종목 없음</td></tr>';
      return;
    }
    sorted.forEach(function (r) {
      var tr = document.createElement("tr");
      if (rowScore(r) >= SCORE_THRESHOLD) tr.className = "candidate";
      SB_COLS.forEach(function (col) {
        var td = document.createElement("td");
        if (col.key === "ticker") {
          td.className = "ticker-cell";
          td.appendChild(tickerLabel(r.ticker));
        } else if (col.key === "strategy_type") {
          td.innerHTML = strategyLabel(r.strategy_type);
        } else if (col.key === "risk_level") {
          td.innerHTML = riskBadge(r);
        } else if (col.key === "drawdown") {
          td.className = pctClass(r.drawdown);
          td.textContent = fmtNum(r.drawdown) + "%";
        } else if (col.key === "volume_ratio") {
          td.textContent = fmtNum(r.volume_ratio, 2);
        } else if (col.key === "score") {
          td.textContent = rowScore(r);
        } else {
          td.textContent = fmtNum(r[col.key]);
        }
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
  }

  // =====================================================================
  // 화면 5: 히트맵 (점수 그리드)
  // =====================================================================
  function heatClass(score) {
    if (score <= 0) return 0;       // 회색
    if (score < 35) return 1;       // 짙은 파랑
    if (score < 60) return 2;       // 중간 파랑
    if (score < 65) return 3;       // 주황 (근접)
    return 4;                       // 빨강 (신호)
  }

  async function loadHeatmap() {
    await runLoad(
      "opportunity_scores(히트맵)",
      async function () {
        var rows = await fetchAllPaged(
          sb.from("opportunity_scores").select("*").order("date", { ascending: false }).order("ticker", { ascending: true })
        );
        // 최근 10개 날짜 (중복 제거, date desc 상태)
        var seen = {};
        var dates = [];
        rows.forEach(function (r) {
          if (!seen[r.date]) { seen[r.date] = true; dates.push(r.date); }
        });
        dates = dates.slice(0, 10).sort(); // 열은 오래된→최신 순

        var map = {};
        rows.forEach(function (r) {
          if (map[r.ticker] === undefined) map[r.ticker] = {};
          map[r.ticker][r.date] = r.opportunity_score;
        });

        var tickers = activeTickers.slice();
        var box = $("heatmap-grid");
        box.innerHTML = "";
        if (!tickers.length || !dates.length) {
          box.innerHTML = '<div class="empty">히트맵을 표시할 데이터가 없습니다.</div>';
          $("heatmap-content").classList.remove("hidden");
          return;
        }

        var table = document.createElement("table");
        table.className = "heatmap-table";
        var thead = document.createElement("thead");
        var htr = document.createElement("tr");
        var thTk = document.createElement("th");
        thTk.textContent = "종목";
        htr.appendChild(thTk);
        dates.forEach(function (d) {
          var th = document.createElement("th");
          th.textContent = d.slice(5);
          th.title = d;
          htr.appendChild(th);
        });
        thead.appendChild(htr);
        table.appendChild(thead);

        var tbody = document.createElement("tbody");
        tickers.forEach(function (tk) {
          var tr = document.createElement("tr");
          var tdTk = document.createElement("td");
          tdTk.className = "ticker-cell";
          tdTk.appendChild(tickerLabel(tk));
          tr.appendChild(tdTk);
          dates.forEach(function (d) {
            var td = document.createElement("td");
            var score = map[tk] ? map[tk][d] : undefined;
            if (score == null) {
              td.className = "hm-empty";
              td.textContent = "·";
            } else {
              td.className = "hm-" + heatClass(score);
              td.textContent = score;
              td.title = tk + " · " + d + " · 점수 " + score;
              td.addEventListener("click", function () { goToDetail(tk); });
            }
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        box.appendChild(table);
        $("heatmap-content").classList.remove("hidden");
      },
      $("heatmap-loading"),
      $("heatmap-error")
    );
  }

  // =====================================================================
  // 화면 3: 상세
  // =====================================================================
  var detailPeriod = 3; // 개월

  function bindDetailControls() {
    $("detail-ticker").addEventListener("change", function () {
      loadDetail();
    });
    $("detail-periods").querySelectorAll("button").forEach(function (btn) {
      btn.addEventListener("click", function () {
        detailPeriod = parseInt(btn.getAttribute("data-period"), 10);
        $("detail-periods").querySelectorAll("button").forEach(function (b) {
          b.classList.toggle("active", b === btn);
        });
        loadDetail();
      });
    });

    $("detail-view-buttons").querySelectorAll("button").forEach(function (btn) {
      btn.addEventListener("click", function () {
        detailView = btn.getAttribute("data-view");
        $("detail-view-buttons").querySelectorAll("button").forEach(function (b) {
          b.classList.toggle("active", b === btn);
        });
        $("detail-custom-controls").classList.toggle("hidden", detailView !== "custom");
        if (detailRows.length) renderDetailCharts(detailRows);
      });
    });

    $("detail-custom-controls").querySelectorAll("input[type=checkbox]").forEach(function (input) {
      input.addEventListener("change", function () {
        detailCustom[input.getAttribute("data-indicator")] = input.checked;
        if (detailRows.length) renderDetailCharts(detailRows);
      });
    });

    $("detail-debug-btn").addEventListener("click", function () {
      var ticker = $("detail-ticker").value;
      if (!ticker) return;
      loadDebugInfo(ticker);
    });
    $("detail-debug-close").addEventListener("click", function () {
      $("detail-debug").classList.add("hidden");
    });
  }

  async function loadDetail() {
    await runLoad(
      "opportunity_scores+daily_data(시계열)",
      async function () {
        var sel = $("detail-ticker");
        if (!sel.options.length) {
          activeTickers.forEach(function (tk) {
            var o = document.createElement("option");
            o.value = tk; o.textContent = tk + (nameMap[tk] ? " · " + nameMap[tk] : "");
            sel.appendChild(o);
          });
        }
        var ticker = sel.value;
        if (!ticker) {
          $("detail-content").classList.remove("hidden");
          return;
        }

        // V8 기회점수(opportunity_scores) + 가격/RSI/MA 시계열(daily_data v8) 조인
        var oppRows = await fetchAllPaged(
          sb.from("opportunity_scores").select("*").eq("ticker", ticker).order("date", { ascending: true })
        );
        var dailyRows = await fetchAllPaged(
          sb.from("daily_data").select("*").eq("ticker", ticker).eq("score_version", 8).order("date", { ascending: true })
        );
        var omap = {};
        oppRows.forEach(function (o) { omap[o.date] = o; });
        var seen = {};
        var rows = [];
        dailyRows.forEach(function (d) {
          seen[d.date] = true;
          var o = omap[d.date];
          rows.push(o ? Object.assign({}, d, o) : d);
        });
        // opportunity_scores 전용 날짜(가격 없음)도 점수 표시용으로 포함
        oppRows.forEach(function (o) {
          if (!seen[o.date]) rows.push(o);
        });
        var cutoff = dateMinusDays(detailPeriod * 30);
        rows = rows.filter(function (r) { return r.date >= cutoff; });

        if (!rows.length) {
          destroyCharts();
          detailRows = [];
          $("detail-content").classList.remove("hidden");
          return;
        }
        detailRows = rows;
        renderDetailCharts(rows);
        $("detail-content").classList.remove("hidden");
      },
      $("detail-loading"),
      $("detail-error")
    );
  }

  var DETAIL_COLORS = {
    price: "#5b8def",
    ma20: "#f5a623",
    ma50: "#b67cff",
    volume: "#6fd08c",
    rsi: "#ff6b8a",
    score: "#4dd0e1",
    ref: "rgba(152,162,179,.45)"
  };

  function fmtMoney(v) {
    if (v == null || !isFinite(Number(v))) return "-";
    return "$" + Number(v).toFixed(2);
  }

  function detailPanelSpecs() {
    if (detailView === "price_score") return ["price", "score"];
    if (detailView === "price_rsi") return ["price", "rsi"];
    if (detailView === "price_volume") return ["price", "volume"];
    if (detailView === "all") return ["price", "volume", "rsi", "score"];

    if (detailView === "custom") {
      var out = [];
      if (detailCustom.price) out.push("price");
      if (detailCustom.volume) out.push("volume");
      if (detailCustom.rsi) out.push("rsi");
      if (detailCustom.score) out.push("score");
      return out.length ? out : ["price"];
    }
    return ["price", "volume", "rsi", "score"];
  }

  function detailCrosshairPlugin() {
    return {
      id: "detailCrosshair",
      afterDraw: function (chart) {
        var idx = chart.$detailSelectedIndex;
        if (idx == null) return;
        var meta = chart.getDatasetMeta(0);
        var point = meta && meta.data ? meta.data[idx] : null;
        if (!point) return;
        var x = point.x;
        var area = chart.chartArea;
        var ctx = chart.ctx;
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(x, area.top);
        ctx.lineTo(x, area.bottom);
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.strokeStyle = "rgba(220,225,235,.72)";
        ctx.stroke();
        ctx.restore();
      }
    };
  }

  function updateDetailSelection(index) {
    if (index == null || !detailRows[index]) return;
    var r = detailRows[index];
    $("detail-selected-date").textContent = r.date;
    $("detail-tooltip-summary").innerHTML =
      '<div><b>가격</b><strong>' + fmtMoney(r.price) + '</strong></div>' +
      '<div><b>MA20</b><strong>' + fmtMoney(r.ma20) + '</strong></div>' +
      '<div><b>MA50</b><strong>' + fmtMoney(r.ma50) + '</strong></div>' +
      '<div><b>점수</b><strong>' + fmtNum(rowScore(r), 0) + '</strong></div>' +
      '<div><b>RSI</b><strong>' + fmtNum(r.rsi, 1) + '</strong></div>' +
      '<div><b>거래량</b><strong>' + fmtNum(r.volume_ratio, 2) + 'x</strong></div>' +
      '<div><b>QQQ 대비 5일</b><strong>' + fmtPct(r.relative_strength_5d) + '</strong></div>' +
      '<div><b>전략</b><strong>' + strategyLabel(r.strategy_type) + '</strong></div>' +
      '<div><b>리스크</b><strong>' + (r.risk_level || "-") + '</strong></div>' +
      '<div><b>기술</b><strong>' + fmtNum(r.technical_score) + '</strong></div>' +
      '<div><b>모멘텀</b><strong>' + fmtNum(r.momentum_score) + '</strong></div>' +
      '<div><b>펀더멘털</b><strong>' + fmtNum(r.fundamental_score) + '</strong></div>' +
      '<div><b>밸류</b><strong>' + fmtNum(r.valuation_score) + '</strong></div>';

    (charts.detail || []).forEach(function (chart) {
      chart.$detailSelectedIndex = index;
      var active = [{ datasetIndex: 0, index: index }];
      chart.setActiveElements(active);
      var y = chart.scales.y ? chart.scales.y.getPixelForValue(chart.data.datasets[0].data[index]) : chart.chartArea.top;
      chart.tooltip.setActiveElements(active, { x: chart.scales.x.getPixelForValue(index), y: y });
      chart.update("none");
    });
  }

  function detailDataset(panel, rows) {
    var labels = rows.map(function (r) { return r.date; });
    if (panel === "price") {
      return {
        title: "가격 · 이동평균",
        datasets: [
          { label: "가격", data: rows.map(function(r){return r.price;}), borderColor: DETAIL_COLORS.price, yAxisID:"y", borderWidth:2.4, pointRadius:0, tension:.12, spanGaps:true },
          { label: "MA20", data: rows.map(function(r){return r.ma20;}), borderColor: DETAIL_COLORS.ma20, yAxisID:"y", borderWidth:1.6, pointRadius:0, tension:.12, spanGaps:true },
          { label: "MA50", data: rows.map(function(r){return r.ma50;}), borderColor: DETAIL_COLORS.ma50, yAxisID:"y", borderWidth:1.6, pointRadius:0, tension:.12, spanGaps:true }
        ],
        scales: { y: { position:"left", title:{display:true,text:"가격"} } }
      };
    }
    if (panel === "rsi") {
      var ref35 = labels.map(function(){return 35;});
      var ref40 = labels.map(function(){return 40;});
      return {
        title: "RSI",
        datasets: [
          { label:"RSI", data:rows.map(function(r){return r.rsi;}), borderColor:DETAIL_COLORS.rsi, yAxisID:"y", borderWidth:2, pointRadius:0, tension:.12, spanGaps:true },
          { label:"RSI 35", data:ref35, borderColor:DETAIL_COLORS.ref, yAxisID:"y", borderWidth:1, borderDash:[5,4], pointRadius:0 },
          { label:"RSI 40", data:ref40, borderColor:DETAIL_COLORS.ref, yAxisID:"y", borderWidth:1, borderDash:[5,4], pointRadius:0 }
        ],
        scales: { y:{min:0,max:100,position:"left",title:{display:true,text:"RSI"}} }
      };
    }
    if (panel === "score") {
      return {
        title: "V8 점수",
        datasets: [
          { label:"점수", data:rows.map(function(r){return rowScore(r);}), borderColor:DETAIL_COLORS.score, yAxisID:"y", borderWidth:2, pointRadius:0, tension:.12, spanGaps:true }
        ],
        scales: { y:{min:0,max:100,position:"left",title:{display:true,text:"점수"}} }
      };
    }
    return {
      title: "거래량",
      datasets: [
        { label:"거래량 / 20일평균", data:rows.map(function(r){return r.volume_ratio;}), borderColor:DETAIL_COLORS.volume, backgroundColor:"rgba(111,208,140,.12)", yAxisID:"y", borderWidth:1.8, pointRadius:0, tension:.08, fill:true, spanGaps:true }
      ],
      scales: { y:{min:0,position:"left",title:{display:true,text:"배수"}} }
    };
  }

  function renderDetailCharts(rows) {
    destroyCharts();
    var host = $("detail-chart-panels");
    host.innerHTML = "";
    var labels = rows.map(function (r) { return r.date; });
    var panels = detailPanelSpecs();

    panels.forEach(function(panel, panelIndex) {
      var box = document.createElement("div");
      box.className = "detail-panel";
      var head = document.createElement("div");
      head.className = "detail-panel-title";
      var spec = detailDataset(panel, rows);
      head.textContent = spec.title;
      box.appendChild(head);
      var wrap = document.createElement("div");
      wrap.className = "detail-panel-canvas";
      var canvas = document.createElement("canvas");
      wrap.appendChild(canvas);
      box.appendChild(wrap);
      host.appendChild(box);

      var chart = new Chart(canvas, {
        type: "line",
        plugins: [detailCrosshairPlugin()],
        data: { labels: labels, datasets: spec.datasets },
        options: {
          responsive:true,
          maintainAspectRatio:false,
          interaction:{mode:"index",intersect:false},
          onHover:function(event, active) {
            if (active && active.length) updateDetailSelection(active[0].index);
          },
          onClick:function(event, active) {
            if (active && active.length) updateDetailSelection(active[0].index);
          },
          plugins:{
            legend:{display:true,position:"top",labels:{boxWidth:10,padding:8,font:{size:10}}},
            tooltip:{
              mode:"index",intersect:false,
              callbacks:{
                title:function(items){return items.length ? items[0].label : "";},
                label:function(ctx){
                  var v=ctx.parsed.y;
                  if(v==null)return null;
                  if(panel==="price") return ctx.dataset.label+": "+fmtMoney(v);
                  if(panel==="volume") return ctx.dataset.label+": "+fmtNum(v,2)+"x";
                  return ctx.dataset.label+": "+fmtNum(v,1);
                }
              }
            }
          },
          scales:{
            x:{ticks:{maxTicksLimit:9,autoSkip:true,maxRotation:0}},
            y:{beginAtZero:panel!=="price", ...(spec.scales.y||{})}
          }
        }
      });
      charts.detail.push(chart);
    });

    var last = labels.length - 1;
    if (last >= 0) updateDetailSelection(last);
  }
  // =====================================================================
  // 화면 4: 신호·성과
  // =====================================================================
  function bindSignalsControls() {
    $("signals-period").addEventListener("change", function () {
      loadSignals();
    });
    $("signals-go-heatmap").addEventListener("click", function () {
      switchScreen("heatmap");
    });
    $("signals-go-status").addEventListener("click", function () {
      switchScreen("status");
    });
  }

  async function loadSignals() {
    await runLoad(
      "signals",
      async function () {
        var weeks = parseInt($("signals-period").value, 10);
        var rows = await fetchAllPaged(sb.from("signals").select("*").eq("score_version", 8).order("signal_date", { ascending: false }));

        // 기간 필터: 오늘(UTC) - N주, signal_date >= cutoff (문자열 비교)
        if (weeks > 0) {
          var cutoff = dateMinusDays(weeks * 7);
          rows = rows.filter(function (r) {
            return String(r.signal_date).slice(0, 10) >= cutoff;
          });
        }

        var stats = computeStats(rows);
        renderSignalsStats(stats);
        renderSignalsTable(rows);
        $("signals-content").classList.remove("hidden");
      },
      $("signals-loading"),
      $("signals-error")
    );
  }

  function renderSignalsStats(stats) {
    var box = $("signals-stats");
    box.innerHTML = "";
    function card(label, value, cls) {
      var d = document.createElement("div");
      d.className = "stat-card";
      d.innerHTML =
        '<div class="stat-label">' + label + "</div>" +
        '<div class="stat-value ' + (cls || "") + '">' + value + "</div>";
      box.appendChild(d);
    }
    card("누적 신호 수", stats.total);
    [["return_5d", "평균수익률 5일"], ["return_10d", "평균수익률 10일"], ["return_20d", "평균수익률 20일"]].forEach(function (p) {
      var s = stats.byKey[p[0]];
      if (s) card(p[1], fmtPct(s.avg), pctClass(s.avg));
      else card(p[1], "데이터 부족", "neutral");
    });
    var w5 = stats.byKey["return_5d"];
    card("승률 (5일 기준)", w5 ? w5.win.toFixed(1) + "%" : "-", w5 && w5.win >= 50 ? "pos" : "neg");
  }

  function renderSignalsTable(rows) {
    var body = $("signals-body");
    body.innerHTML = "";
    if (!rows.length) {
      $("signals-table-wrap").classList.add("hidden");
      $("signals-empty").classList.remove("hidden");
      return;
    }
    $("signals-table-wrap").classList.remove("hidden");
    $("signals-empty").classList.add("hidden");
    rows.forEach(function (r) {
      var tr = document.createElement("tr");
      function cell(html, cls) {
        var td = document.createElement("td");
        if (cls) td.className = cls;
        td.innerHTML = html;
        return td;
      }
      tr.appendChild(cell(String(r.signal_date).slice(0, 10)));
      var tdTk = document.createElement("td");
      tdTk.className = "ticker-cell";
      tdTk.appendChild(tickerLabel(r.ticker));
      tr.appendChild(tdTk);
      tr.appendChild(cell(strategyLabel(r.strategy_type)));
      tr.appendChild(cell(riskBadge(r), "badge-cell"));
      tr.appendChild(cell(String(r.opportunity_score != null ? r.opportunity_score : (r.score != null ? r.score : "-"))));
      tr.appendChild(cell(judgeBadge(r), "badge-cell"));
      tr.appendChild(cell(fmtPrice(r.signal_price)));
      tr.appendChild(cell(String(r.score != null ? r.score : "-")));
      var conf = r.signal_confidence;
      tr.appendChild(cell(conf == null || isNaN(conf) ? "-" : Math.round(conf * 100) + "%"));
      [r.return_5d, r.return_10d, r.return_20d].forEach(function (v) {
        if (v == null || isNaN(v)) tr.appendChild(cell("대기", "neutral"));
        else tr.appendChild(cell(fmtPct(v), pctClass(v)));
      });
      tr.appendChild(cell(fmtPct(r.benchmark_return), "neutral"));
      tr.appendChild(cell(fmtPct(r.excess_return), pctClass(r.excess_return)));
      body.appendChild(tr);
    });
  }

  // =====================================================================
  // 화면 6: 백테스트 (정적 JSON, Supabase 불함)
  // =====================================================================
  var backtestChart = null;
  var backtestLoaded = false;
  var backtestData = null;
  var backtestVersion = "v8";
  var backtestToggleBound = false;

  function loadBacktest() {
    if (backtestLoaded) return;
    backtestLoaded = true;
    var loadingEl = $("backtest-loading");
    var emptyEl = $("backtest-empty");
    loadingEl.classList.remove("hidden");
    emptyEl.classList.add("hidden");
    fetch("data/backtest.json")
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        loadingEl.classList.add("hidden");
        backtestData = data;
        if (!data) {
          emptyEl.textContent = "백테스트 데이터가 없습니다. GitHub Actions에서 Run Backtest를 실행하세요.";
          emptyEl.classList.remove("hidden");
          return;
        }
        // version:"both" 형식(modes.v7/v8) 지원, 구버전 단일(v7/v6) 폴백 유지
        var isBoth = !!(data.modes && (data.modes.v7 || data.modes.v8));
        var toggle = $("backtest-version-toggle");
        if (isBoth) {
          toggle.classList.remove("hidden");
          // 기본 V8 (V8 데이터가 없으면 V7로 폴백)
          backtestVersion = (data.modes.v8 && data.modes.v8.bands && data.modes.v8.bands.length) ? "v8" : "v7";
          var radios = toggle.querySelectorAll('input[name="bt-version"]');
          radios.forEach(function (r) { r.checked = (r.value === backtestVersion); });
          bindBacktestToggle();
        } else {
          toggle.classList.add("hidden");
          backtestVersion = null; // 단일 형식
          if (!data.bands || !data.bands.length) {
            emptyEl.textContent = "백테스트 데이터가 없습니다. GitHub Actions에서 Run Backtest를 실행하세요.";
            emptyEl.classList.remove("hidden");
            return;
          }
        }
        renderBacktest(data, backtestVersion);
        $("backtest-content").classList.remove("hidden");
      })
      .catch(function () {
        loadingEl.classList.add("hidden");
        backtestLoaded = false;
        emptyEl.classList.remove("hidden");
      });
  }

  function bindBacktestToggle() {
    if (backtestToggleBound) return;
    backtestToggleBound = true;
    var toggle = $("backtest-version-toggle");
    function syncLabels() {
      toggle.querySelectorAll('input[name="bt-version"]').forEach(function (radio) {
        radio.parentNode.classList.toggle("active", radio.checked);
      });
    }
    toggle.querySelectorAll('input[name="bt-version"]').forEach(function (radio) {
      radio.addEventListener("change", function () {
        if (!this.checked) return;
        backtestVersion = this.value;
        syncLabels();
        renderBacktest(backtestData, backtestVersion);
      });
    });
    syncLabels();
  }

  function renderBacktest(data, version) {
    // both 형식이면 선택된 모드, 단일 형식이면 data 자체를 모드로 사용
    var mode = (data.modes && version) ? data.modes[version] : data;
    if (!mode || !mode.bands || !mode.bands.length) {
      $("backtest-content").classList.add("hidden");
      var emptyEl = $("backtest-empty");
      emptyEl.textContent = "선택한 버전의 백테스트 데이터가 없습니다.";
      emptyEl.classList.remove("hidden");
      return;
    }
    $("backtest-empty").classList.add("hidden");
    $("backtest-content").classList.remove("hidden");

    var titleEl = $("backtest-chart-title");
    if (titleEl) titleEl.textContent = (version ? version.toUpperCase() + " " : "") + "점수구간별 성과";

    var info = $("backtest-info");
    var parts = [];
    if (data.generated_at) {
      var d = new Date(data.generated_at);
      parts.push("생성: " + (isNaN(d.getTime()) ? data.generated_at : d.toLocaleString()));
    }
    if (data.period_start && data.period_end) {
      parts.push("기간: " + data.period_start + " ~ " + data.period_end);
    }
    if (data.ticker_count != null) parts.push("종목 수: " + data.ticker_count);
    if (data.cooldown_days != null) parts.push("중복 신호 cooldown: " + data.cooldown_days + "일");
    // 원신호→채택 카운트는 선택된 모드의 값 사용
    if (mode.raw_signal_count != null && mode.cooldown_signal_count != null) {
      parts.push("원신호 " + mode.raw_signal_count + " → 채택 " + mode.cooldown_signal_count);
    }
    info.innerHTML = parts.map(function (p) { return "<span>" + p + "</span>"; }).join("");

    var bands = mode.bands || [];
    renderBacktestChart(bands);
    renderBacktestThresholdTable(bands);
    renderBacktestRecentTable(mode.recent_signals || []);
  }

  function renderBacktestChart(bands) {
    if (backtestChart) { backtestChart.destroy(); backtestChart = null; }
    var labels = bands.map(function (b) { return b.band + "점"; });
    var signalData = bands.map(function (b) { return b.signals; });
    var winData = bands.map(function (b) { return b.win_rate == null ? null : b.win_rate; });
    var avg20 = bands.map(function (b) { return b.avg_20d == null ? null : b.avg_20d; });

    backtestChart = new Chart($("chart-backtest"), {
      data: {
        labels: labels,
        datasets: [
          {
            type: "bar",
            label: "신호수",
            data: signalData,
            backgroundColor: "rgba(91,141,239,0.55)",
            yAxisID: "y",
            order: 3,
          },
          {
            type: "line",
            label: "5일 승률(%)",
            data: winData,
            borderColor: "#f5a623",
            backgroundColor: "#f5a623",
            borderWidth: 2,
            pointRadius: 3,
            tension: 0.1,
            yAxisID: "y1",
            order: 1,
          },
          {
            type: "line",
            label: "20일 평균수익률(%)",
            data: avg20,
            borderColor: "#4dd0e1",
            backgroundColor: "#4dd0e1",
            borderWidth: 2,
            pointRadius: 3,
            tension: 0.1,
            yAxisID: "y2",
            order: 2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { labels: { boxWidth: 12 } },
          tooltip: { mode: "index", intersect: false }
        },
        scales: {
          x: { ticks: { maxTicksLimit: 10, autoSkip: true } },
          y: { position: "left", beginAtZero: true, title: { display: true, text: "신호수" } },
          y1: {
            position: "right", beginAtZero: true, min: 0, max: 100,
            grid: { drawOnChartArea: false },
            title: { display: true, text: "승률(%)" },
          },
          y2: {
            display: false, position: "right",
            grid: { drawOnChartArea: false }
          }
        },
      },
    });
  }

  function renderBacktestThresholdTable(bands) {
    var body = $("backtest-threshold-body");
    body.innerHTML = "";
    bands.forEach(function (b) {
      var tr = document.createElement("tr");
      function cell(html, cls) {
        var td = document.createElement("td");
        if (cls) td.className = cls;
        td.innerHTML = html;
        return td;
      }
      tr.appendChild(cell(String(b.band)));
      tr.appendChild(cell(String(b.signals)));
      tr.appendChild(cell(b.win_rate == null ? "-" : fmtNum(b.win_rate) + "%"));
      [b.avg_5d, b.avg_10d, b.avg_20d, b.avg_mae_5d, b.avg_mfe_5d].forEach(function (v) {
        if (v == null || isNaN(v)) tr.appendChild(cell("-"));
        else tr.appendChild(cell(fmtPct(v), pctClass(v)));
      });
      tr.appendChild(cell(String(b.sample_size)));
      body.appendChild(tr);
    });
  }

  function renderBacktestRecentTable(signals) {
    var body = $("backtest-recent-body");
    body.innerHTML = "";
    if (!signals.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty">최근 신호가 없습니다.</td></tr>';
      return;
    }
    signals.slice(0, 20).forEach(function (r) {
      var tr = document.createElement("tr");
      function cell(html, cls) {
        var td = document.createElement("td");
        if (cls) td.className = cls;
        td.innerHTML = html;
        return td;
      }
      tr.appendChild(cell(String(r.date).slice(0, 10)));
      var tdTk = document.createElement("td");
      tdTk.className = "ticker-cell";
      tdTk.appendChild(tickerLabel(r.ticker));
      tr.appendChild(tdTk);
      tr.appendChild(cell(String(r.score)));
      [r.ret5, r.ret10, r.ret20].forEach(function (v) {
        if (v == null || isNaN(v)) tr.appendChild(cell("-"));
        else tr.appendChild(cell(fmtPct(v), pctClass(v)));
      });
      body.appendChild(tr);
    });
  }

  // =====================================================================
  // 화면 7: Watchlist (C1) + 분류 편집 (C2)
  // =====================================================================
  var wlData = [];        // 최신 스캔 기준 opportunity_scores + asset_classification 병합
  var wlClassMap = {};    // ticker -> asset_classification 행
  var wlModal = null;     // 분류 편집 모달

  function bindWatchlistControls() {
    $("wl-filter-strategy").addEventListener("change", renderWatchlist);
    $("wl-filter-risk").addEventListener("change", renderWatchlist);
    $("wl-filter-decision").addEventListener("change", renderWatchlist);
  }

  // opportunity_scores.decision 우선, 없으면 BUY/WATCH 판단
  function rowDecision(r) {
    if (r.decision) return r.decision;
    return judgeSignal(rowScore(r), r.risk_level);
  }
  function decisionBadge(r) {
    var d = rowDecision(r);
    return '<span class="badge badge-decision dec-' + String(d).toLowerCase() + '">' + d + "</span>";
  }
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function loadWatchlistScreen() {
    await runLoad(
      "opportunity_scores + asset_classification",
      async function () {
        var dRes = await sb.from("opportunity_scores").select("date").order("date", { ascending: false }).limit(1);
        if (dRes.error) throw dRes.error;
        var latest = dRes.data && dRes.data.length ? dRes.data[0].date : null;

        var rows = [];
        if (latest) {
          var rRes = await sb.from("opportunity_scores").select("*").eq("date", latest);
          if (rRes.error) throw rRes.error;
          rows = rRes.data || [];
        }

        var cRes = await sb.from("asset_classification").select("*");
        if (cRes.error) throw cRes.error;
        wlClassMap = {};
        (cRes.data || []).forEach(function (c) { wlClassMap[c.ticker] = c; });

        wlData = rows.map(function (r) {
          var c = wlClassMap[r.ticker] || {};
          return Object.assign({}, r, {
            asset_type: c.asset_type || "-",
            class_confidence: c.confidence != null ? c.confidence : r.classification_confidence,
            class_source: c.classification_source || "-",
            class_reason: c.reason || ""
          });
        });

        populateWatchlistFilters();
        renderWatchlist();
        $("watchlist-content").classList.remove("hidden");
      },
      $("watchlist-loading"),
      $("watchlist-error")
    );
  }

  function populateWatchlistFilters() {
    var stratSel = $("wl-filter-strategy");
    var riskSel = $("wl-filter-risk");
    var decSel = $("wl-filter-decision");
    var curStrat = stratSel.value, curRisk = riskSel.value, curDec = decSel.value;

    var strats = {};
    wlData.forEach(function (r) { if (r.strategy_type) strats[r.strategy_type] = true; });
    stratSel.innerHTML = '<option value="">전략: 전체</option>';
    Object.keys(STRATEGY_LABELS).forEach(function (k) {
      if (strats[k]) {
        var o = document.createElement("option");
        o.value = k; o.textContent = STRATEGY_LABELS[k];
        stratSel.appendChild(o);
      }
    });

    var risks = {};
    wlData.forEach(function (r) { if (r.risk_level) risks[r.risk_level] = true; });
    riskSel.innerHTML = '<option value="">리스크: 전체</option>';
    ["LOW", "MEDIUM", "HIGH", "VERY_HIGH"].forEach(function (k) {
      if (risks[k]) {
        var o = document.createElement("option");
        o.value = k; o.textContent = k;
        riskSel.appendChild(o);
      }
    });

    var decs = {};
    wlData.forEach(function (r) { var d = rowDecision(r); if (d) decs[d] = true; });
    decSel.innerHTML = '<option value="">판단: 전체</option>';
    Object.keys(decs).forEach(function (k) {
      var o = document.createElement("option");
      o.value = k; o.textContent = k;
      decSel.appendChild(o);
    });

    stratSel.value = curStrat; riskSel.value = curRisk; decSel.value = curDec;
  }

  function renderWatchlist() {
    var strat = $("wl-filter-strategy").value;
    var risk = $("wl-filter-risk").value;
    var dec = $("wl-filter-decision").value;

    var filtered = wlData.filter(function (r) {
      if (strat && r.strategy_type !== strat) return false;
      if (risk && r.risk_level !== risk) return false;
      if (dec && rowDecision(r) !== dec) return false;
      return true;
    });

    var body = $("watchlist-table").querySelector("tbody");
    body.innerHTML = "";
    if (!filtered.length) {
      body.innerHTML = '<tr><td colspan="9" class="empty">데이터가 없습니다.</td></tr>';
      return;
    }
    filtered.forEach(function (r) {
      var tr = document.createElement("tr");

      var tdTk = document.createElement("td");
      tdTk.className = "ticker-cell";
      tdTk.appendChild(tickerLabel(r.ticker));
      tr.appendChild(tdTk);

      var tdAt = document.createElement("td");
      tdAt.textContent = r.asset_type || "-";
      tr.appendChild(tdAt);

      var tdStrat = document.createElement("td");
      tdStrat.innerHTML = strategyBadge(r);
      tr.appendChild(tdStrat);

      var tdConf = document.createElement("td");
      tdConf.textContent = (r.class_confidence == null || isNaN(r.class_confidence)) ? "-" : Math.round(r.class_confidence * 100) + "%";
      tr.appendChild(tdConf);

      var tdScore = document.createElement("td");
      tdScore.textContent = rowScore(r);
      tr.appendChild(tdScore);

      var tdRisk = document.createElement("td");
      tdRisk.innerHTML = riskBadge(r);
      tr.appendChild(tdRisk);

      var tdDec = document.createElement("td");
      tdDec.innerHTML = decisionBadge(r);
      tr.appendChild(tdDec);

      var tdDate = document.createElement("td");
      tdDate.textContent = r.date || "-";
      tr.appendChild(tdDate);

      var tdSrc = document.createElement("td");
      tdSrc.textContent = r.class_source || "-";
      tdSrc.title = r.class_reason || "";
      tr.appendChild(tdSrc);

      body.appendChild(tr);
    });
  }

  function ensureWlModal() {
    if (wlModal) return wlModal;
    var overlay = document.createElement("div");
    overlay.className = "modal-overlay hidden";
    overlay.id = "wl-modal-overlay";
    overlay.innerHTML =
      '<div class="modal-box">' +
        '<div class="modal-head"><h3>분류 편집</h3>' +
        '<button type="button" class="modal-close" id="wl-modal-close">✕</button></div>' +
        '<div class="modal-body" id="wl-modal-body"></div>' +
      '</div>';
    document.body.appendChild(overlay);
    overlay.addEventListener("click", function (e) { if (e.target === overlay) closeWlModal(); });
    $("wl-modal-close").addEventListener("click", closeWlModal);
    wlModal = overlay;
    return wlModal;
  }
  function closeWlModal() {
    if (wlModal) wlModal.classList.add("hidden");
  }

  async function openClassificationModal(ticker) {
    var modal = ensureWlModal();
    var body = $("wl-modal-body");
    var c = wlClassMap[ticker] || {};
    var curStrat = c.strategy_type ||
      (wlData.find(function (r) { return r.ticker === ticker; }) || {}).strategy_type || "";

    var opts = Object.keys(STRATEGY_LABELS).map(function (k) {
      return '<option value="' + k + '"' + (k === curStrat ? " selected" : "") + '>' +
        STRATEGY_LABELS[k] + " (" + k + ")</option>";
    }).join("");

    body.innerHTML =
      '<div class="modal-info">' +
        '<div><span class="m-label">종목</span><strong>' + ticker + '</strong></div>' +
        '<div><span class="m-label">현재 분류</span><strong>' +
          (c.strategy_type ? STRATEGY_LABELS[c.strategy_type] + " (" + c.strategy_type + ")" : "-") + '</strong></div>' +
        '<div><span class="m-label">분류 출처</span><strong>' + (c.classification_source || "-") + '</strong></div>' +
        '<div><span class="m-label">신뢰도</span><strong>' +
          (c.confidence != null ? Math.round(c.confidence * 100) + "%" : "-") + '</strong></div>' +
        (c.reason ? '<div class="modal-reason"><span class="m-label">사유</span><p>' + escapeHtml(c.reason) + '</p></div>' : '') +
      '</div>' +
      '<label class="modal-field">전략 재분류' +
        '<select id="wl-modal-strategy">' + opts + '</select>' +
      '</label>' +
      '<div class="modal-actions">' +
        '<button type="button" class="btn-cancel" id="wl-modal-cancel">취소</button>' +
        '<button type="button" class="btn-save" id="wl-modal-save">저장</button>' +
      '</div>';

    $("wl-modal-cancel").addEventListener("click", closeWlModal);
    $("wl-modal-save").addEventListener("click", function () {
      var newStrat = $("wl-modal-strategy").value;
      saveClassificationOverride(ticker, newStrat, body);
    });

    modal.classList.remove("hidden");
  }

  async function saveClassificationOverride(ticker, newStrategy, bodyEl) {
    var saveBtn = $("wl-modal-save");
    saveBtn.disabled = true;
    saveBtn.textContent = "저장 중…";
    try {
      var res = await sb.from("asset_classification").upsert({
        ticker: ticker,
        strategy_type: newStrategy,
        classification_source: "manual",
        updated_at: new Date().toISOString()
      }, { onConflict: "ticker" });
      if (res.error) throw res.error;
      closeWlModal();
      loadWatchlistScreen();
    } catch (e) {
      if (bodyEl) {
        var err = document.createElement("div");
        err.className = "error";
        err.textContent = "저장 실패: " + (e && e.message ? e.message : e);
        bodyEl.appendChild(err);
      }
      saveBtn.disabled = false;
      saveBtn.textContent = "저장";
    }
  }

  // =====================================================================
  // 화면 8: 설정 (C3, 읽기 전용)
  // =====================================================================
  var SETTINGS_STRATEGY_RULES = {
    quality:            { strong: 65, opportunity: 55, watch: 40, neutral: 25 },
    established_growth: { strong: 65, opportunity: 55, watch: 40, neutral: 25 },
    speculative:        { strong: 70, opportunity: 65, watch: 50, neutral: 30 },
    broad_market_etf:   { strong: 60, opportunity: 50, watch: 35, neutral: 20 },
    growth_etf:         { strong: 60, opportunity: 50, watch: 35, neutral: 20 },
    dividend_etf:       { strong: 55, opportunity: 45, watch: 30, neutral: 15 },
    income_etf:         { strong: 55, opportunity: 45, watch: 30, neutral: 15 },
    sector_etf:         { strong: 60, opportunity: 50, watch: 35, neutral: 20 },
    general:            { strong: 55, opportunity: 40, watch: 25, neutral: 10 },
    other_etf:          { strong: 55, opportunity: 40, watch: 25, neutral: 10 }
  };

  function loadSettings() {
    var body = $("settings-strategy-body");
    if (body.childElementCount) return; // 정적 읽기 전용 — 한 번만 채움
    body.innerHTML = "";
    Object.keys(SETTINGS_STRATEGY_RULES).forEach(function (k) {
      var r = SETTINGS_STRATEGY_RULES[k];
      var tr = document.createElement("tr");
      function cell(html) {
        var td = document.createElement("td");
        td.innerHTML = html;
        return td;
      }
      tr.appendChild(cell(STRATEGY_LABELS[k] + " (" + k + ")"));
      tr.appendChild(cell(String(r.opportunity)));
      tr.appendChild(cell(String(r.strong)));
      tr.appendChild(cell(String(r.watch)));
      tr.appendChild(cell(String(r.neutral)));
      body.appendChild(tr);
    });
  }

  // =====================================================================
  // C4: 상세 탭 진단 패널
  // =====================================================================
  async function loadDebugInfo(ticker) {
    var panel = $("detail-debug");
    panel.classList.remove("hidden");
    await runLoad(
      "opportunity_scores + asset_classification (진단)",
      async function () {
        var oRes = await sb.from("opportunity_scores").select("*")
          .eq("ticker", ticker).order("date", { ascending: false }).limit(1);
        if (oRes.error) throw oRes.error;
        var opp = oRes.data && oRes.data.length ? oRes.data[0] : null;

        var cRes = await sb.from("asset_classification").select("*")
          .eq("ticker", ticker).limit(1);
        if (cRes.error) throw cRes.error;
        var cls = cRes.data && cRes.data.length ? cRes.data[0] : null;

        var content = $("detail-debug-content");
        if (!opp) {
          content.innerHTML = '<div class="empty">진단할 기회점수 데이터가 없습니다.</div>';
          content.classList.remove("hidden");
          return;
        }

        var comps = opp.components || {};
        var compRows = Object.keys(comps).map(function (k) {
          return '<tr><td>' + escapeHtml(k) + '</td><td>' + fmtNum(comps[k]) + '</td></tr>';
        }).join("") || '<tr><td colspan="2" class="empty">컴포넌트 없음</td></tr>';

        content.innerHTML =
          '<div class="debug-grid">' +
            '<div class="debug-card"><h4>기본 정보</h4><table class="data-table debug-table">' +
              '<tr><td>종목</td><td>' + ticker + '</td></tr>' +
              '<tr><td>스캔 날짜</td><td>' + (opp.date || "-") + '</td></tr>' +
              '<tr><td>전략</td><td>' + strategyLabel(opp.strategy_type) + ' (' + (opp.strategy_type || "-") + ')</td></tr>' +
              '<tr><td>기회점수</td><td>' + rowScore(opp) + '</td></tr>' +
              '<tr><td>판단</td><td>' + decisionBadge(opp) + '</td></tr>' +
              '<tr><td>신호 신뢰도</td><td>' + (opp.signal_confidence != null ? Math.round(opp.signal_confidence * 100) + "%" : "-") + '</td></tr>' +
              '<tr><td>분류 신뢰도</td><td>' + (opp.classification_confidence != null ? Math.round(opp.classification_confidence * 100) + "%" : "-") + '</td></tr>' +
            '</table></div>' +
            '<div class="debug-card"><h4>리스크 요인</h4><table class="data-table debug-table">' +
              '<tr><td>risk_score</td><td>' + fmtNum(opp.risk_score) + '</td></tr>' +
              '<tr><td>risk_level</td><td>' + riskBadge(opp) + '</td></tr>' +
              (cls ? '<tr><td>자산유형</td><td>' + escapeHtml(cls.asset_type || "-") + '</td></tr>' +
                     '<tr><td>분류 출처</td><td>' + escapeHtml(cls.classification_source || "-") + '</td></tr>' +
                     '<tr><td>분류 사유</td><td>' + escapeHtml(cls.reason || "-") + '</td></tr>' : '') +
            '</table></div>' +
            '<div class="debug-card debug-card-wide"><h4>14개 컴포넌트 점수</h4><table class="data-table debug-table">' +
              '<thead><tr><th>컴포넌트</th><th>점수</th></tr></thead><tbody>' + compRows + '</tbody>' +
            '</table></div>' +
            '<div class="debug-card debug-card-wide"><h4>4축 점수</h4><table class="data-table debug-table">' +
              '<tr><td>기술 (technical)</td><td>' + fmtNum(opp.technical_score) + '</td></tr>' +
              '<tr><td>모멘텀 (momentum)</td><td>' + fmtNum(opp.momentum_score) + '</td></tr>' +
              '<tr><td>펀더멘털 (fundamental)</td><td>' + fmtNum(opp.fundamental_score) + '</td></tr>' +
              '<tr><td>밸류 (valuation)</td><td>' + fmtNum(opp.valuation_score) + '</td></tr>' +
            '</table></div>' +
          '</div>';
        content.classList.remove("hidden");
      },
      $("detail-debug-loading"),
      $("detail-debug-error")
    );
  }

  window.loadHeatmap = loadHeatmap;

  // ---- 부트스트랩 ----
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
