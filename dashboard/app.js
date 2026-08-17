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
    ["status", "scoreboard", "detail", "signals", "backtest", "heatmap"].forEach(function (s) {
      $("screen-" + s).classList.toggle("hidden", s !== screen);
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
      "daily_data",
      async function () {
        // 최신 스캔 날짜
        var dRes = await sb
          .from("daily_data")
          .select("date")
          .eq("score_version", 8)
          .order("date", { ascending: false })
          .limit(1);
        if (dRes.error) throw dRes.error;
        var latest = dRes.data && dRes.data.length ? dRes.data[0].date : null;

        var rows = [];
        if (latest) {
          var rRes = await sb.from("daily_data").select("*").eq("date", latest).eq("score_version", 8);
          if (rRes.error) throw rRes.error;
          rows = rRes.data || [];
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
      "daily_data(날짜목록)",
      async function () {
        // 최근 10개 영업일(중복 제거) — date 전용 1000행 조회 후 dedupe
        var res = await sb
          .from("daily_data")
          .select("date")
          .eq("score_version", 8)
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
      "daily_data(" + date + ")",
      async function () {
        var res = await sb.from("daily_data").select("*").eq("date", date).eq("score_version", 8);
        if (res.error) throw res.error;
        sbData = res.data || [];
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
      "daily_data(히트맵)",
      async function () {
        var rows = await fetchAllPaged(
          sb.from("daily_data").select("*").eq("score_version", 8).order("date", { ascending: false }).order("ticker", { ascending: true })
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
          map[r.ticker][r.date] = r.score;
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
  }

  async function loadDetail() {
    await runLoad(
      "daily_data(시계열)",
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

        var rows = await fetchAllPaged(
          sb.from("daily_data").select("*").eq("ticker", ticker).eq("score_version", 8).order("date", { ascending: true })
        );
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
          { label:"점수", data:rows.map(function(r){return r.score;}), borderColor:DETAIL_COLORS.score, yAxisID:"y", borderWidth:2, pointRadius:0, tension:.12, spanGaps:true }
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

  window.loadHeatmap = loadHeatmap;

  // ---- 부트스트랩 ----
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
