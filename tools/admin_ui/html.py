"""Static HTML for the local admin UI."""

from __future__ import annotations

HTML = r"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <title>CCIM v2 관리자</title>
  <style>
    body { font-family: ui-sans-serif, system-ui, sans-serif; margin: 32px; background: #f6f4ef; color: #1e211c; }
    main { max-width: 1040px; margin: 0 auto; }
    h1 { font-size: 30px; margin-bottom: 8px; }
    section { background: #fffdf7; border: 1px solid #d8d0bd; border-radius: 14px; padding: 20px; margin: 18px 0; }
    label { display: block; font-weight: 700; margin: 12px 0 4px; }
    input { width: 100%; box-sizing: border-box; padding: 10px; border: 1px solid #b8ae99; border-radius: 8px; }
    button { margin: 8px 8px 8px 0; padding: 10px 14px; border: 0; border-radius: 8px; background: #263f2c; color: white; cursor: pointer; }
    button.secondary { background: #6f5c3d; }
    button.danger { background: #8a2f23; }
    button.toggle { background: #9f8f73; min-width: 92px; }
    button.toggle.on { background: #1f6b39; }
    button.toggle.off { background: #8a2f23; }
    pre { white-space: pre-wrap; background: #1f241f; color: #e9f0e4; padding: 14px; border-radius: 10px; max-height: 480px; overflow: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border-bottom: 1px solid #d8d0bd; padding: 8px; text-align: right; vertical-align: top; }
    th:first-child, td:first-child, th:nth-child(2), td:nth-child(2), .diagnostic { text-align: left; }
    th { background: #efe8d7; }
    .status { font-weight: 700; }
    .warn { color: #9a4b00; font-weight: 700; }
    .notice { margin-top: 12px; min-height: 20px; font-weight: 700; }
    .notice.ok { color: #1f6b39; }
    .notice.fail { color: #8a2f23; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .hidden { display: none; }
    .setting-row { margin: 12px 0; }
    .deps { display: grid; gap: 8px; margin: 12px 0; }
    .dep { padding: 10px; border-radius: 8px; border: 1px solid #d8d0bd; }
    .dep.ok { background: #edf7ef; border-color: #8ac49a; }
    .dep.fail { background: #fff0eb; border-color: #d28a75; }
    .dep strong { display: inline-block; min-width: 120px; }
    .small { color: #625a4d; font-size: 13px; }
    .summary { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin: 14px 0; }
    .card { border: 1px solid #d8d0bd; border-radius: 10px; padding: 12px; background: #fbf7ec; }
    .card h3 { margin: 0 0 8px; }
    .metric { display: flex; justify-content: space-between; gap: 16px; border-top: 1px solid #e2dac8; padding: 6px 0; }
    .chart { margin: 14px 0; padding: 12px; border: 1px solid #d8d0bd; border-radius: 10px; background: #fffaf0; overflow-x: auto; }
    .chart svg { display: block; }
    .legend { display: flex; flex-wrap: wrap; gap: 10px 18px; align-items: center; font-size: 13px; margin-bottom: 8px; }
    .legend span::before { content: ""; display: inline-block; width: 22px; height: 0; margin-right: 6px; vertical-align: middle; border-top: 3px var(--dash, solid) var(--line); }
    .details { overflow-x: auto; }
    @media (max-width: 900px) {
      body { margin: 20px; }
      .grid, .summary { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
<main>
  <h1>CCIM v2 관리자</h1>
  <p>압축 설정과 CCIM 자식 프로세스를 관리하는 로컬 페이지입니다.</p>

  <section>
    <h2>서버</h2>
    <p class="status" id="status">불러오는 중...</p>
    <p class="warn" id="restart"></p>
    <div class="deps" id="deps"></div>
    <button onclick="post('/api/start')">CCIM 시작</button>
    <button class="secondary" onclick="post('/api/restart')">CCIM 재시작</button>
    <button class="danger" onclick="post('/api/stop')">CCIM 정지</button>
    <button class="secondary" onclick="toggleSettings()">설정</button>
  </section>

  <section id="settingsSection" class="hidden">
    <h2>설정 (.env)</h2>
    <div id="settings"></div>
    <button onclick="saveSettings()">.env 저장</button>
    <div id="saveNotice" class="notice"></div>
  </section>

  <section>
    <h2>측정</h2>
    <div class="grid">
      <div><label>왼쪽 prefix</label><input id="left" value="u1"></div>
      <div><label>오른쪽 prefix</label><input id="right" value="u2"></div>
    </div>
    <label>조회 범위(분)</label><input id="since" value="120">
    <button onclick="runMeasure()">비교 실행</button>
    <button class="secondary" id="measureDiagnosticsToggle" onclick="toggleMeasureDiagnostics()">진단 상세 표시</button>
    <div id="measureNotice" class="notice"></div>
    <div id="measureSummary" class="summary"></div>
    <div id="measureChart" class="chart"></div>
    <div id="measureDetails" class="details"></div>
  </section>

  <section>
    <h2>CCIM 로그</h2>
    <button class="secondary" onclick="loadCcimLog()">로그 새로고침</button>
    <pre id="ccimLog"></pre>
  </section>
</main>
<script>
const keys = [
  "CCIM_LLM_PROVIDER",
  "CCIM_LLM_MODEL",
  "CCIM_LLM_TIMEOUT_S",
  "CCIM_LLM_BASE_URL",
  "CCIM_SESSION_PREFIX",
  "CCIM_COMPRESSION_ENABLED",
  "CCIM_COMPRESSION_TRIGGER_TOKENS",
  "CCIM_COMPRESSION_TARGET_TOKENS",
  "CCIM_COMPRESSION_ENABLE_RETRIEVE",
  "CCIM_CURRENT_TURN_COMPRESSION_ENABLED",
  "CCIM_CURRENT_TURN_COMPRESSION_TRIGGER_TOKENS",
  "CCIM_CURRENT_TURN_COMPRESSION_READ_TOOLS",
  "CCIM_COMPRESSION_CLUSTER_SUMMARY_ENABLED",
  "CCIM_COMPRESSION_WRITE_GUARD_ENABLED",
  "CCIM_COMPRESSION_WRITE_GUARD_TOOLS",
];
const boolKeys = new Set([
  "CCIM_COMPRESSION_ENABLED",
  "CCIM_COMPRESSION_ENABLE_RETRIEVE",
  "CCIM_CURRENT_TURN_COMPRESSION_ENABLED",
  "CCIM_COMPRESSION_CLUSTER_SUMMARY_ENABLED",
  "CCIM_COMPRESSION_WRITE_GUARD_ENABLED",
]);
const settingDescriptions = {
  CCIM_LLM_PROVIDER: "Upstream LLM provider입니다. OpenAI 모델 테스트는 openai를 사용합니다.",
  CCIM_LLM_MODEL: "Claude Code가 보낸 모델명을 upstream 모델명으로 치환합니다. 예: gpt-5-mini, gpt-5.1-codex-mini",
  CCIM_LLM_TIMEOUT_S: "Upstream LLM 응답 대기 시간(초)입니다. 긴 테스트에서는 300 이상을 권장합니다.",
  CCIM_LLM_BASE_URL: "OpenAI-compatible endpoint를 쓸 때만 입력합니다. OpenAI 기본 API를 쓰면 비워 둡니다.",
  CCIM_SESSION_PREFIX: "텔레메트리 실행 라벨입니다. A/B 측정 비교 시 서로 다른 prefix를 사용합니다.",
  CCIM_COMPRESSION_ENABLED: "전역 압축 스위치입니다. false면 history/current-turn/dedupe/structured 압축을 모두 건너뜁니다.",
  CCIM_COMPRESSION_TRIGGER_TOKENS: "요청 입력 토큰 추정치가 이 값 이상일 때만 압축을 시작합니다.",
  CCIM_COMPRESSION_TARGET_TOKENS: "후보 선택 목표값입니다. 낮을수록 더 많은 이전 히스토리를 압축 후보로 선택합니다.",
  CCIM_COMPRESSION_ENABLE_RETRIEVE: "모델이 context_id로 압축 전 원본 코드를 복구할 수 있도록 retrieve_original을 주입합니다.",
  CCIM_CURRENT_TURN_COMPRESSION_ENABLED: "실험 기능: 현재 요청 턴의 eligible Read 도구 결과를 압축합니다.",
  CCIM_CURRENT_TURN_COMPRESSION_TRIGGER_TOKENS: "현재 턴 Read 압축은 요청 입력 토큰 추정치가 이 값 이상일 때만 시작합니다.",
  CCIM_CURRENT_TURN_COMPRESSION_READ_TOOLS: "현재 턴 압축 대상이 되는 읽기 전용 도구 이름 목록입니다. 쉼표로 구분합니다.",
  CCIM_COMPRESSION_CLUSTER_SUMMARY_ENABLED: "실험 기능: 반복 함수 그룹을 하나의 검색 가능한 컨텍스트로 압축합니다.",
  CCIM_COMPRESSION_WRITE_GUARD_ENABLED: "실험 기능: current-turn 압축 이후 쓰기 작업을 보호합니다. 전송 오류가 아니라 retrieve 기반 게이트입니다.",
  CCIM_COMPRESSION_WRITE_GUARD_TOOLS: "current-turn write guard가 확인할 쓰기 도구 이름 목록입니다. 쉼표로 구분합니다.",
};
let lastMeasureData = null;
let showMeasureDiagnostics = false;

function headers() {
  const token = localStorage.getItem("ccim_admin_token") || "";
  return token ? {"Content-Type": "application/json", "X-CCIM-Admin-Token": token} : {"Content-Type": "application/json"};
}
async function api(path, opts={}) {
  const res = await fetch(path, {...opts, headers: {...headers(), ...(opts.headers || {})}});
  if (!res.ok) throw new Error(await res.text());
  return res.headers.get("content-type")?.includes("application/json") ? res.json() : res.text();
}
async function post(path) {
  try {
    await api(path, {method: "POST", body: "{}"});
  } catch (err) {
    alert(readableError(err));
  } finally {
    await load();
  }
}
function toggleSettings() {
  document.getElementById("settingsSection").classList.toggle("hidden");
}
function renderStatus(status) {
  const owner = status.port_owner_pid ? `, 포트 점유 pid=${status.port_owner_pid}` : "";
  document.getElementById("status").textContent = status.running ? `실행 중 pid=${status.pid}${owner}` : `정지됨${owner}`;
  document.getElementById("restart").textContent = status.restart_required_after_save ? "저장한 변경사항을 실행 중인 CCIM 프로세스에 반영하려면 재시작이 필요합니다." : "";
  renderDeps(status.dependencies || {});
}
function renderDeps(deps) {
  const box = document.getElementById("deps");
  const items = [
    ["Redis", deps.redis, "압축과 retrieve에 필요"],
    ["PostgreSQL", deps.postgres, "텔레메트리와 측정에 필요"],
    ["CCIM HTTP", deps.ccim_http, "게이트웨이 /health 엔드포인트"],
  ];
  box.innerHTML = "";
  for (const [label, dep, note] of items) {
    const div = document.createElement("div");
    const ok = dep && dep.ok;
    div.className = `dep ${ok ? "ok" : "fail"}`;
    div.innerHTML = `<strong>${label}</strong> ${ok ? "정상" : "실패"}<br><span class="small">${escapeHtml((dep && dep.url) || "")} - ${escapeHtml((dep && dep.message) || "확인 안 됨")} (${note})</span>`;
    box.append(div);
  }
  if (deps.start_blocked) {
    const warn = document.createElement("div");
    warn.className = "warn";
    warn.textContent = "Redis와 PostgreSQL에 연결될 때까지 시작/재시작이 차단됩니다. Docker 의존성을 실행한 뒤 다시 시도하세요.";
    box.append(warn);
  }
}
function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}
function readableError(err) {
  const text = err && err.message ? err.message : String(err);
  try {
    const parsed = JSON.parse(text);
    return parsed.detail || text;
  } catch {
    return text;
  }
}
function setNotice(id, text, ok=true) {
  const el = document.getElementById(id);
  el.textContent = text || "";
  el.className = `notice ${text ? (ok ? "ok" : "fail") : ""}`;
}
async function load() {
  const data = await api("/api/settings");
  renderStatus(data.status);
  const box = document.getElementById("settings");
  box.innerHTML = "";
  for (const key of keys) {
    const row = document.createElement("div");
    row.className = "setting-row";
    const label = document.createElement("label");
    label.textContent = key;
    row.append(label);
    if (boolKeys.has(key)) {
      const button = document.createElement("button");
      button.id = `env_${key}`;
      button.type = "button";
      button.dataset.value = normalizeBool(data.values[key]);
      renderToggle(button);
      button.onclick = () => {
        button.dataset.value = button.dataset.value === "true" ? "false" : "true";
        renderToggle(button);
      };
      row.append(button);
    } else {
      const input = document.createElement("input");
      input.id = `env_${key}`;
      input.value = data.values[key] || "";
      row.append(input);
    }
    const help = document.createElement("div");
    help.className = "small";
    help.textContent = settingDescriptions[key] || "";
    row.append(help);
    box.append(row);
  }
}
function normalizeBool(value) {
  return String(value || "").toLowerCase() === "true" ? "true" : "false";
}
function renderToggle(button) {
  const enabled = button.dataset.value === "true";
  button.textContent = enabled ? "true" : "false";
  button.className = `toggle ${enabled ? "on" : "off"}`;
}
async function saveSettings() {
  setNotice("saveNotice", "설정을 저장하는 중...");
  const values = {};
  for (const key of keys) {
    const el = document.getElementById(`env_${key}`);
    values[key] = boolKeys.has(key) ? el.dataset.value : el.value;
  }
  try {
    const data = await api("/api/settings", {method: "POST", body: JSON.stringify({values})});
    renderStatus(data.status);
    const suffix = data.restarted ? " CCIM을 자동으로 재시작했습니다." : " CCIM이 실행 중이 아니어서 재시작하지 않았습니다.";
    setNotice("saveNotice", "설정을 저장했습니다." + suffix, true);
    await loadCcimLog();
  } catch (err) {
    setNotice("saveNotice", readableError(err), false);
  }
}
async function runMeasure() {
  setNotice("measureNotice", "측정 데이터를 불러오는 중...");
  document.getElementById("measureSummary").innerHTML = "";
  document.getElementById("measureChart").innerHTML = "";
  document.getElementById("measureDetails").innerHTML = "";
  try {
    const data = await api("/api/measure-data", {
      method: "POST",
      body: JSON.stringify({
        left: document.getElementById("left").value,
        right: document.getElementById("right").value,
        since: Number(document.getElementById("since").value || "120"),
        verbose: true
      })
    });
    lastMeasureData = data;
    renderMeasure(data);
    setNotice("measureNotice", `최근 ${data.since}분 동안의 요청 ${data.left.summary.requests + data.right.summary.requests}개를 불러왔습니다.`, true);
  } catch (err) {
    setNotice("measureNotice", readableError(err), false);
  }
}
function toggleMeasureDiagnostics() {
  showMeasureDiagnostics = !showMeasureDiagnostics;
  updateMeasureDiagnosticsToggle();
  if (lastMeasureData) renderMeasureDetails(lastMeasureData);
}
function updateMeasureDiagnosticsToggle() {
  const button = document.getElementById("measureDiagnosticsToggle");
  if (!button) return;
  button.textContent = showMeasureDiagnostics ? "진단 상세 숨기기" : "진단 상세 표시";
}
function renderMeasure(data) {
  updateMeasureDiagnosticsToggle();
  renderMeasureSummary(data);
  renderMeasureChart(data);
  renderMeasureDetails(data);
}
function renderMeasureSummary(data) {
  const box = document.getElementById("measureSummary");
  box.innerHTML = [summaryCard(data.left), summaryCard(data.right)].join("");
}
function summaryCard(series) {
  const s = series.summary;
  return `<div class="card">
    <h3>${escapeHtml(series.label)}</h3>
    ${metric("요청 수", fmt(s.requests))}
    ${metric("원본 입력", fmt(s.total_input_original))}
    ${metric("전송 입력", fmt(s.total_input_compressed))}
    ${metric("출력", fmt(s.total_output))}
    ${metric("전송 합계", fmt(s.total_tokens_sent))}
    ${metric("절감", s.saved_input_pct === null ? "N/A" : `${fmt(s.saved_input_tokens)} (${s.saved_input_pct}%)`)}
    ${metric("평균 지연", `${fmt(s.avg_latency_ms)} ms`)}
  </div>`;
}
function metric(label, value) {
  return `<div class="metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}
function renderMeasureChart(data) {
  const box = document.getElementById("measureChart");
  const leftOriginal = data.left.requests.map((r, i) => ({index: i + 1, value: r.tokens_input_original || 0}));
  const leftSent = data.left.requests.map((r, i) => ({index: i + 1, value: r.tokens_input_compressed || 0}));
  const rightOriginal = data.right.requests.map((r, i) => ({index: i + 1, value: r.tokens_input_original || 0}));
  const rightSent = data.right.requests.map((r, i) => ({index: i + 1, value: r.tokens_input_compressed || 0}));
  const values = leftOriginal.concat(leftSent, rightOriginal, rightSent).map(p => p.value);
  if (!values.length) {
    box.innerHTML = "(요청 데이터 없음)";
    return;
  }
  const maxY = Math.max(...values, 1);
  const maxX = Math.max(leftOriginal.length, rightOriginal.length, 1);
  const visibleWidth = Math.max(box.clientWidth - 24, 720);
  const width = Math.max(visibleWidth, maxX * 72 + 120);
  const height = Math.max(360, Math.round(visibleWidth * 0.46));
  const pad = {left: 58, right: 24, top: 24, bottom: 42};
  const x = i => pad.left + ((i - 1) / Math.max(maxX - 1, 1)) * (width - pad.left - pad.right);
  const y = v => height - pad.bottom - (v / maxY) * (height - pad.top - pad.bottom);
  const line = points => points.map(p => `${x(p.index)},${y(p.value)}`).join(" ");
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(r => Math.round(maxY * r));
  const grid = ticks.map(t => `<line x1="${pad.left}" y1="${y(t)}" x2="${width - pad.right}" y2="${y(t)}" stroke="#e1d8c3"/><text x="8" y="${y(t) + 4}" font-size="11" fill="#625a4d">${fmt(t)}</text>`).join("");
  const circles = (points, color, label, hollow=false) => points.map(p => `<circle cx="${x(p.index)}" cy="${y(p.value)}" r="4" fill="${hollow ? "#fffaf0" : color}" stroke="${color}" stroke-width="2"><title>#${p.index} ${label}: ${fmt(p.value)} 토큰</title></circle>`).join("");
  const polyline = (points, color, dashed=false) => points.length ? `<polyline points="${line(points)}" fill="none" stroke="${color}" stroke-width="3"${dashed ? " stroke-dasharray=\"6 5\"" : ""}/>` : "";
  const labels = Array.from({length: maxX}, (_, i) => i + 1)
    .filter(i => maxX <= 16 || i === 1 || i === maxX || i % Math.ceil(maxX / 12) === 0)
    .map(i => `<text x="${x(i)}" y="${height - 14}" font-size="11" text-anchor="middle" fill="#625a4d">${i}</text>`)
    .join("");
  box.innerHTML = `
    <div class="legend">
      <span style="--line:#1f6b39;--dash:dashed">${escapeHtml(data.left.label)} 원본 입력</span>
      <span style="--line:#1f6b39">${escapeHtml(data.left.label)} 압축 후 입력</span>
      <span style="--line:#a24b26;--dash:dashed">${escapeHtml(data.right.label)} 원본 입력</span>
      <span style="--line:#a24b26">${escapeHtml(data.right.label)} 압축 후 입력</span>
    </div>
    <svg width="${width}" height="${height}" role="img" aria-label="요청 순서별 입력 토큰 사용량">
      ${grid}
      <line x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}" stroke="#625a4d"/>
      <line x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}" stroke="#625a4d"/>
      ${labels}
      <text x="${width / 2}" y="${height - 2}" font-size="12" text-anchor="middle" fill="#625a4d">요청 순서</text>
      <text x="16" y="18" font-size="12" fill="#625a4d">토큰</text>
      ${polyline(leftOriginal, "#1f6b39", true)}${circles(leftOriginal, "#1f6b39", "원본 입력", true)}
      ${polyline(leftSent, "#1f6b39")}${circles(leftSent, "#1f6b39", "압축 후 입력")}
      ${polyline(rightOriginal, "#a24b26", true)}${circles(rightOriginal, "#a24b26", "원본 입력", true)}
      ${polyline(rightSent, "#a24b26")}${circles(rightSent, "#a24b26", "압축 후 입력")}
    </svg>`;
}
function renderMeasureDetails(data) {
  const rows = [];
  for (const side of [data.left, data.right]) {
    side.requests.forEach((r, i) => rows.push({label: side.label, index: i + 1, row: r}));
  }
  if (!rows.length) {
    document.getElementById("measureDetails").innerHTML = "";
    return;
  }
  const diagnosticHeaders = showMeasureDiagnostics
    ? `<th class="diagnostic">Guard</th><th class="diagnostic">Metadata</th><th class="diagnostic">Compression detail</th>`
    : "";
  document.getElementById("measureDetails").innerHTML = `<h3>요청 상세</h3>
    <table>
      <thead><tr>
        <th>Run</th><th>#</th><th>Time</th><th>Original</th><th>Sent</th><th>Output</th><th>Total</th><th>Saved</th><th>Latency</th>${diagnosticHeaders}
      </tr></thead>
      <tbody>${rows.map(detailRow).join("")}</tbody>
    </table>`;
}
function detailRow(item) {
  const r = item.row;
  const original = r.tokens_input_original || 0;
  const sent = r.tokens_input_compressed || 0;
  const saved = original - sent;
  const diagnosticCells = showMeasureDiagnostics ? `
    <td class="diagnostic">${escapeHtml(guardDetail(r.feature_flags || {}))}</td>
    <td class="diagnostic">${escapeHtml(metadataDetail(r.feature_flags || {}))}</td>
    <td class="diagnostic">${escapeHtml(compressDetail(r.feature_flags || {}))}</td>` : "";
  return `<tr>
    <td>${escapeHtml(item.label)}</td>
    <td>${item.index}</td>
    <td>${escapeHtml(formatTime(r.created_at))}</td>
    <td>${fmt(original)}</td>
    <td>${fmt(sent)}</td>
    <td>${fmt(r.tokens_output || 0)}</td>
    <td>${fmt(totalTokens(r))}</td>
    <td>${fmt(saved)}</td>
    <td>${fmt(r.latency_ms || 0)} ms</td>${diagnosticCells}
  </tr>`;
}
function totalTokens(row) {
  return (row.tokens_input_compressed || 0) + (row.tokens_output || 0);
}
function fmt(value) {
  return Number(value || 0).toLocaleString();
}
function formatTime(value) {
  if (!value) return "?";
  return String(value).replace("T", " ").slice(0, 19);
}
function compressDetail(flags) {
  if (!flags || !Object.keys(flags).length) return "-";
  if (flags.compress_enabled === false) return "disabled";
  const selection = selectionDetail(flags);
  const currentTurn = currentTurnDetail(flags);
  const toolFailures = toolResultFailureDetail(flags);
  const textFailures = textFailureDetail(flags);
  if (flags.compress_skip_reason) {
    return [
      `skip=${flags.compress_skip_reason}`,
      `elig=${flags.compress_eligible_messages || 0}`,
      `comp=${flags.compress_compressible_messages || 0}`,
      selection,
      currentTurn,
      toolFailures,
      textFailures,
    ].filter(Boolean).join(" ");
  }
  return [
    `cand=${flags.compress_candidates || 0}`,
    `msg=${flags.compress_candidate_messages || 0}`,
    `ct=${flags.compress_current_turn_contexts || 0}`,
    `ast=${flags.compress_ast_blocks || 0}`,
    `ref=${flags.compress_tool_result_refs || 0}`,
    selection,
    currentTurn,
    toolFailures,
    textFailures,
    `saved=${flags.compress_saved_tokens_est || 0}`,
  ].filter(Boolean).join(" ");
}
function selectionDetail(flags) {
  const parts = [];
  if ("compress_total_messages" in flags) parts.push(`msgs=${flags.compress_total_messages}`);
  if ("compress_last_user_idx" in flags) parts.push(`last_user=${flags.compress_last_user_idx}`);
  if ("compress_selected_messages" in flags) parts.push(`sel=${flags.compress_selected_messages}`);
  if (flags.compress_no_content_messages) parts.push(`no_content=${flags.compress_no_content_messages}`);
  if (flags.compress_system_excluded) parts.push(`sys_excl=${flags.compress_system_excluded}`);
  return parts.join(" ");
}
function currentTurnDetail(flags) {
  const numericKeys = [
    "compress_current_turn_tool_results",
    "compress_current_turn_allowed_tool_results",
    "compress_current_turn_rejected_tool_results",
    "compress_current_turn_compressible_tool_results",
    "compress_current_turn_candidates",
    "compress_current_turn_excluded",
    "compress_current_turn_raw_lines_max",
    "compress_current_turn_raw_chars_max",
  ];
  const hasSignal = numericKeys.some(key => Number(flags[key] || 0) > 0)
    || (flags.compress_current_turn_matched_tool_names || []).length
    || (flags.compress_current_turn_rejected_tool_names || []).length;
  if (!hasSignal) return "";
  const parts = [
    `ct_read=${flags.compress_current_turn_tool_results || 0}`,
    `ct_allowed=${flags.compress_current_turn_allowed_tool_results || 0}`,
    `ct_comp=${flags.compress_current_turn_compressible_tool_results || 0}`,
  ];
  if (flags.compress_current_turn_candidates) parts.push(`ct_cand=${flags.compress_current_turn_candidates}`);
  if (flags.compress_current_turn_rejected_tool_results) parts.push(`ct_rej=${flags.compress_current_turn_rejected_tool_results}`);
  if (flags.compress_current_turn_excluded) parts.push(`ct_excl=${flags.compress_current_turn_excluded}`);
  if (flags.compress_current_turn_raw_lines_max) parts.push(`ct_raw_lines=${flags.compress_current_turn_raw_lines_max}`);
  if (flags.compress_current_turn_raw_chars_max) parts.push(`ct_raw_chars=${flags.compress_current_turn_raw_chars_max}`);
  const matched = flags.compress_current_turn_matched_tool_names || [];
  const rejected = flags.compress_current_turn_rejected_tool_names || [];
  if (matched.length) parts.push(`ct_match=${matched.join(",")}`);
  if (rejected.length) parts.push(`ct_reject=${rejected.join(",")}`);
  return parts.join(" ");
}
function toolResultFailureDetail(flags) {
  const parts = [];
  if (flags.compress_tool_result_attempts) parts.push(`try=${flags.compress_tool_result_attempts}`);
  if (flags.compress_tool_result_ast_successes) parts.push(`ok=${flags.compress_tool_result_ast_successes}`);
  if (flags.compress_tool_result_failures) parts.push(`fail=${flags.compress_tool_result_failures}`);
  if (flags.compress_tool_result_last_fail_reason) parts.push(`reason=${flags.compress_tool_result_last_fail_reason}`);
  if (flags.compress_tool_result_raw_lines_max) parts.push(`raw_lines=${flags.compress_tool_result_raw_lines_max}`);
  if (flags.compress_tool_result_raw_chars_max) parts.push(`raw_chars=${flags.compress_tool_result_raw_chars_max}`);
  return parts.join(" ");
}
function textFailureDetail(flags) {
  const parts = [];
  if (flags.compress_text_attempts) parts.push(`text_try=${flags.compress_text_attempts}`);
  if (flags.compress_text_ast_successes) parts.push(`text_ok=${flags.compress_text_ast_successes}`);
  if (flags.compress_text_failures) parts.push(`text_fail=${flags.compress_text_failures}`);
  if (flags.compress_text_last_fail_reason) parts.push(`text_reason=${flags.compress_text_last_fail_reason}`);
  if (flags.compress_text_fence_count) parts.push(`text_fences=${flags.compress_text_fence_count}`);
  return parts.join(" ");
}
function guardDetail(flags) {
  if (!flags || !Object.keys(flags).length) return "-";
  if (!("current_turn_write_guard_blocked" in flags)) return "-";
  const mode = flags.current_turn_write_guard_mode || (flags.current_turn_write_guard_blocked ? "blocked" : "allowed");
  const parts = [
    `mode=${mode}`,
    flags.current_turn_write_guard_tool ? `tool=${flags.current_turn_write_guard_tool}` : "",
    flags.current_turn_write_guard_target_path ? `path=${flags.current_turn_write_guard_target_path}` : "",
    flags.current_turn_write_guard_block_reason ? `reason=${flags.current_turn_write_guard_block_reason}` : "",
    flags.current_turn_write_guard_required_contexts ? `need=${flags.current_turn_write_guard_required_contexts}` : "",
    flags.current_turn_write_guard_retrieved_contexts ? `got=${flags.current_turn_write_guard_retrieved_contexts}` : "",
  ].filter(Boolean);
  return parts.length ? parts.join(" ") : "-";
}
function metadataDetail(flags) {
  if (!flags || !Object.keys(flags).length) return "-";
  const parts = [];
  const paths = flags.compress_current_turn_source_paths || [];
  const symbols = flags.compress_context_symbol_names || [];
  const ranges = flags.compress_context_original_ranges || [];
  const langs = flags.compress_tool_result_detected_languages || [];
  if (paths.length) parts.push(`paths=${paths.slice(0, 3).join(",")}${paths.length > 3 ? ",..." : ""}`);
  if (symbols.length) parts.push(`symbols=${symbols.slice(0, 5).join(",")}${symbols.length > 5 ? ",..." : ""}`);
  if (ranges.length) parts.push(`lines=${ranges.slice(0, 3).join(",")}${ranges.length > 3 ? ",..." : ""}`);
  if (langs.length) parts.push(`lang=${langs.join(",")}`);
  if (flags.compress_context_metadata_count) parts.push(`meta=${flags.compress_context_metadata_count}`);
  return parts.length ? parts.join(" ") : "-";
}
async function loadCcimLog() {
  const text = await api("/api/ccim-log");
  document.getElementById("ccimLog").textContent = text || "(아직 로그가 없습니다)";
}
window.addEventListener("resize", () => {
  if (lastMeasureData) renderMeasureChart(lastMeasureData);
});
load().catch(err => alert(err.message));
</script>
</body>
</html>
"""
