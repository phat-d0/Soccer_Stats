"use strict";

// ---------- state ----------
const EDGE_STEPS = [0, 0.02, 0.03, 0.05, 0.08];
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
};
const state = {
  data: null,
  tab: "matches", // the app always opens on upcoming fixtures
  minEdge: store.get("minEdge", 0.03),
  exploreHome: store.get("exploreHome", null),
  exploreAway: store.get("exploreAway", null),
};

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pct = (x, d = 0) => (x == null ? "–" : `${(x * 100).toFixed(d)}%`);
const signedPct = (x, d = 1) => (x == null ? "–" : `${x >= 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(d)}%`);
const odds = (x) => (x == null ? "–" : x.toFixed(2));
const signed = (x, d = 1) => (x == null ? "–" : `${x >= 0 ? "+" : "−"}${Math.abs(x).toFixed(d)}`);

const MARKETS = [
  ["home", "Home win"],
  ["draw", "Draw"],
  ["away", "Away win"],
  ["over25", "Over 2.5"],
  ["under25", "Under 2.5"],
  ["btts", "Both score"],
];
const PICK_LABEL = { home: "Home", draw: "Draw", away: "Away", over25: "Over 2.5", under25: "Under 2.5", "over 2.5": "Over 2.5", "under 2.5": "Under 2.5" };

const CHECK = '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M3.5 8.5l3 3 6-7"/></svg>';

// ---------- model maths (mirrors models/dixon_coles.py) ----------
function poissonPmf(k, lam) {
  let p = Math.exp(-lam);
  for (let i = 1; i <= k; i++) p *= lam / i;
  return p;
}
function scoreMatrix(params, home, away, maxGoals = 10) {
  const a = params.attack, d = params.defence;
  const lam = Math.exp(params.intercept + params.home_adv + (a[home] ?? 0) + (d[away] ?? 0));
  const mu = Math.exp(params.intercept + (a[away] ?? 0) + (d[home] ?? 0));
  const m = [];
  let total = 0;
  for (let i = 0; i <= maxGoals; i++) {
    m.push([]);
    for (let j = 0; j <= maxGoals; j++) {
      let p = poissonPmf(i, lam) * poissonPmf(j, mu);
      if (i === 0 && j === 0) p *= 1 - lam * mu * params.rho;
      else if (i === 0 && j === 1) p *= 1 + lam * params.rho;
      else if (i === 1 && j === 0) p *= 1 + mu * params.rho;
      else if (i === 1 && j === 1) p *= 1 - params.rho;
      m[i].push(p);
      total += p;
    }
  }
  for (const row of m) for (let j = 0; j < row.length; j++) row[j] /= total;
  return { m, lam, mu };
}
function marketsFrom(m) {
  const p = { home: 0, draw: 0, away: 0, over25: 0, btts: 0 };
  const top = [];
  m.forEach((row, i) => row.forEach((v, j) => {
    if (i > j) p.home += v; else if (i === j) p.draw += v; else p.away += v;
    if (i + j > 2.5) p.over25 += v;
    if (i > 0 && j > 0) p.btts += v;
    top.push([`${i}-${j}`, v]);
  }));
  p.under25 = 1 - p.over25;
  top.sort((x, y) => y[1] - x[1]);
  return { p, top: top.slice(0, 5) };
}

// ---------- value picks ----------
function bestPick(fx, minEdge) {
  let best = null;
  for (const [k] of MARKETS) {
    const o = fx.odds?.[k];
    if (o == null) continue;
    const e = fx.p[k] * o - 1;
    if (e >= minEdge && e > 0 && (!best || e > best.edge)) best = { market: k, odds: o, edge: e };
  }
  return best;
}

// Which bookmaker the upcoming-match odds come from (DraftKings when configured).
const isDK = () => state.data?.odds_source?.name === "DraftKings";
const bookName = () => (isDK() ? "DraftKings" : "Bookmaker");

// Current prices: American odds for DraftKings (+650 / -250), decimal otherwise.
function price(d) {
  if (d == null) return "–";
  if (!isDK()) return d.toFixed(2);
  const a = d >= 2 ? Math.round((d - 1) * 100) : Math.round(-100 / (d - 1));
  return a > 0 ? `+${a}` : `−${Math.abs(a)}`;
}

function oddsAge() {
  const s = state.data?.odds_source;
  if (!s || !isDK() || !s.fetched_at) return "";
  const mins = Math.round((Date.now() - Date.parse(s.fetched_at)) / 60000);
  const ago = mins < 60 ? `${mins} min` : mins < 1440 ? `${Math.round(mins / 60)} h` : `${Math.round(mins / 1440)} days`;
  const every = s.refresh_hours ? ` Refreshing about every ${s.refresh_hours < 1.5 ? "hour" : `${Math.round(s.refresh_hours)} h`} to stay on the free plan.` : s.error && s.error.startsWith("paused") ? " Paused until the free allowance resets." : "";
  return `DraftKings odds updated ${ago} ago.${every}`;
}

// Bookmaker odds as probabilities (margin removed). Published data includes these;
// older data files fall back to simple proportional scaling here.
function impliedFor(fx) {
  if (fx.implied && fx.implied.home != null) return fx.implied;
  const o = fx.odds || {};
  const out = {};
  const scale = (keys) => {
    if (!keys.every((k) => o[k] > 1)) return;
    const raw = keys.map((k) => 1 / o[k]);
    const sum = raw.reduce((a, b) => a + b, 0);
    keys.forEach((k, i) => { out[k] = raw[i] / sum; });
  };
  scale(["home", "draw", "away"]);
  scale(["over25", "under25"]);
  return out;
}

function compareTable(fx, pick) {
  const imp = impliedFor(fx);
  const keys = ["home", "draw", "away"];
  const cell = (k, v, isModel) =>
    `<span class="${isModel && pick?.market === k ? "hi" : ""}">${pct(v)}</span>`;
  return `
    <div class="cmp num" role="table" aria-label="Win, draw and loss chances: model and bookmaker">
      <span></span><span class="h">Home</span><span class="h">Draw</span><span class="h">Away</span>
      <span class="lbl">Model</span>${keys.map((k) => cell(k, fx.p[k], true)).join("")}
      <span class="lbl">${bookName()}</span>${
        imp.home != null ? keys.map((k) => cell(k, imp[k], false)).join("") : '<span class="none">Odds not out yet</span>'
      }
    </div>`;
}

// ---------- rendering helpers ----------
function probBar(p, home, away) {
  return `
    <div class="probbar" role="img" aria-label="${esc(home)} ${pct(p.home)}, draw ${pct(p.draw)}, ${esc(away)} ${pct(p.away)}">
      <span style="width:${p.home * 100}%;background:var(--series-home)"></span>
      <span style="width:${p.draw * 100}%;background:var(--series-draw)"></span>
      <span style="width:${p.away * 100}%;background:var(--series-away)"></span>
    </div>
    <div class="problabels num">
      <span><i style="background:var(--series-home)"></i>Home ${pct(p.home)}</span>
      <span><i style="background:var(--series-draw)"></i>Draw ${pct(p.draw)}</span>
      <span><i style="background:var(--series-away)"></i>Away ${pct(p.away)}</span>
    </div>`;
}

function heatmap(matrix, home, away) {
  const n = Math.min(matrix.length, 6);
  const max = Math.max(...matrix.slice(0, n).flatMap((r) => r.slice(0, n)));
  let cells = '<div class="ax"></div>';
  for (let j = 0; j < n; j++) cells += `<div class="ax">${j}</div>`;
  for (let i = 0; i < n; i++) {
    cells += `<div class="ax">${i}</div>`;
    for (let j = 0; j < n; j++) {
      const v = matrix[i][j];
      const a = 0.06 + 0.94 * (v / max);
      const ink = a > 0.55 ? "#fff" : "var(--text-primary)";
      const label = v >= 0.01 ? `${Math.round(v * 100)}%` : "";
      cells += `<div style="background:rgba(var(--heat),${a.toFixed(3)});color:${ink}" title="${esc(home)} ${i}–${j} ${esc(away)}: ${(v * 100).toFixed(1)}%">${label}</div>`;
    }
  }
  return `
    <div class="heat-caption"><span>↓ ${esc(home)} goals</span><span>${esc(away)} goals →</span></div>
    <div class="heat" role="img" aria-label="Chance of each scoreline">${cells}</div>`;
}

// ---------- team news (injuries / suspensions from FPL) ----------
const chanceText = (a) => (a.status === "out" ? "out" : `${a.chance}%`);

function newsLine(fx) {
  if (!fx.news_applied || !fx.news) return "";
  const parts = [];
  for (const side of ["home", "away"]) {
    const n = fx.news[side];
    if (!n || !n.absences.length) continue;
    const who = n.absences.slice(0, 2).map((a) => `${esc(a.name)} ${chanceText(a)}`).join(", ");
    const more = n.absences.length > 2 ? ` +${n.absences.length - 2}` : "";
    parts.push(`<b>${esc(fx[side])}:</b> ${who}${more}`);
  }
  return parts.length ? `<div class="news-line"><span class="news-icon" aria-hidden="true">✚</span><span>${parts.join(" · ")}</span></div>` : "";
}

function newsSection(fx) {
  if (!fx.news) return "";
  const effect = (n) => {
    const bits = [];
    if (n.attack_mult < 0.995) bits.push(`attack ${signedPct(n.attack_mult - 1, 0)}`);
    if (n.defence_mult > 1.005) bits.push(`conceding ${signedPct(n.defence_mult - 1, 0)}`);
    return bits.length ? bits.join(", ") : "no change";
  };
  const team = (side) => {
    const n = fx.news[side];
    if (!n) return "";
    const rows = n.absences.length
      ? n.absences.map((a) => `
          <div class="absence">
            <div><b>${esc(a.name)}</b> <span class="muted">${esc(a.position)}</span><div class="muted small">${esc(a.news || "")}</div></div>
            <span class="pill ${a.status}">${a.status === "out" ? "Out" : `${a.chance}%`}</span>
          </div>`).join("")
      : '<p class="muted small" style="margin:4px 0">No regular players missing.</p>';
    return `<div class="news-team"><div class="news-head"><b>${esc(fx[side])}</b><span class="muted small">${fx.news_applied ? effect(n) : ""}</span></div>${rows}</div>`;
  };
  const base = fx.p_base
    ? `<p class="note">Without team news the model had ${esc(fx.home)} ${pct(fx.p_base.home)}, draw ${pct(fx.p_base.draw)}, ${esc(fx.away)} ${pct(fx.p_base.away)}.</p>`
    : fx.news_applied ? "" : '<p class="note">Team news is applied to each team\'s next match only.</p>';
  return `
    <div class="section-title">Team news</div>
    <div class="card">${team("home")}${team("away")}</div>
    ${base}`;
}

function detailHtml(fx) {
  const { home, away, kickoff, xg, p, odds: o, top, matrix, lowData } = fx;
  const imp = o ? impliedFor(fx) : {};
  const hasOdds = o && Object.values(o).some((v) => v != null);
  const rows = MARKETS.map(([k, label]) => {
    const name = k === "home" ? `${esc(home)} win` : k === "away" ? `${esc(away)} win` : label;
    const odds_ = o?.[k];
    const edge = odds_ != null ? p[k] * odds_ - 1 : null;
    return `<tr><td>${name}</td><td>${pct(p[k])}</td>${
      hasOdds
        ? `<td>${pct(imp[k])}</td><td>${price(odds_)}</td><td class="${edge > 0 ? "edge-pos" : ""}">${signedPct(edge)}</td>`
        : `<td>${odds(1 / p[k])}</td>`
    }</tr>`;
  }).join("");
  const when = kickoff ? new Date(kickoff).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "Any fixture, first team at home";
  return `
    <div class="detail">
      <p class="muted" style="margin:0;font-size:13px">${esc(when)}</p>
      <h2 id="sheet-title">${esc(home)} v ${esc(away)}</h2>
      <p class="muted" style="margin:0 0 10px">Expected goals <b class="num" style="color:var(--text-primary)">${xg[0].toFixed(2)} – ${xg[1].toFixed(2)}</b></p>
      ${probBar(p, home, away)}
      ${lowData ? '<p class="warn">⚠ One team has few matches in the data, so treat this one with extra caution.</p>' : ""}
      ${newsSection(fx)}
      <div class="section-title">Markets</div>
      <div class="card" style="padding:8px 14px">
        <table>
          <thead><tr><th></th><th>Model</th>${hasOdds ? `<th>${isDK() ? "DK %" : "Bookie"}</th><th>Odds</th><th>Edge</th>` : "<th>Fair odds</th>"}</tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <p class="note">${hasOdds ? `${isDK() ? "DK % = DraftKings' odds" : "Bookie = the odds"} as a chance, margin removed. Edge = model chance × payout − 1.` : "Fair odds = the price that would exactly match the model's chance."}</p>
      <div class="section-title">Scorelines</div>
      <div class="card">
        ${heatmap(matrix, home, away)}
        <p class="note" style="margin:10px 0 0">Most likely: ${top.map(([s, v]) => `${s} (${pct(v)})`).join(", ")}</p>
      </div>
    </div>`;
}

function openSheet(html) {
  $("#sheet-body").innerHTML = html;
  const sheet = $("#sheet");
  sheet.hidden = false;
  document.body.style.overflow = "hidden";
  $(".sheet-close", sheet).focus();
}
function closeSheet() {
  $("#sheet").hidden = true;
  document.body.style.overflow = "";
}

function edgeControl() {
  return `
    <div class="control-label"><span>Flag bets with edge of at least</span></div>
    <div class="segmented" role="group" aria-label="Minimum edge">
      ${EDGE_STEPS.map((e) => `<button data-edge="${e}" class="${e === state.minEdge ? "on" : ""}" aria-pressed="${e === state.minEdge}">${e === 0 ? "Any" : pct(e)}</button>`).join("")}
    </div>`;
}

// ---------- views ----------
function viewMatches() {
  const d = state.data;
  if (!d.fixtures.length) {
    return `<div class="empty">No upcoming fixtures found.<br>Try the Explore tab.</div>`;
  }
  const byDay = new Map();
  d.fixtures.forEach((fx, idx) => {
    const day = new Date(fx.kickoff).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
    if (!byDay.has(day)) byDay.set(day, []);
    byDay.get(day).push([fx, idx]);
  });
  const nValue = d.fixtures.filter((fx) => bestPick(fx, state.minEdge)).length;
  let html = `<p class="note">Chances of each result: the model vs ${bookName()}. ${oddsAge()} ${nValue ? `<b>${nValue}</b> of ${d.fixtures.length} matches have a value bet.` : "No value bets right now."}</p>`;
  for (const [day, items] of byDay) {
    html += `<div class="section-title">${esc(day)}</div>`;
    for (const [fx, idx] of items) {
      const pick = bestPick(fx, state.minEdge);
      const time = new Date(fx.kickoff).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
      html += `
        <button class="card match" data-fixture="${idx}">
          <div class="match-head"><span>${esc(time)}</span><span>Details ›</span></div>
          <div class="teams">
            <span>${esc(fx.home)}</span><span class="xg num">${fx.xg[0].toFixed(1)} xG</span>
            <span>${esc(fx.away)}</span><span class="xg num">${fx.xg[1].toFixed(1)} xG</span>
          </div>
          ${compareTable(fx, pick)}
          ${newsLine(fx)}
          ${pick ? `<span class="badge">${CHECK}Value: ${esc(PICK_LABEL[pick.market])} @ ${price(pick.odds)} <span class="num">(${signedPct(pick.edge)})</span></span>` : ""}
          ${fx.low_data ? '<div class="warn">⚠ Few matches for one team</div>' : ""}
        </button>`;
    }
  }
  html += `
    <div class="section-title">How to read this</div>
    <p class="note" style="margin-top:0">${bookName()} = ${isDK() ? "DraftKings'" : "the bookmaker's"} odds turned into chances, with the built-in margin taken out so the three add up to 100%. A value bet is where the model rates an outcome high enough that the odds pay more than it's worth.</p>
    ${edgeControl()}`;
  return html;
}

function absText(team) {
  const n = state.data.team_news?.[team];
  if (!n || !n.absences.length) return "";
  const out = n.absences.filter((a) => a.status === "out").length;
  const doubt = n.absences.length - out;
  const bits = [out && `${out} out`, doubt && `${doubt} doubtful`].filter(Boolean).join(", ");
  return `<small class="abs">✚ ${bits}: ${n.absences.slice(0, 3).map((a) => esc(a.name)).join(", ")}</small>`;
}

function viewRatings() {
  const r = state.data.ratings;
  const maxNet = Math.max(...r.map((t) => Math.abs(t.goal_diff)), 0.01);
  const hasXg = r.some((t) => t.xg_for != null);
  const rows = r.map((t, i) => {
    const w = (Math.abs(t.goal_diff) / maxNet) * 50;
    return `
      <div class="rating-row">
        <span class="rank">${i + 1}</span>
        <span>${esc(t.team)}${hasXg && t.xg_for != null ? `<small>xG ${t.xg_for.toFixed(2)} – ${t.xg_against.toFixed(2)} a game</small>` : ""}${absText(t.team)}</span>
        <span class="r">${t.goals_for.toFixed(2)}</span>
        <span class="r">${t.goals_against.toFixed(2)}</span>
        <span class="netbar" title="Net ${signed(t.goal_diff, 2)}"><span class="axis"></span><span class="fill ${t.goal_diff >= 0 ? "pos" : "neg"}" style="width:${w}%"></span></span>
      </div>`;
  }).join("");
  return `
    <p class="note">Goals each team would score and concede per game against an average Premier League side on a neutral pitch. Recent matches count more.${hasXg ? " The xG line is this season's raw average." : ""}</p>
    <div class="card" style="margin-top:12px">
      <div class="rating-row head"><span></span><span>Team</span><span class="r">For</span><span class="r">Agst</span><span class="r">Net</span></div>
      ${rows}
    </div>`;
}

function summarize(bets, minEdge) {
  const sel = bets.filter((b) => b[5] >= minEdge && b[5] > 0);
  const n = sel.length;
  const profit = sel.reduce((s, b) => s + b[7], 0);
  const clvs = sel.filter((b) => b[6] != null);
  return {
    sel,
    n,
    profit,
    roi: n ? profit / n : null,
    clv: clvs.length ? clvs.reduce((s, b) => s + b[6], 0) / clvs.length : null,
    beat: clvs.length ? clvs.filter((b) => b[6] > 0).length / clvs.length : null,
  };
}

function profitChart(sel) {
  if (!sel.length) return "";
  const daily = [];
  let cum = 0;
  for (const b of sel) {
    cum += b[7];
    const last = daily[daily.length - 1];
    if (last && last.date === b[0]) { last.cum = cum; last.n += 1; last.day += b[7]; }
    else daily.push({ date: b[0], cum, n: 1, day: b[7] });
  }
  // Draw at the real pixel width so text and strokes aren't stretched.
  const W = Math.max(280, Math.round(($("#view").clientWidth || 360) - 60)), H = 200, L = 34, R = 8, T = 10, B = 22;
  const t0 = Date.parse(daily[0].date), t1 = Date.parse(daily[daily.length - 1].date) || t0 + 1;
  const ys = daily.map((p) => p.cum).concat(0);
  let lo = Math.min(...ys), hi = Math.max(...ys);
  const pad = (hi - lo) * 0.1 || 1; lo -= pad; hi += pad;
  const x = (t) => L + ((t - t0) / (t1 - t0 || 1)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  // step line: hold each day's total until the next betting day
  let path = `M${x(t0).toFixed(1)},${y(0).toFixed(1)}`;
  for (const p of daily) {
    const px = x(Date.parse(p.date)).toFixed(1);
    path += ` H${px} V${y(p.cum).toFixed(1)}`;
  }
  const step = niceStep((hi - lo) / 4);
  let grid = "";
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    grid += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 6}" y="${y(v) + 3}" text-anchor="end">${signed(v, 0).replace("+0", "0").replace("−0", "0")}</text>`;
  }
  const months = [];
  const dt = new Date(t0); dt.setDate(1);
  while (dt.getTime() <= t1) {
    if (dt.getTime() >= t0) months.push(dt.getTime());
    dt.setMonth(dt.getMonth() + 3);
  }
  const xl = months.map((t) => `<text class="xlab" x="${x(t)}" y="${H - 6}" text-anchor="middle">${new Date(t).toLocaleDateString(undefined, { month: "short", year: "2-digit" })}</text>`).join("");
  chartData = { daily, x, y, W };
  return `
    <div class="chart" id="profit-chart">
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Running profit in units, ending at ${signed(cum)}">
        <g class="grid">${grid}</g>
        <line class="zero" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}"/>
        <path class="line" d="${path}" vector-effect="non-scaling-stroke"/>
        ${xl}
        <line class="cross" id="cross" y1="${T}" y2="${H - B}" visibility="hidden" vector-effect="non-scaling-stroke"/>
      </svg>
      <div class="tooltip" id="tip" hidden></div>
    </div>`;
}
let chartData = null;
function niceStep(raw) {
  const p = 10 ** Math.floor(Math.log10(raw || 1));
  const f = raw / p;
  return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p;
}
function bindChart() {
  const el = $("#profit-chart");
  if (!el || !chartData) return;
  const svg = $("svg", el), tip = $("#tip", el), cross = $("#cross", el);
  const move = (ev) => {
    const rect = svg.getBoundingClientRect();
    const sx = ((ev.clientX - rect.left) / rect.width) * chartData.W;
    let best = chartData.daily[0], bd = Infinity;
    for (const p of chartData.daily) {
      const dd = Math.abs(chartData.x(Date.parse(p.date)) - sx);
      if (dd < bd) { bd = dd; best = p; }
    }
    const px = chartData.x(Date.parse(best.date));
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.innerHTML = `<b>${new Date(best.date).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}</b><br>${best.n} bet${best.n > 1 ? "s" : ""}, ${signed(best.day, 2)} units<br>Running total ${signed(best.cum)} units`;
    const left = Math.min(Math.max((px / chartData.W) * rect.width, 70), rect.width - 70);
    tip.style.left = `${left}px`;
  };
  const leave = () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); };
  svg.addEventListener("pointermove", move);
  svg.addEventListener("pointerdown", move);
  svg.addEventListener("pointerleave", leave);
}

function viewRecord() {
  const d = state.data;
  const rec = d.record.model;
  if (!rec || !rec.matches) return '<div class="empty">Not enough data to replay yet.</div>';
  const s = summarize(rec.bets, state.minEdge);
  const gap = rec.log_loss - rec.market_log_loss;
  const since = new Date(d.record_start).toLocaleDateString(undefined, { month: "long", year: "numeric" });

  let compare = "";
  if (d.record.goals_only) {
    const g = d.record.goals_only;
    const sg = summarize(g.bets, state.minEdge);
    const row = (name, r, sm) => `<tr><td>${name}</td><td>${r.log_loss.toFixed(4)}</td><td>${signed(r.log_loss - r.market_log_loss, 4)}</td><td>${sm.n}</td><td>${signedPct(sm.roi)}</td></tr>`;
    compare = `
      <div class="section-title">Goals only vs goals + xG</div>
      <div class="card" style="padding:8px 14px">
        <table>
          <thead><tr><th></th><th>Log loss</th><th>vs bookie</th><th>Bets</th><th>ROI</th></tr></thead>
          <tbody>${row("Goals + xG", rec, s)}${row("Goals only", g, sg)}</tbody>
        </table>
      </div>
      <p class="note">Log loss measures prediction error: lower is better. The app uses goals + xG.</p>`;
  }

  const recent = s.sel.slice(-25).reverse().map((b) => `
    <div class="bet-row">
      <span>${esc(b[1])} v ${esc(b[2])}<div class="meta">${new Date(b[0]).toLocaleDateString(undefined, { day: "numeric", month: "short" })} · ${esc(PICK_LABEL[b[3]] || b[3])} @ ${odds(b[4])} · edge ${signedPct(b[5])}</div></span>
      <span class="pl ${b[7] > 0 ? "win" : ""}">${b[7] > 0 ? "Won " : "Lost "}${signed(b[7], 2)}</span>
    </div>`).join("");

  return `
    <p class="note">The model replayed week by week since ${esc(since)}, using only data it would have had before each match. 1-unit bets at the historical opening odds in football-data's files (mostly Pinnacle)${isDK() ? "; past DraftKings prices aren't available, so DraftKings' bigger margin would make real results somewhat worse" : ""}.</p>
    ${edgeControl()}
    <div class="tiles" style="margin-top:12px">
      <div class="tile"><div class="label">Profit</div><div class="value">${signed(s.profit)}</div><div class="sub">units from ${s.n} bets</div></div>
      <div class="tile"><div class="label">Return per bet</div><div class="value">${signedPct(s.roi)}</div><div class="sub">ROI</div></div>
      <div class="tile"><div class="label">Beat closing odds</div><div class="value">${pct(s.beat)}</div><div class="sub">avg ${signedPct(s.clv)} vs close</div></div>
      <div class="tile"><div class="label">Accuracy vs bookie</div><div class="value">${gap <= 0 ? "Better" : "Worse"}</div><div class="sub">log loss ${signed(gap, 4)}</div></div>
    </div>
    <p class="note">Beating the closing odds consistently is the best sign of a real edge; profit alone can be luck.</p>
    <div class="section-title">Running profit (units)</div>
    <div class="card">${profitChart(s.sel) || '<p class="muted">No bets at this threshold.</p>'}</div>
    ${compare}
    ${recent ? `<div class="section-title">Latest bets</div><div class="card">${recent}</div>` : ""}`;
}

function viewExplore() {
  const d = state.data;
  const teams = d.teams;
  let home = teams.includes(state.exploreHome) ? state.exploreHome : teams[0];
  let away = teams.includes(state.exploreAway) && state.exploreAway !== home ? state.exploreAway : teams.find((t) => t !== home);
  const opt = (sel) => teams.map((t) => `<option ${t === sel ? "selected" : ""}>${esc(t)}</option>`).join("");
  const { m, lam, mu } = scoreMatrix(d.params, home, away);
  const { p, top } = marketsFrom(m);
  return `
    <p class="note">Pick any two teams to see the model's prediction.</p>
    <div class="pickers" style="margin:12px 0">
      <select id="ex-home" aria-label="Home team">${opt(home)}</select>
      <span class="vs">v</span>
      <select id="ex-away" aria-label="Away team">${opt(away)}</select>
    </div>
    ${detailHtml({ home, away, xg: [lam, mu], p, top, matrix: m })}`;
}

const VIEWS = { matches: viewMatches, ratings: viewRatings, record: viewRecord, explore: viewExplore };

function render() {
  chartData = null;
  $("#view").innerHTML = VIEWS[state.tab]();
  document.querySelectorAll(".tabbar button").forEach((b) => {
    const on = b.dataset.tab === state.tab;
    b.classList.toggle("active", on);
    b.setAttribute("aria-current", on ? "page" : "false");
  });
  bindChart();
}

function setTab(tab) {
  state.tab = tab;
  closeSheet();
  render();
  window.scrollTo(0, 0);
}

function setUpdated() {
  const d = state.data;
  const mins = Math.round((Date.now() - Date.parse(d.generated_at)) / 60000);
  const ago = mins < 60 ? `${mins} min ago` : mins < 1440 ? `${Math.round(mins / 60)} h ago` : `${Math.round(mins / 1440)} days ago`;
  $("#updated").textContent = `${d.league} ${d.season} · updated ${ago}`;
  const old = document.getElementById("xg-banner");
  if (old) old.remove();
  if (d.xg_error) {
    $("#view").insertAdjacentHTML("beforebegin", '<div id="xg-banner" class="banner" style="margin:0 16px">xG was unavailable at the last update, so ratings use goals only.</div>');
  }
}

// ---------- events ----------
document.addEventListener("click", (ev) => {
  const t = ev.target.closest("button, [data-close]");
  if (!t) return;
  if (t.dataset.tab) {
    setTab(t.dataset.tab);
  } else if (t.dataset.edge !== undefined) {
    state.minEdge = Number(t.dataset.edge); store.set("minEdge", state.minEdge);
    render();
  } else if (t.dataset.fixture !== undefined) {
    const fx = state.data.fixtures[Number(t.dataset.fixture)];
    openSheet(detailHtml({ ...fx, top: fx.top_scores, lowData: fx.low_data }));
  } else if (t.hasAttribute("data-close")) {
    closeSheet();
  } else if (t.id === "refresh") {
    load(true);
  }
});
document.addEventListener("change", (ev) => {
  if (ev.target.id === "ex-home") { state.exploreHome = ev.target.value; store.set("exploreHome", state.exploreHome); render(); }
  if (ev.target.id === "ex-away") { state.exploreAway = ev.target.value; store.set("exploreAway", state.exploreAway); render(); }
});
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeSheet(); });

// ---------- load ----------
async function load(force = false) {
  const btn = $("#refresh");
  btn.classList.add("spin");
  try {
    const res = await fetch("data.json", { cache: force ? "reload" : "no-cache" });
    if (!res.ok) throw new Error(res.statusText);
    state.data = await res.json();
    setUpdated();
    render();
  } catch (err) {
    if (!state.data) $("#view").innerHTML = `<div class="empty">Couldn't load predictions.<br><span class="muted">${esc(err.message || err)}</span></div>`;
  } finally {
    btn.classList.remove("spin");
  }
}

// The Ask (Claude chat) tab was removed: clear the API key and chat it kept on this device.
for (const k of ["anthropicKey", "chatConv", "chatModel"]) {
  try { localStorage.removeItem(k); } catch { /* storage blocked */ }
}

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
}
load();
