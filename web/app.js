"use strict";

// ---------- state ----------
// The minimum edge comes from history (each portfolio's backtest → edge_threshold); these
// steps are only for "Explore other edges". PAPER_EDGE must match trades.py.
const EDGE_STEPS = [0.02, 0.05, 0.08, 0.12];
const PAPER_EDGE = 0.12;
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
};
const state = {
  data: null,
  tab: "matches", // the app always opens on upcoming fixtures
  league: store.get("league", ""), // competition filter (football-data code), "" = all
  exploreEdge: null, // a step picked under "Explore other edges"; null = the recommended edge
  pfView: "live", // Portfolio: "live" paper trades or "backtest"
  pfMarket: "",
  pfSeason: "",
  pfShown: 15,
  pfId: store.get("pfId", "moneyline"), // Portfolio tab: which strategy's portfolio
  recBet: "match", // Record tab: "match" (model replay) or "player" (FanDuel player shots)
  recStrat: "", // Record tab, player shots: which backtest strategy's sweep to show
  recDk: "", // Record tab, match bets: which DraftKings backtest strategy ("raw" or "blend")
  teamsView: "teams", // Teams tab: "teams" or "players"
  pl: { q: "", team: "", pos: "", sort: "exp", shown: 40, mode: "stats", season: "", ssort: "shots", active: true, view: "list", metric: "p90" }, // Players view
  pb: null, // players_backtest.json once loaded (or { error })
  psBy: {}, // season stats per league once loaded: E0 players_stats.json, others players_stats_<code>.json (or { error })
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

// ---------- value picks (the trade rule in trades.py: best_pick) ----------
const TRADE_MARKETS = ["home", "draw", "away", "over25", "under25"];
// The live paper rule (trades.paper_threshold, published as portfolio.rule and
// portfolio.rules[code]): p_source "model" = the owner's fixed live test (rule fixed_raw),
// a fixed threshold on the model's own chance in every league; otherwise the learned
// minimum on the blend. The app flags exactly what paper trades take.
const paperRule = () => state.data?.portfolio?.rule || null;
const fixedRule = () => paperRule()?.p_source === "model";
const leagueRule = (lg) => state.data?.portfolio?.rules?.[lg || "E0"] || paperRule();
// The chance picks use: the raw model under the fixed rule, else p_bet (the blend) when set.
const pickProbs = (fx) => (fixedRule() ? fx.p : fx.p_bet || fx.p);
// Paper trades skip a match where one team has few matches in the model (paper.py,
// low_data); under the live test the app skips it too, so it flags exactly those trades.
const fixedSkips = (fx) => fixedRule() && !!fx.low_data;
function bestPick(fx, minEdge) {
  if (minEdge == null) return null; // history found no edge level that beats the market
  if (fixedSkips(fx)) return null;
  let best = null;
  for (const k of TRADE_MARKETS) {
    const o = fx.odds?.[k], p = pickProbs(fx)?.[k]; // edge against the quoted price
    if (o == null || p == null || o <= 1) continue;
    const e = p * o - 1;
    if (e > 0 && e >= minEdge - 1e-9 && (!best || e > best.edge)) best = { market: k, odds: o, edge: e };
  }
  return best;
}

// Player shot picks (trades.player_picks): best line and side per player and market,
// at most MAX_PLAYER_TRADES per match, highest edges first.
const MAX_PLAYER_TRADES = 4;
const PLAYER_MARKET = { player_shots: "shots", player_shots_on_target: "shots on target" };
function playerPicks(fx, minEdge) {
  if (minEdge == null) return [];
  const best = new Map();
  for (const pl of fx.players || []) {
    for (const ln of pl.lines || []) {
      if (ln.p == null) continue; // no blended chance: never a pick (trades.player_picks)
      const e = ln.p * ln.odds - 1;
      if (!(e > 0 && e >= minEdge - 1e-9)) continue;
      const key = `${pl.player_id}|${ln.market}`;
      if (!best.has(key) || e > best.get(key).edge) best.set(key, { ...ln, edge: e, player: pl.player, team: pl.team });
    }
  }
  return [...best.values()].sort((a, b) => b.edge - a.edge).slice(0, MAX_PLAYER_TRADES);
}
// FanDuel writes "1+ shots" as a whole-number line (1 = one or more); half lines stay "Over 1.5".
const lineText = (side, line) =>
  Number.isInteger(Number(line)) && side === "over" ? `${line}+` : `${side === "over" ? "Over" : "Under"} ${line}`;
const lineLabel = (x) => `${lineText(x.side, x.line)} ${PLAYER_MARKET[x.market] || x.market}`;
// Team corners (portfolio "corners", bet_type "corners"): "Valencia over 4.5 corners".
const isCorner = (t) => t.bet_type === "corners";
const cornerLabel = (t) => `${t.team || (t.team_side === "away" ? t.away : t.home)} ${t.side} ${Number(t.line)} corners`;
function tradeLabel(t) {
  if (isCorner(t)) return cornerLabel(t);
  return t.bet_type === "player" ? `${t.player} ${lineLabel(t).toLowerCase()}` : PICK_LABEL[t.market] || t.market;
}
// A trade row's first line: the bet for player and corner trades, the match otherwise (HTML).
const tradeTitle = (t) => (isCorner(t) ? esc(cornerLabel(t)) : t.bet_type === "player" ? `${esc(t.player)} · ${esc(lineLabel(t))}` : `${esc(t.home)} v ${esc(t.away)}`);
// Closing line value: against Pinnacle's close for corners, DraftKings' for match bets.
const tradeClv = (t) => (isCorner(t) ? t.clv_pinnacle : t.clv_dk);
const tradeBeat = (t) => (isCorner(t) ? t.beat_close_pinnacle : t.beat_close_dk);

// Which bookmaker the upcoming-match odds come from (DraftKings when configured).
const isDK = () => state.data?.odds_source?.name === "DraftKings";
const bookName = () => (isDK() ? "DraftKings" : "Bookmaker");
const bookPoss = () => (isDK() ? "DraftKings'" : "the bookmaker's");

// Current prices: American odds for DraftKings (+650 / -250), decimal otherwise.
function american(d) {
  if (d == null) return "–";
  const a = d >= 2 ? Math.round((d - 1) * 100) : Math.round(-100 / (d - 1));
  return a > 0 ? `+${a}` : `−${Math.abs(a)}`;
}
function price(d) {
  if (d == null) return "–";
  return isDK() ? american(d) : d.toFixed(2);
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
  return `<div class="card">${team("home")}${team("away")}</div>${base}`;
}

// ESPN's card-level team news (publish → espn_news; all leagues, within 36 hours of kickoff).
// Not the top-level `team_news`, which is FPL's dict. Injuries are empty today (ESPN's soccer
// feed has none), so the block only shows when there are some, minus players FPL already lists.
const plainName = (x) => String(x || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().trim();
const sameName = (a, b) => { const x = plainName(a), y = plainName(b); return x === y || x.endsWith(` ${y}`) || y.endsWith(` ${x}`); };
const espnTime = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" }) : null);
const lineupConfirmed = (fx) => fx.team_news?.lineup?.confirmed === true && fx.team_news.lineup.home?.starters?.length > 0;

function espnInjuries(fx) {
  const inj = fx.team_news?.injuries || {};
  const fplNames = (side) => (fx.news?.[side]?.absences || []).map((a) => a.name);
  const sides = ["home", "away"].map((side) => [side, (inj[side] || []).filter((x) => x?.name && !fplNames(side).some((n) => sameName(n, x.name)))]);
  if (!sides.some(([, list]) => list.length)) return "";
  const team = ([side, list]) => (list.length ? `<div class="news-team"><div class="news-head"><b>${esc(fx[side])}</b></div>${list.map((x) => `
    <div class="absence"><div><b>${esc(x.name)}</b>${x.detail ? `<div class="muted small">${esc(x.detail)}</div>` : ""}</div>${x.status ? `<span class="pill doubtful">${esc(x.status)}</span>` : ""}</div>`).join("")}</div>` : "");
  return `<div class="card">${sides.map(team).join("")}</div><p class="note">Injuries and doubts from ESPN${fx.news ? ", where FPL doesn't already list them" : ""}.</p>`;
}

function lineupBlock(fx) {
  const tn = fx.team_news;
  const lu = tn?.lineup;
  const checked = espnTime(tn?.fetched_at || lu?.fetched_at);
  if (!lineupConfirmed(fx)) {
    return `<div class="sub-title">Lineups</div><p class="note" style="margin-top:2px">Not out yet: lineups usually come about an hour before kickoff.${checked ? ` Last checked on ${esc(tn.source || "ESPN")} ${esc(checked)}.` : ""}</p>`;
  }
  const xi = (side) => {
    const t = lu[side] || {};
    return `<div class="xi"><b>${esc(fx[side])}</b><ol>${(t.starters || []).map((n) => `<li>${esc(n)}</li>`).join("")}</ol></div>`;
  };
  const subs = ["home", "away"].filter((side) => lu[side]?.subs?.length);
  const since = espnTime(lu.first_confirmed_at);
  return `
    <div class="sub-title">Confirmed XI</div>
    <div class="card xi-grid">${xi("home")}${xi("away")}</div>
    ${subs.length ? `<details class="fold xi-subs"><summary>Substitutes</summary><div class="card">${subs.map((side) => `<div class="xi-bench"><b>${esc(fx[side])}</b> <span class="muted">${lu[side].subs.map(esc).join(", ")}</span></div>`).join("")}</div></details>` : ""}
    <p class="note">Source: ${esc(tn.source || "ESPN")}${since ? `, lineups in since ${esc(since)}` : ""}${checked ? `, checked ${esc(checked)}` : ""}.</p>`;
}

// Match sheet "Team news": FPL injuries (Premier League, applied to the model), any ESPN
// injuries FPL doesn't list, then lineups (every league).
function teamNewsSection(fx) {
  if (!fx.kickoff && !fx.news) return ""; // Explore: any two teams, no match
  return `
    <div class="section-title">Team news</div>
    ${fx.news ? newsSection(fx) : ""}
    ${espnInjuries(fx)}
    ${fx.kickoff ? lineupBlock(fx) : ""}`;
}

// How much weight the live match blend gives the model (match_calibration: c in
// score = a + b·log(price) + c·log(model)); null without a live fit.
function blendModelWeight() {
  const mb = state.data?.match_blend;
  return mb?.live && mb.h2h?.coef ? mb.h2h.coef[mb.h2h.coef.length - 1] : null;
}

// Why value picks are rare once the blend is live, in plain words (match sheet and Matches).
function blendWhyText(margin) {
  const c = blendModelWeight();
  if (c == null) return "";
  const n = state.data.match_blend.h2h.matches;
  const weight = Math.abs(c) < 0.1 ? "almost no weight" : c > 0 ? `a weight of ${c.toFixed(2)}` : "a slightly negative weight";
  return `Tested on ${n ? `${n.toLocaleString()} ` : ""}past matches, the price already held what the model knows: the best mix gives the model ${weight}, so the blend sits right next to ${bookPoss()} chance. A bet only shows when the blend beats the price by more than ${bookPoss()} margin${margin ? ` (${pct(margin, 1)} here)` : ""}, which is rare. Few or no picks is the honest answer, not a fault.`;
}

function detailHtml(fx) {
  const { home, away, kickoff, xg, p, odds: o, top, matrix, lowData } = fx;
  const imp = o ? impliedFor(fx) : {};
  const hasOdds = o && Object.values(o).some((v) => v != null);
  const blend = hasOdds && fx.p_bet ? fx.p_bet : null;
  const rows = MARKETS.map(([k, label]) => {
    const name = k === "home" ? `${esc(home)} win` : k === "away" ? `${esc(away)} win` : label;
    const odds_ = o?.[k];
    const pb = pickProbs(fx)[k]; // the chance bestPick uses
    const edge = odds_ != null && pb != null ? pb * odds_ - 1 : null;
    return `<tr><td>${name}</td><td>${pct(p[k])}</td>${
      hasOdds
        ? `${blend ? `<td class="${fixedRule() ? "" : "blend-col"}">${pct(blend[k])}</td>` : ""}<td>${pct(imp[k])}</td><td>${price(odds_)}</td><td class="${edge > 0 ? "edge-pos" : ""}">${signedPct(edge)}</td>`
        : `<td>${odds(1 / p[k])}</td>`
    }</tr>`;
  }).join("");
  const when = kickoff ? new Date(kickoff).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "Any fixture, first team at home";
  const dk = isDK() ? "DK" : "Book";
  const marketNote = !hasOdds
    ? "Fair odds = the price that would exactly match the model's chance."
    : fixedRule()
      ? `Model = the model alone: the chance your live test trades on. ${blend ? `Blend = the model mixed with ${bookPoss()} price, shown for comparison. ` : ""}${dk} = ${bookPoss()} odds as a chance, margin removed. Edge = model chance × payout − 1, against the quoted odds.`
      : blend
      ? `Model = the model alone. Blend = the model mixed with ${bookPoss()} price, the chance value picks use. ${dk} = ${bookPoss()} odds as a chance, margin removed. Edge = blend × payout − 1.`
      : `${dk} = ${bookPoss()} odds as a chance, margin removed. Edge = model chance × payout − 1.`;
  const pick = hasOdds ? bestPick(fx, fxEdge(fx)) : null;
  return `
    <div class="detail">
      <p class="muted" style="margin:0;font-size:13px">${esc(when)}</p>
      <h2 id="sheet-title">${esc(home)} v ${esc(away)}</h2>
      <p class="muted" style="margin:0 0 10px">Expected goals <b class="num" style="color:var(--text-primary)">${xg[0].toFixed(2)} – ${xg[1].toFixed(2)}</b></p>
      ${probBar(p, home, away)}
      ${lowData ? '<p class="warn">⚠ One team has few matches in the data, so treat this one with extra caution.</p>' : ""}
      ${teamNewsSection(fx)}
      <div class="section-title">Markets</div>
      <div class="card" style="padding:8px 14px">
        <table class="mkts${blend ? " with-blend" : ""}">
          <thead><tr><th></th><th>Model</th>${hasOdds ? `${blend ? `<th class="${fixedRule() ? "" : "blend-col"}">Blend</th>` : ""}<th>${dk}</th><th>Odds</th><th>Edge</th>` : "<th>Fair odds</th>"}</tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <p class="note">${marketNote}</p>
      ${hasOdds && fixedRule() ? `<div class="explain">${fixedPickText(fx, pick)} ${esc(fixedNote(fxLeague(fx)))}</div>` : hasOdds && fxEdge(fx) == null ? `<div class="explain"><b>Why no value bet here?</b> ${noEdgeText("moneyline", fxLeague(fx))}</div>` : blend && !pick ? `<div class="explain"><b>Why no value bet here?</b> ${blendWhyText(imp.margin)}</div>` : ""}
      ${goalsSection(fx)}
      ${cornersSection(fx)}
      ${playersSection(fx)}
      <div class="section-title">Scorelines</div>
      <div class="card">
        ${heatmap(matrix, home, away)}
        <p class="note" style="margin:10px 0 0">Most likely: ${top.map(([s, v]) => `${s} (${pct(v)})`).join(", ")}</p>
      </div>
    </div>`;
}

// Over/under goals from the model's score matrix: P(goals <= k) for k = 0..6. Cards carry
// `total_goals_cdf` and `goals_cdf` (publish); Explore's full 0..10 matrix is summed here.
// The shown card matrix stops at 5 a side, so it is only used when it reaches 7 goals.
function goalCdfs(fx) {
  const m = fx.matrix;
  const fromMatrix = (f) => {
    if (!m || m.length < 7) return null;
    const by = [];
    m.forEach((row, i) => row.forEach((v, j) => { const k = f(i, j); by[k] = (by[k] || 0) + v; }));
    let c = 0;
    return Array.from({ length: 7 }, (_, k) => (c += by[k] || 0));
  };
  return {
    total: fx.total_goals_cdf || fromMatrix((i, j) => i + j),
    home: fx.goals_cdf?.home || fromMatrix((i) => i),
    away: fx.goals_cdf?.away || fromMatrix((i, j) => j),
  };
}
const overFrom = (cdf, line) => (cdf && cdf[Math.floor(line)] != null ? 1 - cdf[Math.floor(line)] : null);

function goalsSection(fx) {
  const cdf = goalCdfs(fx);
  if (!cdf.total && !cdf.home && !cdf.away) return "";
  const lg = fxLeague(fx);
  const e1 = lg === "E1";
  const totalRows = [0.5, 1.5, 2.5, 3.5, 4.5].map((line) => {
    const over = overFrom(cdf.total, line);
    return `<tr><td>${line}</td><td>${pct(over)}</td><td>${pct(over == null ? null : 1 - over)}</td></tr>`;
  }).join("");
  const tt = fx.team_totals;
  const fd = tt && (tt.home?.length || tt.away?.length);
  const team = (side) => {
    const priced = Object.fromEntries((tt?.[side] || []).map((r) => [r.line, r]));
    const rows = [0.5, 1.5, 2.5].map((line) => {
      const over = overFrom(cdf[side], line);
      const q = priced[line];
      return `<tr><td>${line}</td><td>${pct(over)}</td><td>${pct(over == null ? null : 1 - over)}</td>${fd ? `<td>${q ? pct(q.fair_over) : "–"}</td>` : ""}</tr>`;
    }).join("");
    return `<div class="goals-team"><b>${esc(fx[side])}</b>
      <table class="mkts goals"><thead><tr><th>Over/under</th><th>Over</th><th>Under</th>${fd ? "<th>FD over</th>" : ""}</tr></thead><tbody>${rows}</tbody></table></div>`;
  };
  const when = fd && tt.fetched_at ? new Date(tt.fetched_at).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" }) : null;
  const e1Note = e1 ? " These are less reliable in the Championship: it runs on goals only, and in testing its goal chances were too confident." : "";
  return `
    <div class="section-title">Goals</div>
    <div class="card" style="padding:8px 14px">
      <table class="mkts goals">
        <thead><tr><th>Total goals</th><th>Over</th><th>Under</th></tr></thead>
        <tbody>${totalRows}</tbody>
      </table>
    </div>
    <p class="note">Model's chances of more or fewer goals in the match than each line (over 2.5 = 3 or more). Tested against Pinnacle; the model doesn't beat the market.${e1Note}</p>
    ${cdf.home && cdf.away ? `
    <details class="fold goals-fold">
      <summary>Each team's goals <span class="muted">(${fd ? "with FanDuel's prices" : "model only"})</span></summary>
      <div class="card" style="padding:8px 14px">${team("home")}${team("away")}</div>
      <p class="note">Model's chances. We're testing these against FanDuel's prices; no edge proven yet, so nothing is flagged as a bet.${fd ? ` FD over = FanDuel's chance of the over, margin removed${when ? `, as of ${esc(when)}` : ""}; "–" where FanDuel has no line.` : " No FanDuel price for this match yet: they're logged about a day before kickoff and again just before it."}${e1Note}</p>
    </details>` : ""}`;
}

// Match sheet "Corners": each team's corner lines, model (f) beside Pinnacle (owner's live
// test, portfolio "corners"). corners = {model, home: {mean, cdf}, away, pinnacle?}; each
// Pinnacle line has p_over/p_under/p_push from the model and `pick` at the 12% rule.
const WEAK_CORNER_FIT = new Set(["SP1", "D1", "E1"]);
function cornerTrade(fx, side, line) {
  return (pfById("corners")?.live?.trades || []).find((t) => t.status === "open" && t.home === fx.home && t.away === fx.away && t.team_side === side && Number(t.line) === Number(line));
}
function cornersSection(fx) {
  const co = fx.corners;
  const lg = fxLeague(fx);
  if (!co?.home || !co?.away) {
    const err = state.data.corners_model?.[lg]?.error;
    return err && fx.kickoff ? `<div class="section-title">Corners</div><p class="note">No corners model for ${esc(theLeague(lg))} in this update: there weren't enough recent matches with corner counts.</p>` : "";
  }
  const pin = co.pinnacle;
  const rule = pfById("corners")?.live?.rule;
  const level = rule?.threshold ?? PAPER_EDGE;
  const picks = [];
  const team = (side) => {
    const m = co[side];
    const lines = pin?.[side] || [];
    const name = esc(fx[side]);
    if (!lines.length) {
      const rows = [3.5, 4.5, 5.5].map((line) => `<tr><td>Over ${line}</td><td>${pct(overFrom(m.cdf, line))}</td></tr>`).join("");
      return `<div class="goals-team"><b>${name}</b> <span class="muted small">model expects ${Number(m.mean).toFixed(1)}</span>
        <table class="mkts goals"><thead><tr><th>Corners</th><th>Model</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    }
    const rows = lines.map((q) => {
      const eo = q.p_over != null ? q.p_over * q.over + (q.p_push || 0) - 1 : null;
      const eu = q.p_under != null ? q.p_under * q.under + (q.p_push || 0) - 1 : null;
      const best = eo == null && eu == null ? null : (eu ?? -9) > (eo ?? -9) ? ["under", eu] : ["over", eo];
      if (q.pick) picks.push({ side, q, trade: cornerTrade(fx, side, q.line) });
      return `<tr class="${q.pick ? "pick" : ""}"><td>${Number(q.line)}</td><td>${pct(q.p_over)}</td><td>${pct(q.fair_over)}</td><td>${american(q.over)}</td><td>${american(q.under)}</td><td class="${q.pick ? "edge-pos" : ""}">${best ? `${signedPct(best[1], 0)}<span class="mk-range">${best[0]}</span>` : "–"}</td></tr>`;
    }).join("");
    return `<div class="goals-team"><b>${name}</b> <span class="muted small">model expects ${Number(m.mean).toFixed(1)}</span>
      <table class="mkts corners-t"><thead><tr><th>Line</th><th>Model</th><th>PIN</th><th>Over</th><th>Under</th><th>Edge</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  };
  const body = team("home") + team("away");
  const pickHtml = picks.map(({ side, q, trade }) => `<span class="badge${trade ? " paper" : ""}">${CHECK}${trade ? "Paper trade open" : "Live-test pick"}: ${esc(fx[side])} ${q.pick.side} ${Number(q.line)} @ ${american(q.pick.odds)} <span class="num">(${signedPct(q.pick.edge)})</span></span>`).join("");
  const when = pin?.fetched_at ? espnTime(pin.fetched_at) : null;
  const weak = WEAK_CORNER_FIT.has(lg) ? ` Model (f) fits less well in ${esc(theLeague(lg))}: in testing its chances there were too spread out.` : "";
  return `
    <div class="section-title">Corners</div>
    <div class="explain live-test"><b>Live test.</b> Team corners are paper-traded: model (f) against Pinnacle's price, a $10 trade at ${pct(level)}+ edge. No edge is proven yet; the February test decides. <button class="linkish" data-goto-pf="corners">Team corners portfolio</button></div>
    <div class="card" style="padding:8px 14px">${body}${pickHtml ? `<div class="corner-picks">${pickHtml}</div>` : ""}</div>
    <p class="note">${pin ? `Model = model (f)'s chance of the over; on a whole line (say 5) exactly 5 is a push and the stake comes back. PIN = Pinnacle's chance of the over, margin removed. Over/Under = Pinnacle's odds${when ? `, logged ${esc(when)}` : ""}. Edge = the better side's model chance × odds (+ push chance) − 1. A pick here uses Pinnacle's latest logged price; a paper trade also needs a price from the last 3 hours, so not every pick becomes a trade.` : "Model = model (f)'s chance of each team going over the line. No Pinnacle price for this match yet: team corners are logged about a day before kickoff and again just before it."} Teams with little top-flight history are pulled toward the league average.${weak}</p>`;
}

function playersSection(fx) {
  const ps = fx.players;
  if (!ps || !ps.length) return "";
  const st = state.data.players_status || {};
  const gate = st.gate;
  const book = playerBook();
  const anyLines = ps.some((pl) => pl.lines?.length);
  const anyBlend = ps.some((pl) => pl.lines?.some((ln) => ln.p != null));
  // One grid per player: line, price, raw model, blend (the chance picks use), book's implied, edge.
  const lineRow = (ln) => {
    const minE = playerEdge();
    const pick = minE != null && ln.p != null && ln.edge > 0 && ln.edge >= minE - 1e-9;
    const implied = ln.implied ?? (ln.odds > 1 ? 1 / ln.odds : null);
    return `<div class="pl-row${pick ? " pick" : ""}"><span>${esc(lineLabel(ln))}</span><span>${american(ln.odds)}</span><span>${pct(ln.p_model)}</span><span>${pct(ln.p)}</span><span>${pct(implied)}</span><span class="${pick ? "edge-pos" : ""}">${signedPct(ln.edge)}</span></div>`;
  };
  const head = `<div class="pl-row pl-hdr" aria-hidden="true"><span>Line</span><span>Odds</span><span>Model</span><span>Blend</span><span>${esc(playerBookShort())}</span><span>Edge</span></div>`;
  const rows = ps.map((pl) => {
    const lines = (pl.lines || []).slice().sort((a, b) => (b.edge ?? -9) - (a.edge ?? -9));
    const priced = lines.length
      ? `<div class="pl-grid" role="table" aria-label="${esc(pl.player)}: ${esc(book)} lines">${head}${lines.map(lineRow).join("")}</div>`
      : `<div class="meta">1+ shot ${pct(pl.chances?.["shots_o0.5"])} · 2+ ${pct(pl.chances?.["shots_o1.5"])} · 1+ on target ${pct(pl.chances?.["sot_o0.5"])}</div>`;
    return `
      <div class="player-row">
        <div class="player-head"><b>${esc(pl.player)}</b> <span class="muted small">${esc(pl.team)} · ${esc(pl.position || "")}</span><span class="num small">${pl.exp_shots.toFixed(1)} shots · ${pl.exp_sot.toFixed(1)} on target</span></div>
        ${priced}
        ${pl.p_play != null && pl.p_play < 1 ? `<div class="meta">FPL: ${pct(pl.p_play)} chance of playing</div>` : ""}
      </div>`;
  }).join("");
  const status = [];
  if (anyLines && playerTradesOff()) status.push("<b>Player paper trades are off.</b> No player rule has made money in testing, so these lines are shown for interest only.");
  if (st.blend_note) status.push(esc(st.blend_note));
  else if (anyLines && !anyBlend) status.push("No blended chance yet, so there is no edge or pick.");
  const note = anyLines
    ? `Model = the player model alone. Blend = the model mixed with ${book}'s price (fitted on past lines), the chance picks use. ${playerBookShort()} = ${book}'s chance, 1 / odds (their lines are over-only, so it includes their margin). Edge = blend × payout − 1. Chances assume he plays (bets on non-players are void).`
    : gate?.passed ? `No ${book} player lines for this match yet.` : `Expected shots if he plays. ${book} player lines appear once the player model beats its baseline in testing.`;
  return `
    <div class="section-title">Player shots</div>
    ${status.length ? `<div class="explain">${status.join(" ")}</div>` : ""}
    <div class="card">${rows}</div>
    <p class="note">${note}</p>`;
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

// ---------- competitions (football-data league codes) ----------
const LEAGUES = [["E0", "Premier League", "Premier"], ["SP1", "La Liga", "La Liga"], ["D1", "Bundesliga", "Bundesliga"], ["I1", "Serie A", "Serie A"], ["F1", "Ligue 1", "Ligue 1"], ["E1", "Championship", "Championship"], ["E2", "League One", "League One"], ["E3", "League Two", "League Two"]];
const leagueName = (c) => state.data?.leagues?.find((l) => l.code === c)?.name || LEAGUES.find(([k]) => k === c)?.[1] || c;
// "the Premier League", "the Championship", "the Bundesliga", but "La Liga", "Serie A", "Ligue 1".
const theLeague = (c) => (["E0", "E1", "E2", "E3", "D1"].includes(c) ? `the ${leagueName(c)}` : leagueName(c));
const leagueShort = (c) => LEAGUES.find(([k]) => k === c)?.[2] || leagueName(c);
const fxLeague = (fx) => fx.league || "E0"; // older data: Premier League only
// Understat's xG covers these; elsewhere (the Championship) the model runs on goals only,
// so a card's expected goals aren't labelled "xG".
const XG_LEAGUES = new Set(["E0", "SP1", "D1", "I1", "F1"]);
const xgLabel = (fx) => (XG_LEAGUES.has(fxLeague(fx)) ? "xG" : "exp. goals");
const tLeague = (t) => t.league || "E0";
// Competitions in the data: fixtures plus Moneyline's trades, in LEAGUES order.
function leaguesPresent() {
  const ml = pfById("moneyline");
  const co = pfById("corners");
  const codes = new Set([...(state.data.fixtures || []).map(fxLeague), ...(state.data.leagues || []).filter((l) => l.fixtures > 0).map((l) => l.code), ...[...(ml?.live?.trades || []), ...(ml?.backtest?.trades || []), ...(co?.live?.trades || [])].map(tLeague)]);
  const order = LEAGUES.map(([k]) => k);
  return [...codes].sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99) || a.localeCompare(b));
}
const multiLeague = () => leaguesPresent().length > 1;
// The selected competition, "" for all (or when only one competition is in the data).
const leagueOn = () => (multiLeague() && leaguesPresent().includes(state.league) ? state.league : "");
// All + one chip per competition; nothing at all with a single competition.
function leagueFilter() {
  if (!multiLeague()) return "";
  const cur = leagueOn();
  const chip = (code, label) => `<button class="chip${cur === code ? " on" : ""}" data-league="${esc(code)}" aria-pressed="${cur === code}">${esc(label)}</button>`;
  return `<div class="chips" role="group" aria-label="Competition">${chip("", "All")}${leaguesPresent().map((c) => chip(c, leagueShort(c))).join("")}</div>`;
}
// ---------- Teams tab per league (data.teams_by_league; older data: E0 only) ----------
function teamsLeagues() {
  const by = state.data.teams_by_league || {};
  const order = LEAGUES.map(([k]) => k);
  const codes = Object.keys(by).length ? Object.keys(by) : ["E0"];
  return codes.sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99));
}
// The Teams tab shows one league: the chosen competition, or the Premier League under "All".
const teamsLeague = () => (teamsLeagues().includes(state.league) ? state.league : teamsLeagues().includes("E0") ? "E0" : teamsLeagues()[0]);
function teamsBlock(lg = teamsLeague()) {
  const d = state.data;
  return d.teams_by_league?.[lg] || (lg === "E0" ? { name: d.league, teams: d.teams, ratings: d.ratings, xg: !!d.xg_weight } : null);
}
// One chip per league with ratings (no "All": the tab shows one table at a time).
function teamsLeagueChips() {
  const codes = teamsLeagues();
  if (codes.length < 2) return "";
  const cur = teamsLeague();
  return `<div class="chips" role="group" aria-label="Competition">${codes.map((c) => `<button class="chip${c === cur ? " on" : ""}" data-league="${esc(c)}" aria-pressed="${c === cur}">${esc(leagueShort(c))}</button>`).join("")}</div>`;
}
// Season stats file per league; E0's players_stats.json also carries FPL's active flags.
const psFile = (lg) => (lg === "E0" ? state.data.players_stats : state.data.players_stats_by_league?.[lg]);
const curPs = () => state.psBy[teamsLeague()] || null;
const psE0 = () => state.psBy.E0 || null;

// With "All" and per-league levels: one short line per competition.
function leagueEdgeList(pfId, lg) {
  if (pfId === "moneyline" && fixedRule()) {
    if (lg || !multiLeague()) return "";
    const levels = [...new Set(leaguesPresent().map((c) => leagueRule(c)?.threshold).filter((v) => v != null))];
    return levels.length === 1 ? `<div class="meta">The same ${pct(levels[0])} in every competition.</div>` : "";
  }
  const by = pfById(pfId)?.backtest?.edge_threshold?.by_league || {};
  const rules = pfId === "moneyline" ? state.data.portfolio?.rules || {} : {};
  if (lg || !multiLeague() || (!Object.keys(by).length && !Object.keys(rules).length)) return "";
  const items = leaguesPresent().filter((c) => by[c] || rules[c]).map((c) => { const e = edgeInfo(pfId, c); return `${esc(leagueShort(c))}: ${e?.min_edge == null ? "nothing flagged" : `${pct(e.min_edge)}+`}`; });
  return items.length ? `<div class="meta">By competition: ${items.join(" · ")}.</div>` : "";
}

// ---------- minimum edge, learned from past bets ----------
// edge_threshold from a portfolio's backtest (research lab): min_edge (null = no edge level
// beat the market), confidence, method, n_bets, seasons, note, by_bucket. Missing = older data.
// With a league, that league's own level (edge_threshold.by_league[code]) when it has one.
// Moneyline also has portfolio.rules[code] ({threshold, source, note}; source "none" = no
// learned level) for leagues without their own edge_threshold.
function edgeInfo(pfId, lg = "") {
  const et = pfById(pfId)?.backtest?.edge_threshold || null;
  if (lg && et?.by_league?.[lg]) return et.by_league[lg];
  // Under the owner's fixed rule, rules[code] is the paper rule, not a learned level.
  const r = pfId === "moneyline" && lg && lg !== "E0" && !fixedRule() ? state.data.portfolio?.rules?.[lg] : null;
  if (r) return { min_edge: r.source === "none" ? null : r.threshold ?? null, note: r.note || "", by_bucket: [] };
  return et;
}
// The minimum edge in force for a portfolio: an explored step, else history's level,
// else (no edge_threshold yet) the paper-trade rule. edge null = flag nothing.
function edgeRule(pfId, lg = "") {
  if (state.exploreEdge != null) return { edge: state.exploreEdge, source: "explore" };
  if (pfId === "moneyline" && fixedRule()) return { edge: leagueRule(lg)?.threshold ?? PAPER_EDGE, source: "owner_fixed" };
  const et = edgeInfo(pfId, lg);
  if (!et) return { edge: PAPER_EDGE, source: "default" };
  return { edge: et.min_edge ?? null, source: "history" };
}
const matchEdge = (lg = leagueOn()) => edgeRule("moneyline", lg).edge;
const fxEdge = (fx) => edgeRule("moneyline", fxLeague(fx)).edge; // each match by its own league
const playerEdge = () => edgeRule("player_shots").edge;
// "a 12%" but "an 8%" / "an 11%" / "an 18%".
const aPct = (x) => { const t = pct(x); return `${/^(8|11|18)/.test(t) ? "an" : "a"} ${t}`; };
const seasonLabel = (x) => String(x).replace(/^(\d{4})\b/, (m) => seasonName(m));
function seasonsText(v) {
  if (!v) return "";
  if (Array.isArray(v)) return v.map(seasonLabel).join(", ");
  if (typeof v === "object") {
    const dev = (v.development || []).map(seasonLabel).join(", ");
    const chk = (v.check || []).map(seasonLabel).join(", ");
    return [dev && `found on ${dev}`, chk && `checked on ${chk}`].filter(Boolean).join(", ");
  }
  return seasonLabel(v);
}
const confText = (c) => (c == null ? "" : typeof c === "number" ? `${pct(c)} confidence` : `${c} confidence`);
// Why there is no pick, in one sentence (Matches note, match sheet).
function noEdgeText(pfId, lg = "") {
  const et = edgeInfo(pfId, lg);
  return `Nothing is flagged. ${et?.note ? esc(et.note) : "No edge level has beaten the market in past bets."}`;
}

// The recommendation, in plain English, with the old edge buttons folded away under
// "Explore other edges" (they override the recommendation until reset).
function edgePanel(pfId = "moneyline", lg = "") {
  if (pfId === "moneyline" && fixedRule()) return fixedEdgePanel(lg);
  const et = edgeInfo(pfId, lg);
  const rule = edgeRule(pfId, lg);
  const basis = et ? [et.n_bets ? `${et.n_bets.toLocaleString()} past bets` : "", esc(seasonsText(et.seasons)), confText(et.confidence)].filter(Boolean).join(" · ") : "";
  const rec = !et
    ? `Flagging bets with at least ${aPct(PAPER_EDGE)} edge, the paper-trade rule. A level learned from past bets appears here once the backtest has one.`
    : et.min_edge == null
      ? `<b>Nothing is flagged.</b> ${et.note ? esc(et.note) : "No edge level has beaten the market in past bets."}`
      : `<b>Only flag bets with at least ${aPct(et.min_edge)} edge:</b> below that, past bets didn't beat the market.${et.note ? ` ${esc(et.note)}` : ""}`;
  const exploring = rule.source === "explore";
  return `
    <div class="edge-panel">
      <div class="edge-rec">${rec}${basis ? `<div class="meta">Based on ${basis}.</div>` : ""}${leagueEdgeList(pfId, lg)}</div>
      ${exploring ? `<div class="edge-exploring">Exploring: flagging ${pct(state.exploreEdge)}+ instead. <button class="linkish" data-edge="reset">Back to the recommended level</button></div>` : ""}
      <details class="fold edge-explore"${exploring ? " open" : ""}>
        <summary>Explore other edges</summary>
        <div class="segmented" role="group" aria-label="Minimum edge to explore" style="margin-top:10px">
          ${EDGE_STEPS.map((e) => `<button data-edge="${e}" class="${e === state.exploreEdge ? "on" : ""}" aria-pressed="${e === state.exploreEdge}">${pct(e)}</button>`).join("")}
        </div>
        <p class="note">For a look only: this changes what the app flags until you go back. Paper trades always follow the recommended minimum above.</p>
      </details>
    </div>`;
}

// The owner's fixed live test: the level in force, the reason, and (folded) what the
// backtests found, so the learned result stays visible beside the rule that overrides it.
const fixedNote = (lg) => leagueRule(lg)?.note || paperRule()?.note || "";
// The match sheet's live-test line. One paper trade per match: once one is open, a later
// price move can flag another outcome (or none), but no second trade opens.
function fixedPickText(fx, pick) {
  const t = (pfById("moneyline")?.live?.trades || []).find((x) => x.status === "open" && x.bet_type !== "player" && x.home === fx.home && x.away === fx.away);
  const now = pick ? `${esc(PICK_LABEL[pick.market] || pick.market)} at ${price(pick.odds)}, ${signedPct(pick.edge)} edge on the model alone` : "";
  if (t) {
    const same = pick && PICK_LABEL[pick.market] === PICK_LABEL[t.market];
    return `<b>Paper trade already open:</b> ${esc(tradeLabel(t))} at ${price(t.odds)}. Only one trade per match, so nothing new opens here.${same ? ` It is still the live-test pick at today's price: ${now}.` : pick ? ` At today's price the live test would pick ${now}.` : !pick ? " At today's price no outcome reaches the live test's edge any more; the trade stays open until it settles." : ""}`;
  }
  if (pick) return `<b>Live-test pick:</b> ${now}. A $10 paper trade opens on it at the next update with fresh odds.`;
  if (fixedSkips(fx)) return "<b>No pick here.</b> One team has few matches in the model, so the live test skips this match.";
  return `<b>No pick here.</b> No outcome reaches the ${pct(fxEdge(fx))} edge your live test needs.`;
}
function learnedText(lg) {
  const et = edgeInfo("moneyline", lg);
  if (!et) return "No backtest result yet for this competition.";
  const basis = [et.n_bets ? `${et.n_bets.toLocaleString()} past bets` : "", esc(seasonsText(et.seasons))].filter(Boolean).join(" · ");
  const head = et.min_edge == null
    ? "<b>Backtests found no edge level that made money.</b>"
    : `<b>Backtests found that bets at ${aPct(et.min_edge)}+ edge made money</b> (on the model blended with the price).`;
  return `${head}${et.note ? ` ${esc(et.note)}` : ""}${basis ? `<div class="meta">Based on ${basis}.</div>` : ""}`;
}
function fixedEdgePanel(lg = "") {
  const th = edgeRule("moneyline", lg);
  const level = leagueRule(lg)?.threshold ?? PAPER_EDGE;
  const exploring = th.source === "explore";
  return `
    <div class="edge-panel">
      <div class="edge-rec"><b>Fixed ${pct(level)} (your live test).</b> Flagging every pick where the model alone, without the market blend, shows at least ${aPct(level)} edge against DraftKings' price: exactly what paper trades take. This rule lost money in backtests; the live test measures it on real prices.${leagueEdgeList("moneyline", lg)}</div>
      ${exploring ? `<div class="edge-exploring">Exploring: flagging ${pct(state.exploreEdge)}+ instead. <button class="linkish" data-edge="reset">Back to the live-test level</button></div>` : ""}
      <details class="fold edge-learned">
        <summary>What the backtests found</summary>
        <div class="edge-rec" style="margin-top:8px">${learnedText(lg)}</div>
      </details>
      <details class="fold edge-explore"${exploring ? " open" : ""}>
        <summary>Explore other edges</summary>
        <div class="segmented" role="group" aria-label="Minimum edge to explore" style="margin-top:10px">
          ${EDGE_STEPS.map((e) => `<button data-edge="${e}" class="${e === state.exploreEdge ? "on" : ""}" aria-pressed="${e === state.exploreEdge}">${pct(e)}</button>`).join("")}
        </div>
        <p class="note">For a look only: this changes what the app flags until you go back. Paper trades always follow the fixed ${pct(level)} live test above.</p>
      </details>
    </div>`;
}

// What the chart draws: the portfolio's own edge_threshold buckets, else (too few bets
// there, by_bucket empty) a bigger pool from the same backtest, with a caption.
const hasBuckets = (et) => (et?.by_bucket || []).some((r) => r.n);
function edgeChartSource(pfId, lg = "") {
  const bt = pfById(pfId)?.backtest || {};
  const et = edgeInfo(pfId, lg);
  if (hasBuckets(et)) return { et, caption: "" };
  if (lg && lg !== "E0") return null; // the fallback pools below are the Premier League's
  if (hasBuckets(bt.edge_threshold_pinnacle)) return { et: bt.edge_threshold_pinnacle, caption: "Too few bets of its own yet, so this shows the model alone (not the live blend) against Pinnacle's early price, over more matches (football-data)." };
  const raw = bt.strategies?.raw?.edge_threshold;
  if (hasBuckets(raw)) return { et: raw, caption: "Too few bets of its own yet, so this shows the model alone (not the live blend) at DraftKings' prices." };
  return null;
}

// Claimed edge vs what happened: per edge bucket, the book's implied chance, the model's
// chance and the realized win rate with its range. A dot plot; tap a row for the readout.
function edgeBucketsHtml(pfId, lg = "") {
  const src = edgeChartSource(pfId, lg);
  if (!src) return "";
  const et = src.et;
  const rows = et.by_bucket.filter((r) => r.n);
  const vals = rows.flatMap((r) => [r.implied, r.model, r.realized, r.realized_lo, r.realized_hi]).filter((v) => v != null);
  const hi = Math.min(1, Math.ceil((Math.max(...vals) + 0.02) * 10) / 10);
  const lo = Math.max(0, Math.floor((Math.min(...vals) - 0.02) * 10) / 10);
  const W = 100; // percent of the plot column
  const x = (v) => ((v - lo) / (hi - lo || 1)) * W;
  const label = (r) => `${pct(r.edge_lo)}${r.edge_hi != null ? `–${pct(r.edge_hi)}` : "+"}`;
  const ticks = [];
  for (let v = lo; v <= hi + 1e-9; v += (hi - lo) / 4) ticks.push(v);
  const body = rows.map((r, i) => {
    const beat = r.realized != null && r.implied != null && r.realized >= r.implied;
    const range = r.realized_lo != null && r.realized_hi != null
      ? `<span class="eb-range ${beat ? "pos" : "neg"}" style="left:${x(r.realized_lo)}%;width:${Math.max(0.5, x(r.realized_hi) - x(r.realized_lo))}%"></span>` : "";
    const dot = (v, cls) => (v == null ? "" : `<span class="eb-dot ${cls}" style="left:${x(v)}%"></span>`);
    const above = et.min_edge != null && r.edge_lo >= et.min_edge - 1e-9;
    return `<button class="eb-row${above ? " above" : ""}" data-ebrow="${i}" data-eb="${esc(pfId)}">
      <span class="eb-label">${label(r)}<span class="meta">${r.n} bets</span>${r.roi != null ? `<span class="meta ${plClass(r.roi)}">ROI ${signedPct(r.roi, 0)}</span>` : ""}</span>
      <span class="eb-plot">${range}${dot(r.implied, "imp")}${dot(r.model, "mod")}${dot(r.realized, `real ${beat ? "pos" : "neg"}`)}</span>
    </button>`;
  }).join("");
  const axis = `<div class="eb-axis"><span></span><span class="eb-plot">${ticks.map((v) => `<span style="left:${x(v)}%">${pct(v)}</span>`).join("")}</span></div>`;
  return `
    <div class="section-title">Claimed edge vs what happened</div>
    <div class="card eb-card" data-ebcard="${esc(pfId)}">
      <div class="eb-legend"><span><i class="eb-key imp"></i>Book's chance</span><span><i class="eb-key mod"></i>Model's chance</span><span><i class="eb-key real pos"></i>Won (range)</span></div>
      ${body}
      ${axis}
      <div class="eb-readout" id="eb-readout-${esc(pfId)}">${ebReadout(et, rows.length - 1)}</div>
    </div>
    <p class="note">${src.caption ? `${esc(src.caption)} ` : ""}Each row groups past bets by the edge the model claimed, with their return per bet. A real edge wins more often than the book's chance (blue); red means it won less. Rows at or above the recommended level are shaded.${et.method ? ` Method: ${esc(et.method)}.` : ""}</p>`;
}
function ebReadout(et, i) {
  const r = (et.by_bucket || []).filter((b) => b.n)[i];
  if (!r) return "";
  return `<b>Edge ${pct(r.edge_lo)}${r.edge_hi != null ? `–${pct(r.edge_hi)}` : "+"}</b>, ${r.n} bets: book ${pct(r.implied, 1)}, model ${pct(r.model, 1)}, won ${pct(r.realized, 1)}${r.realized_lo != null ? ` (range ${pct(r.realized_lo, 0)}–${pct(r.realized_hi, 0)})` : ""}${r.roi != null ? `; return per bet ${signedPct(r.roi)}${r.roi_lo != null ? ` (range ${signedPct(r.roi_lo, 0)} to ${signedPct(r.roi_hi, 0)})` : ""}` : ""}.`;
}

// ---------- views ----------
function viewMatches() {
  const d = state.data;
  if (!d.fixtures.length) {
    return `<div class="empty">No upcoming fixtures found.<br>Try the Explore tab.</div>`;
  }
  const lg = leagueOn();
  const shown = d.fixtures.map((fx, idx) => [fx, idx]).filter(([fx]) => !lg || fxLeague(fx) === lg);
  const byDay = new Map();
  shown.forEach(([fx, idx]) => {
    const day = new Date(fx.kickoff).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
    if (!byDay.has(day)) byDay.set(day, []);
    byDay.get(day).push([fx, idx]);
  });
  const minE = matchEdge(lg);
  const nValue = shown.filter(([fx]) => bestPick(fx, fxEdge(fx))).length;
  const allNull = shown.every(([fx]) => fxEdge(fx) == null);
  const openTrades = new Map((pfById("moneyline")?.live?.trades || []).filter((t) => t.status === "open" && t.bet_type !== "player").map((t) => [`${t.home}|${t.away}`, t]));
  let html = `${leagueFilter()}<p class="note">Chances of each result: the model vs ${bookName()}. ${oddsAge()} ${lg && shown.every(([fx]) => !fx.odds) ? `No ${bookName()} odds for ${esc(leagueName(lg))} matches yet: odds for this league are fetched from 48 hours before a kickoff, so there are no value bets here.` : fixedRule() ? `<b>${nValue}</b> of ${shown.length} matches have a live-test pick: the model alone shows ${minE != null ? `${pct(minE)}+` : "enough"} edge against ${bookPoss()} price. A $10 paper trade opens on each match that doesn't have one yet (one per match).` : nValue ? `<b>${nValue}</b> of ${shown.length} matches have a value bet${minE != null ? ` at ${pct(minE)}+ edge` : ""}.` : allNull ? noEdgeText("moneyline", lg) : d.match_blend?.live ? `No value bets right now. Value picks use the model blended with ${bookPoss()} price, and in past matches the price already held what the model knows, so the blend rarely beats ${bookPoss()} margin. Tap a match to see model, blend and ${bookName()} side by side.` : "No value bets right now."}</p>${fixedRule() && fixedNote(lg) ? `<div class="explain live-test">${esc(fixedNote(lg))}</div>` : ""}`;
  for (const [day, items] of byDay) {
    html += `<div class="section-title">${esc(day)}</div>`;
    for (const [fx, idx] of items) {
      const pick = bestPick(fx, fxEdge(fx));
      const time = new Date(fx.kickoff).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
      html += `
        <button class="card match" data-fixture="${idx}">
          <div class="match-head"><span>${esc(time)}${!lg && multiLeague() ? ` · ${esc(leagueName(fxLeague(fx)))}` : ""}</span><span>Details ›</span></div>
          <div class="teams">
            <span>${esc(fx.home)}</span><span class="xg num">${fx.xg[0].toFixed(1)} ${xgLabel(fx)}</span>
            <span>${esc(fx.away)}</span><span class="xg num">${fx.xg[1].toFixed(1)} ${xgLabel(fx)}</span>
          </div>
          ${compareTable(fx, pick)}
          ${newsLine(fx)}
          ${lineupConfirmed(fx) ? `<span class="badge lineup">${CHECK}Lineups confirmed</span>` : ""}
          ${pick ? `<span class="badge">${CHECK}${pickBadge(pick, openTrades.get(`${fx.home}|${fx.away}`))}: ${esc(PICK_LABEL[pick.market])} @ ${price(pick.odds)} <span class="num">(${signedPct(pick.edge)})</span></span>` : ""}
          ${openTrades.has(`${fx.home}|${fx.away}`) ? (() => { const t = openTrades.get(`${fx.home}|${fx.away}`); const { cur } = currentPrice(t); const m = markToMarket(t, cur); return `<span class="badge paper">Paper trade open: ${esc(tradeLabel(t))} @ ${american(t.odds)} · now ${american(cur)} <b class="${plClass(m)}">${usd(m, 2)}</b></span>`; })() : ""}
          ${fx.low_data ? '<div class="warn">⚠ Few matches for one team</div>' : ""}
        </button>`;
    }
  }
  const pp = shown.flatMap(([fx, idx]) => playerPicks(fx, playerEdge()).map((x) => ({ ...x, fx, idx })));
  if (pp.length) {
    html += `<div class="section-title">Player picks</div><div class="card">${pp.map((x) => `
      <button class="bet-row trade" data-fixture="${x.idx}">
        <span>${esc(x.player)} <span class="muted small">${esc(x.team)}</span><div class="meta">${esc(x.fx.home)} v ${esc(x.fx.away)} · ${esc(lineLabel(x))} @ ${american(x.odds)} · chance ${pct(x.p)}</div></span>
        <span class="pl win">${signedPct(x.edge)}</span>
      </button>`).join("")}</div>${playerTradesOff() ? '<p class="note">Shown for interest only: player paper trades are off until a player rule makes money in testing.</p>' : ""}`;
  }
  html += `
    <div class="section-title">How to read this</div>
    <p class="note" style="margin-top:0">${bookName()} = ${isDK() ? "DraftKings'" : "the bookmaker's"} odds turned into chances, with the built-in margin taken out so the three add up to 100%. A value bet is where the model rates an outcome high enough that the odds pay more than it's worth.${fixedRule() ? " Your live test checks value with the model alone, not blended with the price." : d.match_blend?.live ? " The value check uses the model blended with DraftKings' price, weighted by how much the model added in past matches." : ""}</p>
    <div class="section-title">Minimum edge</div>
    ${edgePanel("moneyline", lg)}`;
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

function teamsToggle() {
  return `
    <div class="segmented" role="group" aria-label="Teams or players" style="margin-bottom:10px">
      ${[["teams", "Teams"], ["players", "Players"]].map(([k, l]) => `<button data-tv="${k}" class="${state.teamsView === k ? "on" : ""}" aria-pressed="${state.teamsView === k}">${l}</button>`).join("")}
    </div>`;
}

// ---------- players: backtest expected vs actual shots, and next match ----------
async function loadPlayersBacktest() {
  if (state.pb || !state.data.players_backtest) return;
  try {
    const res = await fetch(state.data.players_backtest, { cache: "no-cache" });
    if (!res.ok) throw new Error(res.statusText);
    state.pb = await res.json();
  } catch (err) {
    state.pb = { error: String(err.message || err), players: [], apps: {} };
  }
  if (state.tab === "ratings" && state.teamsView === "players") render();
}

// Next-match expected shots per player, from the fixture cards.
function nextMatchShots() {
  const out = new Map();
  for (const fx of state.data.fixtures) {
    for (const pl of fx.players || []) {
      if (!out.has(pl.player_id)) out.set(pl.player_id, { ...pl, opp: pl.team === fx.home ? fx.away : fx.home, home: pl.team === fx.home, kickoff: fx.kickoff });
    }
  }
  return out;
}

function playerRows() {
  const next = nextMatchShots();
  const bt = state.pb?.players || [];
  const rows = bt.map((r) => ({ ...r, next: next.get(r.player_id) }));
  const seen = new Set(rows.map((r) => r.player_id));
  for (const [pid, n] of next) {  // players with a next match but no backtest record yet
    if (!seen.has(pid)) rows.push({ player_id: pid, player: n.player, team: n.team, position: n.position, apps: 0, next: n });
  }
  return rows;
}

const PL_SORTS = {
  exp: ["Expected shots (backtest)", (r) => r.exp_shots ?? -1],
  shots: ["Actual shots (backtest)", (r) => r.shots ?? -1],
  pergame: ["Expected shots per game", (r) => (r.apps ? r.exp_shots / r.apps : -1)],
  next: ["Next match expected shots", (r) => r.next?.exp_shots ?? -1],
  over: ["Shot more than expected", (r) => (r.apps ? (r.shots - r.exp_shots) / r.apps : -99)],
  under: ["Shot less than expected", (r) => (r.apps ? (r.exp_shots - r.shots) / r.apps : -99)],
};

// "Martin Ødegaard" -> "martin odegaard", so searches work without accents.
const FOLD = { "ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "đ": "d", "ł": "l", "ı": "i" };
const foldName = (s) => s.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/[øæœßđłı]/g, (c) => FOLD[c]);

function filteredPlayers() {
  const f = state.pl;
  const q = foldName(f.q.trim());
  const act = psE0()?.players?.length ? activeMap() : null;
  const rows = playerRows().filter((r) =>
    (!f.active || !act || act.get(r.player_id)?.active || r.next) &&
    (!q || foldName(r.player).includes(q)) &&
    (!f.team || r.team === f.team) && (!f.pos || r.position === f.pos));
  const key = PL_SORTS[f.sort][1];
  return rows.sort((a, b) => key(b) - key(a));
}

function playerListHtml() {
  const rows = filteredPlayers();
  if (!rows.length) return '<p class="muted" style="padding:6px 0">No players match these filters.</p>';
  const shown = rows.slice(0, state.pl.shown);
  const body = shown.map((r) => {
    const per = r.apps ? `${(r.exp_shots / r.apps).toFixed(2)} expected vs ${(r.shots / r.apps).toFixed(2)} actual a game` : "no backtest record yet";
    const diff = r.apps ? r.shots - r.exp_shots : null;
    const next = r.next ? `<div class="meta">Next: ${r.next.home ? "v" : "at"} ${esc(r.next.opp)} · ${r.next.exp_shots.toFixed(1)} expected shots, ${r.next.exp_sot.toFixed(1)} on target</div>` : "";
    return `
      <button class="bet-row trade" data-player="${esc(r.player_id)}">
        <span>${esc(r.player)} <span class="muted small">${esc(r.team)} · ${esc(r.position || "")}</span><div class="meta">${r.apps ? `${r.apps} games · ` : ""}${per}</div>${next}</span>
        <span class="pl num">${r.apps ? `${r.exp_shots.toFixed(0)} → ${r.shots}` : r.next ? r.next.exp_shots.toFixed(1) : "–"}${diff != null ? `<div class="meta ${plClass(diff)}">${signed(diff, 1)}</div>` : ""}</span>
      </button>`;
  }).join("");
  return `${body}${rows.length > shown.length ? `<button class="more" id="pl-more">Show more (${rows.length - shown.length} left)</button>` : ""}`;
}

function playersModeToggle() {
  return `
    <div class="segmented small-seg" role="group" aria-label="Player view" style="margin:0 0 10px">
      ${[["stats", "Season stats"], ["model", "Model backtest"]].map(([k, l]) => `<button data-plmode="${k}" class="${state.pl.mode === k ? "on" : ""}" aria-pressed="${state.pl.mode === k}">${l}</button>`).join("")}
    </div>`;
}

// One fetch per league file: a re-render while it is loading reuses the request in flight.
const psLoading = {};
function loadPlayerStats(lg = teamsLeague()) {
  const file = psFile(lg);
  if (state.psBy[lg] || !file) return Promise.resolve();
  if (psLoading[lg]) return psLoading[lg];
  psLoading[lg] = (async () => {
    try {
      const res = await fetch(file, { cache: "no-cache" });
      if (!res.ok) throw new Error(res.statusText);
      state.psBy[lg] = await res.json();
    } catch (err) {
      state.psBy[lg] = { error: String(err.message || err), players: [], seasons: [] };
    } finally {
      delete psLoading[lg];
    }
    if (state.tab === "ratings" && state.teamsView === "players") render();
  })();
  return psLoading[lg];
}

// Who's in a current Premier League squad (from players_stats.json's FPL matching).
function activeMap() {
  const m = new Map();
  for (const r of psE0()?.players || []) {
    const cur = m.get(r.player_id);
    m.set(r.player_id, { active: (cur?.active || false) || !!r.active, team: r.current_team || cur?.team || null });
  }
  return m;
}
function activeToggle() {
  const on = state.pl.active;
  return `<button class="chip ${on ? "on" : ""}" id="pl-active" aria-pressed="${on}">${on ? CHECK : ""}Active players only</button>`;
}

const per90 = (n, min) => (min > 0 ? (n * 90) / min : null);
const STAT_SORTS = {
  shots: ["Shots", (r) => r.shots],
  p90: ["Shots per 90 (300+ min)", (r) => (r.minutes >= 300 ? per90(r.shots, r.minutes) : -1)],
  sot: ["Shots on target", (r) => r.sot],
  sotpct: ["On-target % (10+ shots)", (r) => (r.shots >= 10 ? r.sot / r.shots : -1)],
  goals: ["Goals", (r) => r.goals],
  xg: ["Expected goals (xG)", (r) => r.xg],
  minutes: ["Minutes", (r) => r.minutes],
};

// Understat's league summaries (leagues other than E0) have no shots on target or starts.
const hasSot = () => (curPs()?.players || []).some((r) => r.sot != null);
const statSorts = () => Object.fromEntries(Object.entries(STAT_SORTS).filter(([k]) => hasSot() || !["sot", "sotpct"].includes(k)));
const devMetrics = () => Object.fromEntries(Object.entries(DEV_METRICS).filter(([k]) => hasSot() || !["sot90", "sotpct"].includes(k)));

function statRows() {
  const f = state.pl;
  const ps = curPs();
  const season = ps?.seasons?.includes(f.season) ? f.season : ps?.seasons?.[0];
  const q = foldName(f.q.trim());
  const e0 = teamsLeague() === "E0"; // FPL's active flags exist for the Premier League only
  const act = activeMap();
  const rows = (ps?.players || []).filter((r) => r.season === season &&
    (!e0 || !f.active || act.get(r.player_id)?.active) &&
    (!q || foldName(r.player).includes(q)) && (!f.team || r.team === f.team) && (!f.pos || r.position === f.pos));
  const key = (STAT_SORTS[f.ssort] && statSorts()[f.ssort] ? STAT_SORTS[f.ssort] : STAT_SORTS.shots)[1];
  return rows.sort((a, b) => key(b) - key(a));
}

function statListHtml() {
  const rows = statRows();
  if (!rows.length) return '<p class="muted" style="padding:6px 0">No players match these filters.</p>';
  const f = state.pl;
  let club = "";
  if (f.team) {
    const t = rows.reduce((a, r) => ({ shots: a.shots + r.shots, sot: a.sot + (r.sot || 0), goals: a.goals + r.goals, xg: a.xg + r.xg }), { shots: 0, sot: 0, goals: 0, xg: 0 });
    const games = rows[0].team_games || Math.max(...rows.map((r) => r.apps));
    club = `<div class="club-total"><b>${esc(f.team)}${f.pos ? ` · ${esc(f.pos)}` : ""}</b><span class="num">${t.shots} shots in ${games} games (${games ? (t.shots / games).toFixed(1) : "–"} a game)${hasSot() ? ` · ${t.sot} on target` : ""} · ${t.goals} goals · ${t.xg.toFixed(1)} xG</span></div>`;
  }
  const shown = rows.slice(0, f.shown);
  const e0 = teamsLeague() === "E0";
  const act = activeMap();
  const body = shown.map((r) => {
    const p = per90(r.shots, r.minutes);
    const now = act.get(r.player_id);
    const moved = !e0 ? "" : now?.team && now.team !== r.team ? ` <span class="muted small">· now ${esc(now.team)}</span>` : !now?.active ? ' <span class="muted small">· left the PL</span>' : "";
    const sot = r.sot == null ? "" : ` · ${r.sot} on target${r.shots ? ` (${Math.round((r.sot / r.shots) * 100)}%)` : ""}`;
    const inner = `
        <span>${esc(r.player)} <span class="muted small">${f.team ? "" : `${esc(r.team)} · `}${esc(r.position || "")}</span>${moved}<div class="meta">${r.apps} games${r.starts != null ? ` (${r.starts} starts)` : ""} · ${r.minutes} min</div><div class="meta">${p != null ? p.toFixed(2) : "–"} per 90${sot} · ${r.goals} G · ${r.xg.toFixed(1)} xG</div></span>
        <span class="pl num">${r.shots}<div class="meta">shots</div></span>`;
    // The player sheet (model record, next match) exists for the Premier League only.
    return e0 ? `<button class="bet-row trade" data-player="${esc(r.player_id)}">${inner}</button>` : `<div class="bet-row">${inner}</div>`;
  }).join("");
  return `${club}${body}${rows.length > shown.length ? `<button class="more" id="pl-more">Show more (${rows.length - shown.length} left)</button>` : ""}`;
}

// ---------- deviation-from-the-mean chart (season stats) ----------
const DEV_METRICS = {
  p90: ["Shots per 90", (r) => per90(r.shots, r.minutes), (r) => r.minutes >= 300, 2, ""],
  sot90: ["On target per 90", (r) => per90(r.sot, r.minutes), (r) => r.minutes >= 300, 2, ""],
  sotpct: ["On-target %", (r) => (r.shots ? (100 * r.sot) / r.shots : null), (r) => r.shots >= 10, 0, "%"],
  gxg: ["Goals minus xG", (r) => r.goals - r.xg, (r) => r.shots >= 10, 1, ""],
  shots: ["Shots (total)", (r) => r.shots, () => true, 0, ""],
};
const DEV_MAX_ROWS = 40;
let devData = null;

function devChartHtml() {
  const [label, val, qualifies, dp, unit] = devMetrics()[state.pl.metric] || DEV_METRICS.p90;
  const group = statRows().filter(qualifies).map((r) => ({ r, v: val(r) })).filter((x) => x.v != null);
  if (group.length < 3) return '<p class="muted" style="padding:6px 0">Not enough qualifying players for a chart. Widen the filters.</p>';
  const mean = group.reduce((a, x) => a + x.v, 0) / group.length;
  const sd = Math.sqrt(group.reduce((a, x) => a + (x.v - mean) ** 2, 0) / (group.length - 1)) || 1;
  group.forEach((x) => { x.d = x.v - mean; x.z = x.d / sd; });
  group.sort((a, b) => b.d - a.d);
  // Too many to read: keep the top and bottom of the group.
  let rows = group, cut = false;
  if (group.length > DEV_MAX_ROWS) { rows = group.slice(0, DEV_MAX_ROWS / 2).concat(group.slice(-DEV_MAX_ROWS / 2)); cut = true; }
  const W = Math.max(300, Math.round(($("#view").clientWidth || 360) - 30)), RH = 22, T = 18, B = 24, LBL = 112;
  const H = T + rows.length * RH + (cut ? 14 : 0) + B;
  const ext = Math.max(...rows.map((x) => Math.abs(x.d)), sd) * 1.08;
  const x0 = LBL + (W - LBL - 8) / 2, half = (W - LBL - 8) / 2;
  const sx = (d) => x0 + (d / ext) * half;
  const band = `<rect class="sd-band" x="${sx(-sd)}" y="${T - 4}" width="${sx(sd) - sx(-sd)}" height="${rows.length * RH + (cut ? 14 : 0) + 4}"/>`;
  let y = T;
  const bars = rows.map((x, i) => {
    if (cut && i === DEV_MAX_ROWS / 2) y += 14;
    const yy = y; y += RH;
    const w = Math.max(Math.abs(sx(x.d) - x0), 1.5), left = x.d >= 0 ? x0 : x0 - w;
    const r = Math.min(4, w / 2);
    // rounded only at the data end, square at the baseline
    const path = x.d >= 0
      ? `M${left},${yy + 4} h${w - r} a${r},${r} 0 0 1 ${r},${r} v${RH - 8 - 2 * r} a${r},${r} 0 0 1 -${r},${r} h-${w - r} z`
      : `M${left + w},${yy + 4} h-${w - r} a${r},${r} 0 0 0 -${r},${r} v${RH - 8 - 2 * r} a${r},${r} 0 0 0 ${r},${r} h${w - r} z`;
    const name = x.r.player.length > 16 ? `${x.r.player.slice(0, 15)}…` : x.r.player;
    return `<g class="dev-row" data-i="${i}">
      <rect class="hit" x="0" y="${yy}" width="${W}" height="${RH}"/>
      <text class="dev-name" x="${LBL - 8}" y="${yy + RH / 2 + 4}" text-anchor="end">${esc(name)}</text>
      <path class="${x.d >= 0 ? "dev-pos" : "dev-neg"}" d="${path}"/>
    </g>`;
  }).join("");
  // SD labels only when there's room beside the average's label.
  const roomy = sx(sd) - x0 > 46;
  const ticks = [-1, 0, 1].filter((k) => k === 0 || roomy).map((k) => `<text class="xlab" x="${sx(k * sd)}" y="${H - 8}" text-anchor="${k < 0 ? "end" : k > 0 ? "start" : "middle"}" dx="${k * 4}">${k === 0 ? `avg ${mean.toFixed(dp)}${unit}` : `${k > 0 ? "+" : "−"}1 SD`}</text>`).join("");
  devData = { rows, mean, sd, dp, unit, label, W };
  const ps = curPs();
  const scope = [seasonName(ps.seasons.includes(state.pl.season) ? state.pl.season : ps.seasons[0]), state.pl.team || "all clubs", state.pl.pos || "all positions"].join(" · ");
  return `
    <p class="note" style="margin-top:0"><b>${esc(label)}</b>, each player against the average of this group (${esc(scope)}, ${group.length} players${state.pl.metric === "shots" ? "" : " who qualify"}). Blue = above average, red = below; the gray band is ±1 standard deviation (${sd.toFixed(dp)}${unit}).${cut ? ` Showing the top and bottom ${DEV_MAX_ROWS / 2}.` : ""}</p>
    <div class="dev-readout" id="dev-readout" aria-live="polite">Tap a bar for that player's numbers.</div>
    <div class="chart" id="dev-chart">
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}: deviation from the group average of ${mean.toFixed(dp)}${unit}">
        ${band}
        <line class="zero" x1="${x0}" x2="${x0}" y1="${T - 6}" y2="${H - B + 2}"/>
        ${bars}
        ${ticks}
      </svg>
    </div>`;
}

function bindDevChart() {
  const el = $("#dev-chart");
  if (!el || !devData) return;
  const out = $("#dev-readout");
  const show = (g) => {
    const x = devData.rows[Number(g.dataset.i)];
    el.querySelectorAll(".dev-row.on").forEach((n) => n.classList.remove("on"));
    g.classList.add("on");
    const r = x.r, u = devData.unit, dp = devData.dp;
    out.innerHTML = `<b>${esc(r.player)}</b> <span class="muted">${esc(r.team)} · ${esc(r.position || "")}</span><br>${esc(devData.label)} ${x.v.toFixed(dp)}${u} · <span class="${x.d >= 0 ? "gain" : "loss"}">${x.d >= 0 ? "+" : "−"}${Math.abs(x.d).toFixed(dp)}${u} vs average (${x.z >= 0 ? "+" : "−"}${Math.abs(x.z).toFixed(1)} SD)</span> · ${r.shots} shots, ${r.minutes} min`;
  };
  el.querySelectorAll(".dev-row").forEach((g) => {
    g.addEventListener("pointerenter", () => show(g));
    g.addEventListener("pointerdown", () => show(g));
  });
}

function refreshPlayerList() {
  $("#pl-list").innerHTML = state.pl.mode === "stats" ? statsBodyHtml() : playerListHtml();
  if (state.pl.mode === "stats" && state.pl.view === "chart") bindDevChart();
}

function statsBodyHtml() {
  return state.pl.view === "chart" ? devChartHtml() : statListHtml();
}

function viewPlayerStats() {
  const lg = teamsLeague();
  const top = `${teamsToggle()}${teamsLeagueChips()}${playersModeToggle()}`;
  if (lg === "E1" || (lg !== "E0" && !psFile(lg) && !XG_LEAGUES.has(lg))) {
    return `${top}<div class="empty">No player stats for ${esc(theLeague(lg))}: Understat, where these come from, doesn't cover it.</div>`;
  }
  if (psFile(lg) && !curPs()) {
    loadPlayerStats(lg);
    return `${top}<div class="empty">Loading player stats…</div>`;
  }
  const ps = curPs();
  if (!ps || !ps.players?.length) {
    return `${top}<div class="empty">${ps?.error ? `Couldn't load player stats (${esc(ps.error)}).` : "Player stats appear after the next update."}</div>`;
  }
  const f = state.pl;
  const season = ps.seasons.includes(f.season) ? f.season : ps.seasons[0];
  const e0 = lg === "E0";
  const teams = [...new Set(ps.players.filter((r) => r.season === season).map((r) => r.team))].sort();
  const opt = (vals, cur, label, fmt = (v) => v) => `${label ? `<option value="">${label}</option>` : ""}${vals.map((v) => `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(fmt(v))}</option>`).join("")}`;
  return `
    ${top}
    <input id="pl-q" class="search" type="search" placeholder="Search players" value="${esc(f.q)}" aria-label="Search players" autocomplete="off">
    <div class="filters">
      <select id="pl-season" aria-label="Season">${opt(ps.seasons, season, "", seasonName)}</select>
      <select id="pl-team" aria-label="Club">${opt(teams, f.team, "All clubs")}</select>
      <select id="pl-pos" aria-label="Position">${opt(["FWD", "MID", "DEF", "GK"], f.pos, "All positions")}</select>
      <select id="pl-ssort" aria-label="Sort">${Object.entries(statSorts()).map(([k, [l]]) => `<option value="${k}" ${k === f.ssort ? "selected" : ""}>${l}</option>`).join("")}</select>
    </div>
    <div class="row-controls">
      ${e0 ? activeToggle() : "<span></span>"}
      <div class="segmented mini" role="group" aria-label="List or chart">
        ${[["list", "List"], ["chart", "Chart"]].map(([k, l]) => `<button data-plview="${k}" class="${f.view === k ? "on" : ""}" aria-pressed="${f.view === k}">${l}</button>`).join("")}
      </div>
    </div>
    ${f.view === "chart" ? `<select id="pl-metric" class="metric" aria-label="Chart measure">${Object.entries(devMetrics()).map(([k, [l]]) => `<option value="${k}" ${k === f.metric ? "selected" : ""}>Chart: ${l}</option>`).join("")}</select>` : ""}
    <div class="card" id="pl-list">${statsBodyHtml()}</div>
    <p class="note">${e0
      ? "Active players = in a current Premier League squad (Fantasy Premier League's list; injured or suspended players still count). Premier League shots from Understat. On target = goals + saved shots. Pick a club to see its totals. Tap a player for his seasons, next match and model record."
      : `${esc(leagueName(lg))} season totals from Understat: games, minutes, shots, goals and xG (no shots on target or starts there). A player who changed clubs in a season shows once, at his last club. Pick a club to see its totals. Player shot predictions cover the Premier League only.`}</p>`;
}

function viewPlayers() {
  if (state.pl.mode === "stats") return viewPlayerStats();
  if (teamsLeague() !== "E0") {
    return `${teamsToggle()}${teamsLeagueChips()}${playersModeToggle()}<div class="empty">The player shot model and its backtest cover the Premier League only. Season stats for ${esc(theLeague(teamsLeague()))} are under Season stats.</div>`;
  }
  const d = state.data;
  if (d.players_stats && !state.psBy.E0) loadPlayerStats("E0");  // for the active-players filter
  if (d.players_backtest && !state.pb) {
    loadPlayersBacktest();
    return `${teamsToggle()}<div class="empty">Loading player results…</div>`;
  }
  const all = playerRows();
  if (!all.length) {
    return `${teamsToggle()}<div class="empty">No player results yet. They appear after the next Player model test run (every Monday, or Actions → Player model).</div>`;
  }
  const teams = [...new Set(all.map((r) => r.team))].sort();
  const f = state.pl;
  const opt = (vals, cur, label, fmt = (v) => v) => `<option value="">${label}</option>${vals.map((v) => `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(fmt(v))}</option>`).join("")}`;
  const pb = state.pb;
  const totals = pb?.players?.length ? (() => {
    const e = pb.players.reduce((a, r) => a + r.exp_shots, 0), a = pb.players.reduce((x, r) => x + r.shots, 0);
    const better = pb.players.filter((r) => r.beats_baseline).length;
    return `<p class="note">Backtest ${esc(pb.seasons || "")}: every player's expected shots, predicted before each match, against what he actually took. Overall ${e.toFixed(0)} expected, ${a} actual. The model beat the season-average baseline for ${better} of ${pb.players.length} players.</p>`;
  })() : `<p class="note">${pb?.error ? `Couldn't load backtest results (${esc(pb.error)}). ` : ""}Showing next-match expected shots only; backtest results appear after the next Player model run.</p>`;
  return `
    ${teamsToggle()}
    ${teamsLeagueChips()}
    ${playersModeToggle()}
    ${totals}
    <input id="pl-q" class="search" type="search" placeholder="Search players" value="${esc(f.q)}" aria-label="Search players" autocomplete="off">
    <div class="filters three">
      <select id="pl-team" aria-label="Team">${opt(teams, f.team, "All teams")}</select>
      <select id="pl-pos" aria-label="Position">${opt(["FWD", "MID", "DEF", "GK"], f.pos, "All positions")}</select>
      <select id="pl-sort" aria-label="Sort">${Object.entries(PL_SORTS).map(([k, [l]]) => `<option value="${k}" ${k === f.sort ? "selected" : ""}>${l}</option>`).join("")}</select>
    </div>
    ${activeToggle()}
    <div class="card" id="pl-list">${playerListHtml()}</div>
    <p class="note">Right column: expected → actual shots over the backtest, and the difference. Tap a player for his match-by-match record.</p>`;
}

function seasonStatsHtml(pid) {
  const rows = (psE0()?.players || []).filter((x) => x.player_id === pid);
  if (!rows.length) return "";
  const body = rows.map((x) => `<tr><td>${esc(seasonName(x.season))}<div class="meta">${esc(x.team)} · ${x.apps} games</div></td><td>${x.shots}</td><td>${per90(x.shots, x.minutes)?.toFixed(2) ?? "–"}</td><td>${x.sot}</td><td>${x.goals}</td><td>${x.xg.toFixed(1)}</td></tr>`).join("");
  return `
    <div class="section-title">Season stats</div>
    <div class="card" style="padding:8px 14px">
      <table><thead><tr><th></th><th>Shots</th><th>/90</th><th>OT</th><th>G</th><th>xG</th></tr></thead><tbody>${body}</tbody></table>
    </div>`;
}

function playerHtml(pid) {
  let r = playerRows().find((x) => x.player_id === pid);
  if (!r) {  // only in the season stats (not in the backtest or next fixtures)
    const st = (psE0()?.players || []).find((x) => x.player_id === pid);
    if (!st) return "";
    r = { player_id: pid, player: st.player, team: st.team, position: st.position, apps: 0 };
  }
  const fields = state.pb?.fields || [];
  const ix = Object.fromEntries(fields.map((f, i) => [f, i]));
  const apps = (state.pb?.apps?.[pid] || []).slice().reverse();
  const rows = apps.map((a) => `<tr><td>${esc(new Date(a[ix.date]).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "2-digit" }))}<div class="meta">${esc(a[ix.opponent])} · ${a[ix.started] ? "started" : "sub"}, ${a[ix.minutes]}′</div></td><td>${a[ix.exp_shots].toFixed(1)}</td><td class="${a[ix.shots] > a[ix.exp_shots] + 0.5 ? "gain" : a[ix.shots] < a[ix.exp_shots] - 0.5 ? "loss" : ""}">${a[ix.shots]}</td><td>${a[ix.exp_sot].toFixed(1)}</td><td>${a[ix.sot]}</td><td>${pct(a[ix.p_1plus])}</td></tr>`).join("");
  const n = r.next;
  const noRecord = state.data.players_backtest && !state.pb ? "Loading his model record…" : "No model backtest record for him yet.";
  return `
    <div class="detail" data-pid="${esc(pid)}">
      <p class="muted" style="margin:0;font-size:13px">${esc(r.team)} · ${esc(r.position || "")}</p>
      <h2 id="sheet-title">${esc(r.player)}</h2>
      ${seasonStatsHtml(pid)}
      ${n ? `<div class="card" style="margin-top:10px"><b>Next: ${n.home ? "v" : "at"} ${esc(n.opp)}</b> <span class="muted small">${esc(kickoffText(n.kickoff))}</span>
        <div class="meta" style="margin-top:4px">${n.exp_shots.toFixed(2)} expected shots · ${n.exp_sot.toFixed(2)} on target · 1+ shot ${pct(n.chances?.["shots_o0.5"])} · 2+ ${pct(n.chances?.["shots_o1.5"])}${n.p_play != null && n.p_play < 1 ? ` · FPL ${pct(n.p_play)} to play` : ""}</div></div>` : ""}
      ${r.apps ? `
      <div class="tiles" style="margin-top:12px">
        <div class="tile"><div class="label">Expected shots</div><div class="value">${r.exp_shots.toFixed(1)}</div><div class="sub">${(r.exp_shots / r.apps).toFixed(2)} a game</div></div>
        <div class="tile"><div class="label">Actual shots</div><div class="value">${r.shots}</div><div class="sub">${(r.shots / r.apps).toFixed(2)} a game</div></div>
        <div class="tile"><div class="label">On target, expected</div><div class="value">${r.exp_sot.toFixed(1)}</div><div class="sub">actual ${r.sot}</div></div>
        <div class="tile"><div class="label">Games</div><div class="value">${r.apps}</div><div class="sub">${r.starts} starts · ${r.minutes} min</div></div>
      </div>
      <p class="note">Model error ${r.ll.toFixed(3)} vs ${r.ll_base.toFixed(3)} for his season average (lower is better): ${r.beats_baseline ? "the model did better" : "the average did better"} for him.</p>
      <div class="section-title">Match by match (predicted before each game)</div>
      <div class="card" style="padding:8px 14px">
        <table><thead><tr><th></th><th>Exp</th><th>Shots</th><th>Exp OT</th><th>OT</th><th>1+</th></tr></thead><tbody>${rows}</tbody></table>
      </div>
      <p class="note">Exp = expected shots, OT = on target, 1+ = the model's chance of at least one shot. Green: more than half a shot above expected; red: more than half a shot below.</p>` : `<p class="note">${noRecord}</p>`}
    </div>`;
}

function viewRatings() {
  if (state.teamsView === "players") return viewPlayers();
  const lg = teamsLeague();
  const block = teamsBlock(lg);
  if (!block?.ratings?.length) return `${teamsToggle()}${teamsLeagueChips()}<div class="empty">No ratings for ${esc(theLeague(lg))} yet.</div>`;
  const r = block.ratings;
  const maxNet = Math.max(...r.map((t) => Math.abs(t.goal_diff)), 0.01);
  const hasXg = r.some((t) => t.xg_for != null);
  const rows = r.map((t, i) => {
    const w = (Math.abs(t.goal_diff) / maxNet) * 50;
    return `
      <div class="rating-row">
        <span class="rank">${i + 1}</span>
        <span>${esc(t.team)}${hasXg && t.xg_for != null ? `<small>xG ${t.xg_for.toFixed(2)} – ${t.xg_against.toFixed(2)} a game</small>` : ""}${lg === "E0" ? absText(t.team) : ""}</span>
        <span class="r">${t.goals_for.toFixed(2)}</span>
        <span class="r">${t.goals_against.toFixed(2)}</span>
        <span class="netbar" title="Net ${signed(t.goal_diff, 2)}"><span class="axis"></span><span class="fill ${t.goal_diff >= 0 ? "pos" : "neg"}" style="width:${w}%"></span></span>
      </div>`;
  }).join("");
  return `
    ${teamsToggle()}
    ${teamsLeagueChips()}
    <p class="note">Goals each team would score and concede per game against an average ${esc(leagueName(lg))} side on a neutral pitch. Recent matches count more.${hasXg ? " The xG line is this season's raw average." : XG_LEAGUES.has(lg) ? "" : ` ${esc(theLeague(lg).replace(/^the/, "The"))} has no xG data (Understat doesn't cover it), so these come from goals only.`}</p>
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

function profitChart(sel, unit = "units") {
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
  chartData = { daily, x, y, W, unit };
  return `
    <div class="chart" id="profit-chart">
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Running profit in ${unit === "$" ? "dollars" : "units"}, ending at ${fmtAmount(cum, unit)}">
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
function fmtAmount(v, unit, d = 1) {
  if (unit !== "$") return `${signed(v, d)} units`;
  return `${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(d === 1 ? 0 : 2)}`;
}
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
    const u = chartData.unit;
    tip.innerHTML = `<b>${new Date(best.date).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}</b><br>${best.n} ${u === "$" ? "trade" : "bet"}${best.n > 1 ? "s" : ""}, ${fmtAmount(best.day, u, 2)}<br>Running total ${fmtAmount(best.cum, u)}`;
    const left = Math.min(Math.max((px / chartData.W) * rect.width, 70), rect.width - 70);
    tip.style.left = `${left}px`;
  };
  const leave = () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); };
  svg.addEventListener("pointermove", move);
  svg.addEventListener("pointerdown", move);
  svg.addEventListener("pointerleave", leave);
}

function recordToggle() {
  return `
    <div class="segmented" role="group" aria-label="Bet type" style="margin-bottom:12px">
      ${[["match", "Match bets"], ["player", "Player shots"], ["markets", "Goals & corners"]].map(([k, l]) => `<button data-recbet="${k}" class="${state.recBet === k ? "on" : ""}" aria-pressed="${state.recBet === k}">${l}</button>`).join("")}
    </div>`;
}

function viewRecord() {
  const body = state.recBet === "player" ? recordPlayerHtml() : state.recBet === "markets" ? recordMarketsHtml() : recordMatchHtml();
  return recordToggle() + body;
}

// ---------- Record → Goals & corners: research results (lab/markets_research.json) ----------
// A range plot per league (dot = estimate, bar = its range, line at 0). Blue when the whole
// range is above 0, red when it is all below, grey when it could be 0. Tap a row to read it.
const rcCharts = {};
function rangeChart(id, rows, fmt) {
  rows = rows.filter((r) => r.v != null && r.lo != null && r.hi != null);
  if (!rows.length) return "";
  rcCharts[id] = rows;
  const vals = rows.flatMap((r) => [r.lo, r.hi]).concat(0);
  const span = Math.max(...vals) - Math.min(...vals) || 1;
  const lo = Math.min(...vals) - span * 0.08, hi = Math.max(...vals) + span * 0.08;
  const x = (v) => ((v - lo) / (hi - lo)) * 100;
  const tone = (r) => (r.lo > 0 ? "pos" : r.hi < 0 ? "neg" : "mid");
  const body = rows.map((r, i) => `
    <button class="eb-row rc-row" data-rcrow="${i}" data-rc="${esc(id)}">
      <span class="eb-label">${esc(r.label)}${r.sub ? `<span class="meta">${esc(r.sub)}</span>` : ""}</span>
      <span class="eb-plot"><span class="rc-zero" style="left:${x(0)}%"></span><span class="eb-range ${tone(r)}" style="left:${x(r.lo)}%;width:${Math.max(0.5, x(r.hi) - x(r.lo))}%"></span><span class="eb-dot real ${tone(r)}" style="left:${x(r.v)}%"></span></span>
    </button>`).join("");
  // Ticks at 0 and near each end, skipping any that would crowd the 0 label.
  const ticks = [lo + (hi - lo) * 0.1, hi - (hi - lo) * 0.1].filter((v) => Math.abs(x(v) - x(0)) > 18);
  const axis = `<div class="eb-axis"><span></span><span class="eb-plot">${ticks.map((v) => `<span style="left:${x(v)}%">${fmt(v)}</span>`).join("")}<span style="left:${x(0)}%">0</span></span></div>`;
  return `<div class="card eb-card">${body}${axis}<div class="eb-readout" id="rc-readout-${esc(id)}" aria-live="polite">${rows[0].readout}</div></div>`;
}

const gainFmt = (v, d = 3) => (v == null ? "–" : `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(d)}`);
const pctPts = (v, d = 1) => (v == null ? "–" : `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(d)}%`);
const runsLine = (runs) => (runs ? Object.entries(runs).map(([lg, id]) => `${esc(leagueShort(lg))} ${esc(id)}`).join(", ") : "");
const rangeMeta = (lo, hi) => (lo == null || hi == null ? "" : `<span class="mk-range">${gainFmt(lo)} to ${gainFmt(hi)}</span>`);
const andList = (xs) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);
const capFirst = (t) => (t ? t[0].toUpperCase() + t.slice(1) : "");
const sourceNote = (part) => `<p class="note small-note">Source: ${esc(part.source || "")}${part.runs ? `. Runs: ${runsLine(part.runs)}` : ""}.</p>`;

function recordMarketsHtml() {
  const mr = state.data.markets_research;
  if (!mr || typeof mr !== "object" || !(mr.total_goals || mr.team_goals || mr.corners)) {
    return `<div class="empty">The research results for goals and corners aren't in this update. They come back with the next site update.</div>`;
  }
  const cards = [marketTotalGoals(mr.total_goals), marketTeamGoals(mr.team_goals), marketCorners(mr.corners)].filter(Boolean).join("");
  return `
    <p class="note">What our tests found for three markets the model can price. Nothing here is a bet: no market has shown an edge yet.${mr.updated ? ` Updated ${esc(mr.updated)}.` : ""}</p>
    ${cards}`;
}

function marketCard(title, verdict, tone, inner, fold) {
  return `
    <div class="section-title">${esc(title)}</div>
    <div class="verdict ${tone}">${esc(verdict || "")}</div>
    ${inner}
    ${fold ? `<details class="fold mk-fold"><summary>Details by league</summary>${fold}</details>` : ""}`;
}

function marketTotalGoals(tg) {
  if (!tg) return "";
  const pin = tg.pinnacle || {}, base = tg.baseline || {};
  const rows = (pin.rows || []).map((r) => ({
    label: leagueShort(r.league), sub: `${r.bets} bets`, v: r.clv_pct, lo: r.clv_lo, hi: r.clv_hi,
    readout: `<b>${esc(leagueName(r.league))}</b>, ${r.matches.toLocaleString()} matches: our picks at a 12% edge got ${pctPts(r.clv_pct)} against Pinnacle's closing price (range ${pctPts(r.clv_lo)} to ${pctPts(r.clv_hi)}), on ${r.bets} bets.`,
  }));
  const chart = rangeChart("tg", rows, (v) => pctPts(v, 0));
  const table = pin.rows?.length ? `
    <div class="card" style="padding:8px 14px"><table class="mkts mk-table">
      <thead><tr><th></th><th>Model</th><th>Pinnacle early</th><th>Bets</th><th>CLV</th></tr></thead>
      <tbody>${pin.rows.map((r) => `<tr><td>${esc(leagueShort(r.league))}</td><td>${r.ll_model.toFixed(4)}</td><td>${r.ll_early.toFixed(4)}</td><td>${r.bets}</td><td class="loss">${pctPts(r.clv_pct)}</td></tr>`).join("")}</tbody>
    </table></div>
    <p class="note">Model and Pinnacle early: average log loss on the over/under 2.5 (lower is better). Pinnacle's early price is better in every league, and the model earns no weight when the two are mixed (its weight's range includes 0 everywhere). ${esc(pin.seasons || "")}, ranges at ${esc(pin.level || "")}.</p>
    ${sourceNote(pin)}` : "";
  const baseTable = base.rows?.length ? `
    <div class="sub-title">Against the league's recent average, 0.5 to 5.5 goals</div>
    <div class="card" style="padding:8px 14px"><table class="mkts mk-table">
      <thead><tr><th></th><th>Lines better</th><th>Which</th></tr></thead>
      <tbody>${base.rows.map((r) => `<tr><td>${esc(leagueShort(r.league))}</td><td>${r.beat} of ${r.of}</td><td>${esc(r.which || "–")}</td></tr>`).join("")}</tbody>
    </table></div>
    <p class="note">The model beats a simple league average at ${base.lines_beating} of ${base.lines} lines, and its chances are too spread out (well calibrated at ${esc(base.calibrated || "")}; slopes ${esc(base.slopes || "")}, where 1 is ideal). ${esc(base.seasons || "")}.</p>
    ${sourceNote(base)}` : "";
  return marketCard("Total goals", tg.verdict, "neg", `
    <p class="note" style="margin-top:4px">Closing line value at Pinnacle, over/under 2.5 goals: how much better or worse our picks' prices were than Pinnacle's last price. Below 0 means the market moved against us.</p>
    ${chart}`, table + baseTable);
}

function marketTeamGoals(tm) {
  if (!tm) return "";
  const res = tm.research || {}, live = tm.live || {};
  const p = live.progress;
  const target = p?.target || live.target || 50;
  let liveHtml;
  if (!p) liveHtml = `<p class="note">The live test's progress isn't in this update.</p>`;
  else if (p.error) liveHtml = `<p class="note">The live test's progress couldn't be counted in this update (${esc(p.error)}); it is checked again on the next one.</p>`;
  else {
    const done = Math.min(1, (p.settled || 0) / target);
    const r = p.result;
    liveHtml = `
      <div class="card">
        <div class="mk-progress-head"><b>${p.settled || 0} of ${target}</b> matches ready to score</div>
        <div class="mk-progress" role="progressbar" aria-valuemin="0" aria-valuemax="${target}" aria-valuenow="${p.settled || 0}"><span style="width:${(done * 100).toFixed(1)}%"></span></div>
        <p class="note" style="margin:8px 0 0">A match is ready when FanDuel's price was logged about a day before kickoff and again just before it, and the result is in. So far: ${p.looks || 0} with the early price, ${p.both || 0} with both prices.</p>
        ${r ? `<p class="note" style="margin:8px 0 0"><b>${r.passes ? "Passes" : "No edge yet"}.</b> Against FanDuel's early price the model's log-loss gain is ${gainFmt(r.gain, 4)}${r.gain_range ? ` (range ${gainFmt(r.gain_range[0], 4)} to ${gainFmt(r.gain_range[1], 4)})` : ""}. At a 12% edge: ${r.bets ?? 0} bets, closing line value ${signedPct(r.clv)}${r.clv_range ? ` (range ${signedPct(r.clv_range[0])} to ${signedPct(r.clv_range[1])})` : ""}. An edge needs both ranges above 0.</p>` : ""}
      </div>`;
  }
  const table = res.rows?.length ? `
    <div class="card" style="padding:8px 14px"><table class="mkts mk-table">
      <thead><tr><th></th><th>Lines better than the average</th></tr></thead>
      <tbody>${res.rows.map((r) => `<tr><td>${esc(leagueShort(r.league))}</td><td class="${r.beat === r.of ? "gain" : r.beat === 0 ? "loss" : ""}">${r.beat} of ${r.of}</td></tr>`).join("")}</tbody>
    </table></div>
    <p class="note">Each team over/under 0.5, 1.5 and 2.5 goals. Gains ${esc(res.gains || "")}. Well calibrated at ${esc(res.calibrated || "")} (slopes ${esc(res.slopes || "")}, 1 is ideal). ${esc(res.e1 || "")} ${esc(res.seasons || "")}.</p>
    ${sourceNote(res)}` : "";
  return marketCard("Each team's goals", tm.verdict, "mid", `
    <p class="note" style="margin-top:4px">In past seasons the model beat the league average at ${res.lines_beating_top5 ?? "–"} of ${res.lines_top5 ?? "–"} lines in the top five leagues. ${esc(live.plan || "")}</p>
    <div class="sub-title">${esc(live.title || "Live test against FanDuel")}</div>
    ${liveHtml}`, table);
}

const bandText = (part) => ((part?.slope_band || [0.8, 1.25]).map((v) => v.toFixed(2)).join("–"));
// Round 11: the 2024/25 bake-off holdout (each team's and total corners), per league.
function cornerHoldoutHtml(ho) {
  if (!ho?.rows?.length) return "";
  const band = bandText(ho);
  return `
    <div class="sub-title">${esc(ho.title || "")}</div>
    <div class="card" style="padding:8px 14px"><table class="mkts mk-table mk-wide">
      <thead><tr><th></th><th>Team gain</th><th>Slopes</th><th>Total gain</th></tr></thead>
      <tbody>${ho.rows.map((r) => `<tr><td>${esc(leagueShort(r.league))}</td><td class="${r.team_lo > 0 ? "gain" : ""}">${gainFmt(r.team_gain)}${rangeMeta(r.team_lo, r.team_hi)}</td><td>${esc(r.team_slopes)}</td><td>${gainFmt(r.total_gain)}${rangeMeta(r.total_lo, r.total_hi)}</td></tr>`).join("")}</tbody>
    </table></div>
    <p class="note">Each team's corners (over 3.5, 4.5 and 5.5): the range clears 0 in ${andList(ho.rows.filter((r) => r.team_lo > 0).map((r) => esc(theLeague(r.league)))) || "no league"}, but every league has a line whose slope is outside ${band}. Total corners (8.5 to 11.5): no league's range clears 0. ${esc(capFirst(ho.seasons || ""))}, ranges at ${esc(ho.level || "")}.</p>
    ${sourceNote(ho)}`;
}
// Round 12: the same models recalibrated, tested on 2025/26, per league.
function cornerRecalHtml(rc) {
  if (!rc?.rows?.length) return "";
  const band = bandText(rc);
  return `
    <div class="sub-title">${esc(rc.title || "")}</div>
    <div class="card" style="padding:8px 14px"><table class="mkts mk-table mk-wide">
      <thead><tr><th></th><th>Gain</th><th>Slopes</th><th>Before</th></tr></thead>
      <tbody>${rc.rows.map((r) => `<tr><td>${esc(leagueShort(r.league))}</td><td class="${r.lo > 0 ? "gain" : ""}">${gainFmt(r.gain)}${rangeMeta(r.lo, r.hi)}</td><td>${esc(r.slopes)}</td><td>${gainFmt(r.raw_gain)}${rangeMeta(r.raw_lo, r.raw_hi)}</td></tr>`).join("")}</tbody>
    </table></div>
    <p class="note">Each team's corners after recalibration (Before = the same model without it). Lines outside ${band}: ${rc.rows.map((r) => `${esc(leagueShort(r.league))} ${esc(r.outside)}`).join("; ")}. ${esc(capFirst(rc.seasons || ""))}, ranges at ${esc(rc.level || "")}.</p>
    ${sourceNote(rc)}`;
}
// Round 13: corners bake-off 2, model (f) in development: the model the live test trades.
function cornerDevChart(dev, id) {
  const rows = (dev?.rows || []).map((r) => ({
    label: leagueShort(r.league), sub: r.finalist ? "finalist" : "not in band", v: r.gain, lo: r.lo, hi: r.hi,
    readout: `<b>${esc(leagueName(r.league))}</b>, ${r.matches.toLocaleString()} matches: model (f) gain ${gainFmt(r.gain, 4)} a line (range ${gainFmt(r.lo, 4)} to ${gainFmt(r.hi, 4)}); line slopes ${esc(r.slopes)}. ${r.passes ? "Every line in the slope band: a finalist for the February test." : "At least one line outside the slope band: no February test in this league."}`,
  }));
  return rangeChart(id, rows, (v) => gainFmt(v, 2));
}
function cornerDevHtml(dev) {
  if (!dev?.rows?.length) return "";
  const band = bandText(dev);
  return `
    <div class="card" style="padding:8px 14px"><table class="mkts mk-table mk-wide">
      <thead><tr><th></th><th>Gain</th><th>Slopes</th><th>Result</th></tr></thead>
      <tbody>${dev.rows.map((r) => `<tr><td>${esc(leagueShort(r.league))}</td><td class="${r.lo > 0 ? "gain" : ""}">${gainFmt(r.gain, 4)}${rangeMeta(r.lo, r.hi)}</td><td>${esc(r.slopes)}</td><td class="${r.passes ? "gain" : ""}">${r.passes ? "Passes: finalist" : "Too spread out"}</td></tr>`).join("")}</tbody>
    </table></div>
    <p class="note">Gain = how much better model (f)'s chances score than the league average, log loss a line (above 0 is better). A model passes only if the gain's range is above 0 and every line's slope is in ${band} (1 means its chances are spread just right). ${esc(dev.seasons || "")}, ranges at ${esc(dev.level || "")}. ${esc(dev.test || "")}</p>
    ${sourceNote(dev)}`;
}

function marketCorners(co) {
  if (!co) return "";
  const ho = co.holdout || {}, rc = co.recalibration || {};
  const band = bandText(rc.slope_band ? rc : ho);
  const rows = (rc.rows || []).map((r) => ({
    label: leagueShort(r.league), sub: `${r.matches} matches`, v: r.gain, lo: r.lo, hi: r.hi,
    readout: `<b>${esc(leagueName(r.league))}</b>, 2025/26, recalibrated: gain ${gainFmt(r.gain)} a line (range ${gainFmt(r.lo)} to ${gainFmt(r.hi)}); line slopes ${esc(r.slopes)}. Outside ${band}: ${esc(r.outside || "none")}.`,
  }));
  const chart = rangeChart("co", rows, (v) => gainFmt(v, 2));
  const live = pfById("corners");
  const link = live ? `<div class="explain live-test"><b>Now in a live test.</b> The owner is paper-trading model (f), a newer team-corner model, against Pinnacle's live prices in every competition. <button class="linkish" data-goto-pf="corners">Open the ${esc(live.name)} portfolio</button></div>` : "";
  return marketCard("Corners", co.verdict, "neg", `
    ${link}
    <p class="note" style="margin-top:4px">Each team's corners, 2025/26 test after recalibration: how much better the model's chances scored than the league average (log loss a line; above 0 is better). Blue = the whole range is above 0. A model also needs every line's slope between ${band} (1 means its chances are spread just right), and none managed it, so nothing passes.</p>
    ${chart}`, (co.development?.rows?.length ? `<div class="sub-title">${esc(co.development.title || "")}</div>${cornerDevHtml(co.development)}` : "") + cornerHoldoutHtml(ho) + cornerRecalHtml(rc));
}

// Portfolio → Team corners → Backtest: the research record (kind "research"). No corner
// prices exist historically, so there is no money here: verdict, model (f) per league and
// the earlier tests, folded.
function researchBacktestHtml(bt) {
  const dev = bt.development;
  return `
    <div class="verdict mid" style="margin-top:12px">${esc(bt.verdict || "")}</div>
    <p class="note">${esc(bt.note || "")}</p>
    ${dev?.rows?.length ? `
      <div class="section-title">${esc(dev.title || "Model (f), development")}</div>
      <p class="note" style="margin-top:4px">Model (f) = ${esc(dev.model_name || "")}. Its gain over the league average, per league; tap a row for its numbers.</p>
      ${cornerDevChart(dev, "pfco")}
      ${cornerDevHtml(dev)}` : ""}
    ${bt.holdout || bt.recalibration ? `
      <details class="fold mk-fold"><summary>Earlier corner tests <span class="muted">(rounds 11 and 12)</span></summary>
        ${cornerHoldoutHtml(bt.holdout)}${cornerRecalHtml(bt.recalibration)}
      </details>` : ""}
    <p class="note">No money tiles: football-data has no historical corner prices, so this strategy can't be replayed for profit. The live paper trades are its only money record.</p>`;
}

const STRATEGY_LABEL = {
  blend_lineup: "Starters, after lineups",
  blend_3h: "Blend, 3 h before",
  raw_3h: "Raw model, 3 h before",
};
// Short names for the strategy switch and table headers (fit three across a phone).
const STRATEGY_SHORT = { blend_lineup: "After lineups", blend_3h: "3 h, blend", raw_3h: "3 h, raw" };
const STRATEGY_ORDER = ["blend_lineup", "blend_3h", "raw_3h"];
const STRATEGY_NOTE = {
  blend_lineup: "Confirmed starters only, at FanDuel's last price before kickoff (after lineups are out), using the model's chance blended with FanDuel's price. This is the main rule.",
  blend_3h: "Everyone priced 3 hours before kickoff (lineups not yet known), using the blended chance.",
  raw_3h: "The original rule: the raw model's chance, 3 hours before kickoff. Kept for comparison.",
};
const countWord = (market, n) => (market === "player_shots" ? `${n} shot${n === 1 ? "" : "s"}` : `${n} on target`);

// The priced player backtest: FanDuel player shot lines, strategies side by side.
function recordPlayerHtml() {
  const pf = state.data.portfolio || {};
  const pm = pf.player_model || {};
  const pr = pm.priced;
  if (!pr || !pr.strategies || !Object.keys(pr.strategies).length) {
    return `<div class="empty">No priced player backtest yet. It appears after the Player model run prices FanDuel's historical lines.</div>${playerModelHtml(pm)}`;
  }
  const strats = Object.keys(pr.strategies).sort((a, b) => {
    const ia = STRATEGY_ORDER.indexOf(a), ib = STRATEGY_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  const main = strats.includes(pr.main_strategy) ? pr.main_strategy : strats[0];
  const cur = strats.includes(state.recStrat) ? state.recStrat : main;
  const st = pr.strategies[cur];
  const short = (k) => STRATEGY_SHORT[k] || STRATEGY_LABEL[k] || k;
  // Bet-by-bet trades exist for the main strategy only (E0_players.json "trades").
  const trades = (pfById("player_shots")?.backtest?.trades || [])
    .filter((t) => t.bet_type === "player" && (t.status === "won" || t.status === "lost"))
    .sort((a, b) => a.kickoff.localeCompare(b.kickoff));
  const s = st.summary || {};
  const seasons = [...new Set(trades.map((t) => t.season))].sort();
  const span = seasons.length ? `${seasonName(seasons[0])}${seasons.length > 1 ? `–${seasonName(seasons[seasons.length - 1])}` : ""}` : "";
  // Threshold sweep: one row per edge threshold, one column per strategy.
  const ths = [...new Set(strats.flatMap((k) => Object.keys(pr.strategies[k].sweep || {})))]
    .sort((a, b) => parseFloat(a) - parseFloat(b));
  const sweepRows = ths.map((th) => `<tr><td>${esc(th)}+</td>${strats.map((k) => {
    const x = pr.strategies[k].sweep?.[th];
    return `<td class="${k === cur ? "on-col" : ""}">${x?.trades ? `<span class="${plClass(x.roi)}">${signedPct(x.roi)}</span><div class="meta">${x.trades} bets</div>` : "–"}</td>`;
  }).join("")}</tr>`).join("");
  const cal = pr.calibration?.[st.snapshot === "after lineups" ? "close" : "look"]?.table || [];
  const calRows = cal.map((r) => `<tr><td>${esc(r.bucket.replace(/%-/, "–"))}<div class="meta">${r.lines.toLocaleString()} lines</div></td><td>${pct(r.implied, 1)}</td><td>${pct(r.model, 1)}</td><td>${pct(r.blend, 1)}</td><td><b>${pct(r.won, 1)}</b></td></tr>`).join("");
  const sel = trades.map((t) => [t.kickoff.slice(0, 10), t.home, t.away, t.market, t.odds, t.edge, 0, t.profit]);
  const recent = trades.slice(-25).reverse().map((t) => `
    <button class="bet-row trade" data-trade="${esc(t.id)}">
      <span>${esc(t.player)} · ${esc(lineLabel(t))}<div class="meta">${shortDate(t.kickoff)} · ${esc(t.home)} v ${esc(t.away)} · ${american(t.odds)} (${odds(t.odds)})</div><div class="meta">Chance ${pct(t.model_p, 0)} vs ${esc(playerBook())} ${pct(t.implied, 0)} · had ${countWord(t.market, t.actual)}</div></span>
      <span class="pl ${t.profit > 0 ? "win" : "loss"}">${usd(t.profit)}</span>
    </button>`).join("");
  const isMain = cur === main;
  return `
    <div class="section-title" style="margin-top:4px">Minimum edge</div>
    ${edgePanel("player_shots")}
    ${edgeBucketsHtml("player_shots")}
    <p class="note">The player rule replayed on ${esc(playerBook())}'s historical player shot lines${span ? ` (${esc(span)})` : ""}: $10 per bet, best line per player and market, at most ${MAX_PLAYER_TRADES} per match, with the model's chances computed only from matches before each bet.</p>
    <div class="segmented small-seg" role="group" aria-label="Strategy" style="margin:10px 0 6px">
      ${strats.map((k) => `<button data-recstrat="${esc(k)}" class="${k === cur ? "on" : ""}" aria-pressed="${k === cur}">${esc(short(k))}</button>`).join("")}
    </div>
    <p class="note"><b>${esc(STRATEGY_LABEL[cur] || cur)}.</b> ${esc(STRATEGY_NOTE[cur] || "")}</p>
    <div class="tiles" style="margin-top:12px">
      <div class="tile"><div class="label">Profit</div><div class="value ${plClass(s.profit)}">${usd(s.profit)}</div><div class="sub">from ${s.trades || 0} bets at 12%+ edge</div></div>
      <div class="tile"><div class="label">Return per bet</div><div class="value">${signedPct(s.roi)}</div><div class="sub">${s.roi_ci95 ? `95%: ${signedPct(s.roi_ci95[0], 0)} to ${signedPct(s.roi_ci95[1], 0)}` : "ROI"}</div></div>
      <div class="tile"><div class="label">Won</div><div class="value">${pct(s.win_rate, 1)}</div><div class="sub">${pct(s.breakeven, 1)} needed to break even</div></div>
      <div class="tile"><div class="label">Beat the close</div><div class="value">${s.beat_close_dk != null ? pct(s.beat_close_dk) : "–"}</div><div class="sub">${s.clv_dk != null ? `avg price move ${signedPct(s.clv_dk)}` : "bets placed at the close"}</div></div>
    </div>
    ${isMain ? `<div class="section-title">Running profit ($)</div><div class="card">${profitChart(sel, "$") || '<p class="muted">No bets.</p>'}</div>` : ""}
    <div class="section-title">Return per bet by edge threshold</div>
    <div class="card" style="padding:8px 14px">
      <table class="sweep"><thead><tr><th>Edge</th>${strats.map((k) => `<th class="${k === cur ? "on-col" : ""}">${esc(short(k))}</th>`).join("")}</tr></thead><tbody>${sweepRows}</tbody></table>
    </div>
    <p class="note">Each cell: return per bet, and how many bets, when only bets with at least that edge are placed. A real edge should hold up, or improve, as the threshold rises.</p>
    ${calRows ? `<div class="section-title">Predicted vs actual win rate</div>
    <div class="card" style="padding:8px 14px">
      <table><thead><tr><th>Group</th><th>${esc(playerBook())}</th><th>Model</th><th>Blend</th><th>Won</th></tr></thead><tbody>${calRows}</tbody></table>
    </div>
    <p class="note">Every priced line ${st.snapshot === "after lineups" ? "at the last price before kickoff" : "3 hours before kickoff"}, grouped by ${esc(playerBook())}'s chance (1 / odds, so it includes their margin). The column closest to "Won" is the best calibrated; bets use the blend.</p>` : ""}
    ${isMain && recent ? `<div class="section-title">Latest bets</div><div class="card">${recent}</div>` : ""}
    ${isMain ? "" : `<p class="note">The running profit and bet-by-bet list are kept for the main strategy (${esc(STRATEGY_LABEL[main] || main)}) only.</p>`}
    ${playerModelHtml(pm)}`;
}

// Match bets against DraftKings' historical prices (E0_dk.json "strategies"), styled like
// the player shots view: a strategy switch, its tiles, then a sweep with one column each.
const DK_LABEL = { raw: "Model alone", blend: "Model + DraftKings blend" };
const DK_SHORT = { raw: "Model alone", blend: "Blend" };
const DK_NOTE = {
  raw: "The model's own chances against DraftKings' price. This is what the app used before the blend.",
  blend: "The model mixed with DraftKings' margin-free price, the mix refitted every 4 weeks on earlier matches only. This is what value picks use now.",
};
// Backtest seasons as start years ("2025", "2023-2025") to "2025/26", "2023/24–2025/26".
const startYears = (v) => String(v).split(/[-,]/).map((y) => y.trim()).filter(Boolean)
  .map((y) => `${y}/${String((Number(y) + 1) % 100).padStart(2, "0")}`).filter((_, i, a) => i === 0 || i === a.length - 1).join("–");
function recordDkHtml() {
  const bt = pfById("moneyline")?.backtest;
  const strategies = bt?.strategies;
  if (!strategies || !Object.keys(strategies).length) return "";
  const strats = Object.keys(strategies).sort((a, b) => (a === "raw" ? -1 : b === "raw" ? 1 : a.localeCompare(b)));
  const cur = strats.includes(state.recDk) ? state.recDk : strats.includes("blend") ? "blend" : strats[0];
  const short = (k) => DK_SHORT[k] || strategies[k].label || k;
  const s = strategies[cur].summary || {};
  const th = bt.threshold ?? PAPER_EDGE;
  // Sweep rows keyed by threshold, uncapped first, then with odds capped (max_odds).
  const key = (r) => `${r.threshold}|${r.max_odds ?? ""}`;
  const rowKeys = [];
  for (const k of strats) for (const r of strategies[k].sweep || []) if (!rowKeys.includes(key(r))) rowKeys.push(key(r));
  rowKeys.sort((a, b) => {
    const [ta, ca] = a.split("|"), [tb, cb] = b.split("|");
    return (ca ? 1 : 0) - (cb ? 1 : 0) || parseFloat(ta) - parseFloat(tb);
  });
  const cell = (k, rk) => {
    const x = (strategies[k].sweep || []).find((r) => key(r) === rk);
    return `<td class="${k === cur ? "on-col" : ""}">${x?.trades ? `<span class="${plClass(x.roi)}">${signedPct(x.roi)}</span><div class="meta">${x.trades} bets</div>` : x ? '<span class="muted">0 bets</span>' : "–"}</td>`;
  };
  let capHead = false;
  const sweepRows = rowKeys.map((rk) => {
    const [t, cap] = rk.split("|");
    const sub = cap && !capHead ? `<tr class="sub-head"><td colspan="${strats.length + 1}">Odds capped at ${Number(cap).toFixed(1)}</td></tr>` : "";
    if (cap) capHead = true;
    return `${sub}<tr><td>${pct(Number(t))}+</td>${strats.map((k) => cell(k, rk)).join("")}</tr>`;
  }).join("");
  const ll = bt.log_loss || {};
  const llRows = [["Model alone", ll.model_on_blend ?? ll.model], ["Blend", ll.blend], ["DraftKings", ll.draftkings_on_blend ?? ll.draftkings]]
    .filter(([, v]) => v != null);
  const best = Math.min(...llRows.map(([, v]) => v));
  const noBets = !s.trades;
  return `
    <div class="section-title" style="margin-top:4px">Against DraftKings' prices</div>
    <p class="note" style="margin-top:0">$10 bets at DraftKings' historical prices${bt.seasons ? ` (${esc(startYears(bt.seasons))})` : ""}, one per match on the best edge, with chances computed only from earlier matches.</p>
    <div class="segmented small-seg" role="group" aria-label="DraftKings strategy" style="margin:10px 0 6px">
      ${strats.map((k) => `<button data-recdk="${esc(k)}" class="${k === cur ? "on" : ""}" aria-pressed="${k === cur}">${esc(short(k))}</button>`).join("")}
    </div>
    <p class="note"><b>${esc(DK_LABEL[cur] || strategies[cur].label || cur)}.</b> ${esc(DK_NOTE[cur] || "")}</p>
    ${noBets ? `<div class="explain"><b>No bets at a ${pct(th)} edge.</b> ${cur === "blend" ? "The blend stays so close to DraftKings' own chance that it never clears their margin, so it doesn't bet. That is the result: the model adds little that DraftKings' price doesn't already know." : "Nothing reached the threshold."}</div>` : `
    <div class="tiles" style="margin-top:12px">
      <div class="tile"><div class="label">Profit</div><div class="value ${plClass(s.profit)}">${usd(s.profit)}</div><div class="sub">from ${s.trades} bets at ${pct(th)}+ edge</div></div>
      <div class="tile"><div class="label">Return per bet</div><div class="value">${signedPct(s.roi)}</div><div class="sub">${s.roi_ci95 ? `95%: ${signedPct(s.roi_ci95[0], 0)} to ${signedPct(s.roi_ci95[1], 0)}` : "ROI"}</div></div>
      <div class="tile"><div class="label">Won</div><div class="value">${pct(s.win_rate, 1)}</div><div class="sub">${pct(s.breakeven, 1)} needed to break even</div></div>
      <div class="tile"><div class="label">Beat the close</div><div class="value">${pct(s.beat_close_dk)}</div><div class="sub">avg price move ${signedPct(s.clv_dk)}</div></div>
    </div>`}
    <div class="section-title">Return per bet by edge threshold</div>
    <div class="card" style="padding:8px 14px">
      <table class="sweep"><thead><tr><th>Edge</th>${strats.map((k) => `<th class="${k === cur ? "on-col" : ""}">${esc(short(k))}</th>`).join("")}</tr></thead><tbody>${sweepRows}</tbody></table>
    </div>
    <p class="note">Each cell: return per bet, and how many bets, when only bets with at least that edge are placed. A real edge should hold up, or improve, as the threshold rises.</p>
    ${llRows.length > 1 ? `<div class="section-title">Forecast error</div>
    <div class="card" style="padding:8px 14px">
      <table><thead><tr><th></th><th>Log loss</th><th>vs DraftKings</th></tr></thead><tbody>${llRows.map(([n, v]) => `<tr><td>${v === best ? `<b>${n}</b>` : n}</td><td>${v.toFixed(4)}</td><td>${n === "DraftKings" ? "–" : signed(v - (ll.draftkings_on_blend ?? ll.draftkings), 4)}</td></tr>`).join("")}</tbody></table>
    </div>
    <p class="note">Home/draw/away forecasts on ${(ll.blend_matches ?? ll.matches) || "the"} matches; lower is better. Most accurate: ${llRows.find(([, v]) => v === best)[0]}. Bet by bet: Portfolio → Backtest.</p>` : ""}
    <div class="section-title" style="margin-top:22px">Longer replay at Pinnacle odds</div>`;
}

// The DraftKings strategy comparison and the Pinnacle replay (data.record) are Premier
// League numbers; another competition's backtest bets are listed in Portfolio → Backtest.
function recordMatchHtml() {
  const d = state.data;
  const lg = leagueOn();
  const edgeTop = `
    ${leagueFilter()}
    <div class="section-title" style="margin-top:4px">Minimum edge${lg ? ` · ${esc(leagueName(lg))}` : ""}</div>
    ${edgePanel("moneyline", lg)}
    ${edgeBucketsHtml("moneyline", lg)}`;
  if (lg && lg !== "E0") {
    const n = (pfById("moneyline")?.backtest?.trades || []).filter((t) => tLeague(t) === lg).length;
    return `${edgeTop}<p class="note">The DraftKings strategy comparison and the week-by-week model replay cover the Premier League only: choose Premier or All to see them.${n ? ` ${esc(leagueName(lg))}'s ${n} backtest bet${n > 1 ? "s are" : " is"} in Portfolio → Backtest.` : ""}</p>`;
  }
  const rec = d.record.model;
  if (!rec || !rec.matches) return `${edgeTop}<div class="empty">Not enough data to replay yet.</div>`;
  const replayEdge = matchEdge("E0") ?? PAPER_EDGE; // no recommended level: replay the paper rule
  const s = summarize(rec.bets, replayEdge);
  const gap = rec.log_loss - rec.market_log_loss;
  const since = new Date(d.record_start).toLocaleDateString(undefined, { month: "long", year: "numeric" });

  let compare = "";
  if (d.record.goals_only) {
    const g = d.record.goals_only;
    const sg = summarize(g.bets, replayEdge);
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

  const recent = s.sel.slice(-10).reverse().map((b) => `
    <div class="bet-row">
      <span>${esc(b[1])} v ${esc(b[2])}<div class="meta">${new Date(b[0]).toLocaleDateString(undefined, { day: "numeric", month: "short" })} · ${esc(PICK_LABEL[b[3]] || b[3])} @ ${odds(b[4])} · edge ${signedPct(b[5])}</div></span>
      <span class="pl ${b[7] > 0 ? "win" : ""}">${b[7] > 0 ? "Won " : "Lost "}${signed(b[7], 2)}</span>
    </div>`).join("");

  return `
    ${edgeTop}
    ${recordDkHtml()}
    <p class="note">The model replayed week by week since ${esc(since)}, using only data it would have had before each match. 1-unit bets at the historical opening odds in football-data's files (mostly Pinnacle)${isDK() ? "; past DraftKings prices aren't available, so DraftKings' bigger margin would make real results somewhat worse" : ""}.</p>
    <p class="note">${multiLeague() ? "Premier League matches, r" : "R"}eplayed at ${pct(replayEdge)}+ edge${matchEdge("E0") == null ? ", the paper-trade rule, since no level is recommended" : ""}.</p>
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

// ---------- portfolio (paper trades and the DraftKings backtest) ----------
// Signed dollars; an amount that rounds to zero shows as "$0" with no sign (never "−$0.00").
const usd = (v, d = 0) => {
  if (v == null) return "–";
  const body = `$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })}`;
  return Math.abs(v) < 0.5 * 10 ** -d ? body : `${v > 0 ? "+" : "−"}${body}`;
};
const seasonName = (s) => (s && s.length === 4 ? `20${s.slice(0, 2)}/${s.slice(2)}` : s || "–");
const shortDate = (iso) => new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "2-digit" });
const kickoffText = (iso) => new Date(iso).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

// The per-strategy portfolios (data.json → portfolio.portfolios). An older data.json
// without them is shown as one Moneyline portfolio built from its live and backtest.
function portfolios() {
  const pf = state.data.portfolio || {};
  if (pf.portfolios?.length) return pf.portfolios;
  return [{ id: "moneyline", name: "Moneyline", status: "live", note: "", live: pf.live, backtest: pf.backtest }];
}
const pfById = (id) => portfolios().find((p) => p.id === id);
// The portfolio on screen: the remembered one, else the first that isn't retired.
function pfCurrent() {
  const all = portfolios();
  return all.find((p) => p.id === state.pfId) || all.find((p) => p.status !== "retired") || all[0];
}
function pfSet() {
  const p = pfCurrent();
  return p ? p[state.pfView === "backtest" ? "backtest" : "live"] : null;
}
// Every trade in every portfolio, live and backtest (for the trade sheet).
const allTrades = () => portfolios().flatMap((p) => [...(p.live?.trades || []), ...(p.backtest?.trades || [])]);
const PF_STATUS = { live: "Live", testing: "In testing", retired: "Retired" };

// Current DraftKings price for an open trade: the latest price in data.json, else the
// last one the ledger saw, else the entry price.
function currentPrice(t) {
  const fx = state.data.fixtures.find((f) => f.home === t.home && f.away === t.away);
  let cur = null;
  if (isCorner(t)) {
    const q = fx?.corners?.pinnacle?.[t.team_side]?.find((x) => Number(x.line) === Number(t.line));
    cur = q ? q[t.side] : null;
  } else if (t.bet_type === "player") {
    const pl = fx?.players?.find((p) => p.player_id === t.player_id);
    cur = pl?.lines?.find((l) => l.market === t.market && l.line === t.line && l.side === t.side)?.odds ?? null;
  } else {
    cur = fx?.odds?.[t.market] ?? null;
  }
  return { cur: cur ?? t.close_odds ?? t.odds, live: cur != null, fx };
}
// What an open bet is worth if cashed out at the current price (ignoring the
// bookmaker's cash-out margin): stake x entry odds / current odds.
const markToMarket = (t, cur) => t.stake * t.odds / cur - t.stake;
// When a match trade's closing price was taken, in minutes before kickoff:
// close_minutes_before when published, else worked out from close_fetched_at.
const CLOSE_APPROX_MIN = 60;
function closeMinutes(t) {
  if (t.close_minutes_before != null) return t.close_minutes_before;
  if (!t.close_fetched_at || !t.kickoff) return null;
  const m = (Date.parse(t.kickoff) - Date.parse(t.close_fetched_at)) / 60000;
  return Number.isFinite(m) && m >= 0 ? Math.round(m) : null;
}
const closeApprox = (t) => (closeMinutes(t) ?? 0) > CLOSE_APPROX_MIN;
const minutesText = (m) => (m == null ? "–" : m >= 120 ? `${(m / 60).toFixed(1)} h` : `${Math.round(m)} min`);
const priceBoth = (d) => (d ? `${american(d)} (${d.toFixed(2)})` : "–");
// One meta line for a settled match trade: closing price, when, CLV ("–" when missing).
function closeLine(t) {
  if (t.status === "open" || isPlayer(t)) return "";
  const m = closeMinutes(t);
  const clv = tradeClv(t);
  return `<div class="meta">${isCorner(t) ? "Pinnacle's close" : "Close"} ${priceBoth(t.close_odds)}${m != null ? ` · ${minutesText(m)} before` : ""} · <span class="nowrap">CLV <span class="${plClass(clv)}">${signedPct(clv)}</span></span>${closeApprox(t) ? ' · <span class="approx">approx.</span>' : ""}</div>`;
}
const plClass = (v) => (v == null || Math.abs(v) < 0.005 ? "" : v > 0 ? "gain" : "loss");

function openPL(trades) {
  const open = trades.filter((t) => t.status === "open");
  return {
    n: open.length,
    atRisk: open.reduce((a, t) => a + t.stake, 0),
    pl: open.reduce((a, t) => a + markToMarket(t, currentPrice(t).cur), 0),
  };
}

function pfTiles(s, trades = []) {
  const se = s.roi_se != null ? ` ± ${(s.roi_se * 100).toFixed(1)}%` : "";
  const o = openPL(trades);
  const total = (s.profit || 0) + o.pl;
  const openTiles = o.n ? `
      <div class="tile"><div class="label">Open bets, if cashed out</div><div class="value ${plClass(o.pl)}">${usd(o.pl, 2)}</div><div class="sub">$${o.atRisk} at risk on ${o.n} bet${o.n > 1 ? "s" : ""}</div></div>
      <div class="tile"><div class="label">Total gain/loss</div><div class="value ${plClass(total)}">${usd(total, 2)}</div><div class="sub">settled + open</div></div>` : "";
  const profit = `<div class="tile"><div class="label">Settled profit</div><div class="value ${plClass(s.profit)}">${usd(s.profit)}</div><div class="sub">${s.staked ? `on $${s.staked.toLocaleString()} staked` : "nothing settled yet"}</div></div>`;
  const roi = `<div class="tile"><div class="label">ROI</div><div class="value">${signedPct(s.roi)}</div><div class="sub">${se ? `standard error${se}` : "per dollar staked"}</div></div>`;
  const corners = trades.length > 0 && trades.every(isCorner);
  const book = corners ? "Pinnacle's" : trades.length && trades.every(isPlayer) ? `${playerBook()}'s` : "DraftKings'";
  const sClv = corners ? s.clv_pinnacle : s.clv_dk;
  const sBeat = corners ? s.beat_close_pinnacle : s.beat_close_dk;
  const clv = `<div class="tile"><div class="label">Closing line value</div><div class="value ${plClass(sClv)}">${signedPct(sClv)}</div><div class="sub">avg vs ${esc(book)} close</div></div>`;
  const beat = `<div class="tile"><div class="label">Beat the close</div><div class="value">${pct(sBeat)}</div><div class="sub">${corners ? "vs Pinnacle's close" : s.clv_pinnacle != null ? `Pinnacle CLV ${signedPct(s.clv_pinnacle)}` : "share of settled trades"}</div></div>`;
  const count = `<div class="tile"><div class="label">Trades</div><div class="value">${s.trades || 0}</div><div class="sub">${s.open || 0} open · ${s.settled || 0} settled${s.void ? ` · ${s.void} void` : ""}</div></div>`;
  const dd = `<div class="tile"><div class="label">Max drawdown</div><div class="value">${s.max_drawdown != null ? `$${s.max_drawdown.toFixed(0)}` : "–"}</div><div class="sub">peak to trough</div></div>`;
  return state.pfView === "live"
    ? `<div class="tiles" style="margin-top:12px">${profit}${roi}${clv}${beat}</div>
    ${clvNote(s, trades)}
    <div class="tiles" style="margin-top:12px">${openTiles}${count}${dd}</div>`
    : `<div class="tiles" style="margin-top:12px">${openTiles}${count}${profit}${roi}${clv}${beat}${dd}</div>`;
}

// Live view: why CLV matters, and how many closes were taken early (approximate CLV).
function clvNote(s, trades) {
  if (trades.length && trades.every(isCorner)) {
    return `<p class="note">Closing line value compares the price you got with Pinnacle's last team-corner price before kickoff, margin removed. Until that close is logged, a trade's close is its entry price, so its CLV is just Pinnacle's margin (slightly negative). Profit takes hundreds of bets to tell skill from luck; beating Pinnacle's close shows up much sooner.</p>`;
  }
  const match = trades.filter((t) => !isPlayer(t) && t.status !== "open");
  if (!trades.some((t) => !isPlayer(t))) return "";
  const early = s.close_early ?? s.close_over_60min ?? match.filter(closeApprox).length; // either name: moneyline uses close_early
  return `<p class="note">Closing line value compares the price you got with DraftKings' last price before kickoff. Profit takes hundreds of bets to tell skill from luck; consistently beating the close shows up within a few dozen, so it is the faster, more reliable sign of an edge.${early ? ` ${early} close${early > 1 ? "s were" : " was"} taken more than an hour before kickoff, so ${early > 1 ? "their" : "its"} CLV is approximate.` : ""}</p>`;
}

function pfOpen(trades) {
  const open = trades.filter((t) => t.status === "open");
  if (!open.length) return "";
  const rows = open.map((t) => {
    const { cur, live, fx } = currentPrice(t);
    let curEdge = null;
    if (isCorner(t)) {
      const q = fx?.corners?.pinnacle?.[t.team_side]?.find((x) => Number(x.line) === Number(t.line));
      const pw = q?.[t.side === "over" ? "p_over" : "p_under"];
      if (pw != null && live) curEdge = pw * cur + (q.p_push || 0) - 1;
    } else {
      const p = t.bet_type === "player" ? null : fx?.p?.[t.market];
      curEdge = p != null ? p * cur - 1 : null;
    }
    const mtm = markToMarket(t, cur);
    const move = cur < t.odds ? "shortened" : cur > t.odds ? "drifted" : "unchanged";
    return `
      <button class="bet-row trade" data-trade="${esc(t.id)}">
        <span>${tradeTitle(t)}<div class="meta">${esc(kickoffText(t.kickoff))} · ${t.bet_type === "player" || isCorner(t) ? `${esc(t.home)} v ${esc(t.away)}` : esc(tradeLabel(t))} · $${t.stake} @ ${american(t.odds)}</div><div class="meta">Now ${american(cur)} (${move}${live ? "" : ", last seen"})${curEdge != null ? ` · edge now ${signedPct(curEdge)}` : ""}</div></span>
        <span class="pl ${plClass(mtm)}">${usd(mtm, 2)}<div class="meta">if cashed out</div></span>
      </button>`;
  }).join("");
  return `<div class="section-title">Open positions</div><div class="card">${rows}</div>
    <p class="note">Gain/loss on open bets is what each would be worth at the current price (${open.every(isCorner) ? "Pinnacle's latest logged team-corner price" : `DraftKings for match bets, ${esc(playerBook())} for player shots`}: stake × entry odds ÷ current odds), before any cash-out fee. A shortened price means the bet gained value. Bets settle after the final whistle.</p>`;
}

function pfSettled(trades) {
  const done = trades.filter((t) => t.status !== "open");
  if (!done.length) return "";
  const markets = [...new Set(done.map((t) => t.market))];
  const seasons = [...new Set(done.map((t) => t.season))].sort().reverse();
  const sel = done.filter((t) => (!state.pfMarket || t.market === state.pfMarket) && (!state.pfSeason || t.season === state.pfSeason));
  const shown = sel.slice(0, state.pfShown);
  const rows = shown.map((t) => `
    <button class="bet-row trade" data-trade="${esc(t.id)}">
      <span>${tradeTitle(t)}<div class="meta">${esc(shortDate(t.kickoff))} · ${t.bet_type === "player" || isCorner(t) ? `${esc(t.home)} v ${esc(t.away)}` : esc(tradeLabel(t))} @ ${american(t.odds)} · edge ${signedPct(t.edge)}${t.score ? ` · ${esc(t.score)}` : ""}${t.actual != null ? ` · had ${isCorner(t) ? `${t.actual} corners` : countWord(t.market, t.actual)}` : ""}</div>${closeLine(t)}</span>
      <span class="pl ${t.profit > 0 ? "win" : t.profit < 0 ? "loss" : ""}">${t.status === "void" ? (t.push ? "Push<div class=\"meta\">stake back</div>" : "Void") : `${t.status === "won" ? "Won" : "Lost"} ${usd(t.profit, 0)}`}</span>
    </button>`).join("");
  const opt = (vals, cur, label, fmt) => `<option value="">${label}</option>${vals.map((v) => `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(fmt(v))}</option>`).join("")}`;
  return `
    <div class="section-title">Settled trades</div>
    <div class="filters">
      <select id="pf-market" aria-label="Filter by market">${opt(markets, state.pfMarket, "All markets", marketName)}</select>
      <select id="pf-season" aria-label="Filter by season">${opt(seasons, state.pfSeason, "All seasons", seasonName)}</select>
    </div>
    <div class="card">${rows || '<p class="muted">No settled trades match these filters.</p>'}</div>
    ${sel.length > shown.length ? `<button class="more" id="pf-more">Show more (${sel.length - shown.length} left)</button>` : ""}`;
}

const marketName = (m) => (m === "team_corners" ? "Team corners" : PICK_LABEL[m] || (PLAYER_MARKET[m] ? `Player ${PLAYER_MARKET[m]}` : m));
function pfTable(title, rows, label = (g) => g) {
  if (!rows || !rows.length) return "";
  // Range groups ("under 2.0", "2.0–3.5", "over 6.0", "12–15%") in numeric order.
  const lo = (g) => (/^under/i.test(g) ? -Infinity : /^over/i.test(g) ? Infinity : parseFloat(g));
  if (rows.every((r) => !Number.isNaN(lo(String(r.group))))) rows = rows.slice().sort((a, b) => lo(String(a.group)) - lo(String(b.group)));
  const body = rows.map((r) => `<tr><td>${esc(label(r.group))}</td><td>${r.trades}</td><td>${signedPct(r.roi)}</td><td>${signedPct(pfCurrent()?.id === "corners" ? r.clv_pinnacle : r.clv_dk)}</td></tr>`).join("");
  return `
    <div class="section-title">${esc(title)}</div>
    <div class="card" style="padding:8px 14px">
      <table class="bd"><thead><tr><th></th><th>Trades</th><th>ROI</th><th>CLV</th></tr></thead><tbody>${body}</tbody></table>
    </div>`;
}

function pfBacktestExtras(bt) {
  let html = "";
  if (bt.coverage?.length) {
    const body = bt.coverage.map((c) => `<tr><td>${esc(seasonName(c.season))}</td><td>${c.priced} of ${c.matches}</td><td>${pct(c.share)}</td></tr>`).join("");
    html += `
      <div class="section-title">DraftKings coverage</div>
      <div class="card" style="padding:8px 14px">
        <table><thead><tr><th>Season</th><th>Priced</th><th>Share</th></tr></thead><tbody>${body}</tbody></table>
      </div>`;
  }
  if (bt.log_loss?.matches) {
    html += `<p class="note">Forecast error (log loss, lower is better) on ${bt.log_loss.matches} matches: model ${bt.log_loss.model.toFixed(4)}${bt.log_loss.blend != null ? `, blend ${bt.log_loss.blend.toFixed(4)}` : ""}, DraftKings ${bt.log_loss.draftkings.toFixed(4)}. The threshold sweep and the model-vs-blend comparison are on Record → Match bets.</p>`;
  }
  return html;
}

// The breakdown tables, folded away so the phone view stays short.
function pfBreakdowns(b) {
  const backtest = state.pfView === "backtest";
  const tables = [
    pfTable("By market", b.market, marketName),
    pfTable("By line", b.line, (g) => (Number.isInteger(Number(g)) && pfCurrent()?.id !== "corners" ? `${Number(g)}+` : `${g}`)),
    pfTable("By position", b.position),
    pfTable("Starter or substitute", b.started, (g) => (g === "True" || g === "true" ? "Started" : "Came on")),
    pfTable("By edge at entry", b.edge_bucket),
    pfTable("By odds", b.odds_bucket),
    backtest ? pfTable("By season", b.season, seasonName) : "",
    backtest ? pfTable("By look", b.look, (g) => `${g} before kickoff`) : "",
  ].filter(Boolean);
  if (!tables.length) return "";
  return `
    <details class="fold">
      <summary>Breakdowns <span class="muted">(${tables.length} tables: market, odds, edge${backtest ? ", season" : ""}…)</span></summary>
      ${tables.join("")}
    </details>`;
}

// A section narrowed to one competition: its trades, and its summary and breakdowns from
// by_league (or, without one, a count only: older data is Premier League only).
function leagueSet(set, lg) {
  const trades = (set.trades || []).filter((t) => tLeague(t) === lg);
  const part = set.by_league?.[lg];
  const fallback = trades.length === (set.trades || []).length ? { summary: set.summary, breakdowns: set.breakdowns } : { summary: { trades: trades.length }, breakdowns: {} };
  return { ...set, trades, ...(part || fallback) };
}

// A card's pick badge. Under the live test, a match whose paper trade is already open on
// another outcome says "Would pick now": one trade per match, so nothing new opens.
const pickBadge = (pick, t) => (!fixedRule() ? "Value" : t && PICK_LABEL[t.market] !== PICK_LABEL[pick.market] ? "Would pick now" : "Live-test pick");
const isMoneylineFixed = (pf) => pf?.id === "moneyline" && fixedRule();
function viewPortfolio() {
  const pf = state.data.portfolio || {};
  const rule = pf.rule || { threshold: PAPER_EDGE, stake: 10 };
  const all = portfolios();
  const cur = pfCurrent();
  const active = all.filter((p) => p.status !== "retired");
  const retired = all.filter((p) => p.status === "retired" && p.id !== cur.id);
  const switcher = active.length > 1 || cur.status === "retired" ? `
    <div class="segmented" role="group" aria-label="Portfolio">
      ${active.map((p) => `<button data-pfid="${esc(p.id)}" class="${p.id === cur.id ? "on" : ""}" aria-pressed="${p.id === cur.id}">${esc(p.name)}</button>`).join("")}
    </div>` : "";
  const head = `
    <div class="pf-head">
      <span class="pill st-${esc(cur.status)}">${esc(PF_STATUS[cur.status] || cur.status)}</span>
      <span>${cur.status === "retired" ? `<b>${esc(cur.name)}</b>, read-only history. ` : ""}${esc(cur.note || "")}</span>
    </div>`;
  const retiredLinks = retired.length ? `
    <div class="pf-retired">Retired: ${retired.map((p) => `<button class="linkish" data-pfid="${esc(p.id)}">${esc(p.name)}</button>`).join(", ")}</div>` : "";
  const toggle = `
    <div class="segmented small-seg" role="group" aria-label="Which trades">
      ${[["live", "Live paper"], ["backtest", "Backtest"]].map(([k, l]) => `<button data-pf="${k}" class="${state.pfView === k ? "on" : ""}" aria-pressed="${state.pfView === k}">${l}</button>`).join("")}
    </div>`;
  const fullSet = pfSet();
  // Moneyline and Team corners: filtered by competition (by_league summaries from paper.py).
  const byComp = cur.id === "moneyline" || cur.id === "corners";
  const research = state.pfView === "backtest" && fullSet?.kind === "research";
  const lg = byComp && !research ? leagueOn() : "";
  const set = fullSet && lg ? leagueSet(fullSet, lg) : fullSet;
  const top = `${switcher}${head}${byComp && !research ? leagueFilter() : ""}${toggle}`;
  if (research) return `${top}${researchBacktestHtml(fullSet)}${retiredLinks}`;
  const isMoneyline = cur.id === "moneyline";
  const ruleNote = cur.id === "corners" && state.pfView === "live"
    ? `<div class="explain live-test"><b>Live test: fixed ${pct(set?.rule?.threshold ?? PAPER_EDGE)} on model (f) against Pinnacle.</b> A $${set?.rule?.stake ?? 10} paper trade opens on a team's corner line when the model's chance shows at least ${aPct(set?.rule?.threshold ?? PAPER_EDGE)} edge against Pinnacle's price, at most one per team and line, in every competition. On a whole line (say 5), exactly 5 corners is a push: the stake comes back.</div>`
    : state.pfView === "live" && cur.status === "live"
    ? (isMoneylineFixed(cur)
      ? `<div class="explain live-test"><b>Your live test: fixed ${pct(rule.threshold ?? PAPER_EDGE)} on the model alone.</b> A $${rule.stake ?? 10} paper trade opens on every match where the model's own chance (not blended with the market) shows at least ${aPct(rule.threshold ?? PAPER_EDGE)} edge against DraftKings' quoted price, in every competition. The Matches tab flags exactly these.</div>`
      : rule.threshold == null ? "" : `<p class="note">A $${rule.stake ?? 10} paper trade opens whenever a pick reaches a ${pct(rule.threshold)} edge, the same minimum the Matches tab flags with.</p>`) : "";
  const foot = `<p class="note">${state.pfView === "live"
    ? (isMoneyline ? oddsAge() || "Odds: DraftKings." : cur.id === "corners" ? "Prices: Pinnacle's team corners, logged about a day before kickoff and again just before it." : "")
    : isMoneyline ? `Historical DraftKings odds from The Odds API${set?.generated_at ? `, run ${shortDate(set.generated_at)}` : ""}. Probabilities without team news (its history starts Oct 2026).`
      : set?.generated_at ? `Backtest run ${shortDate(set.generated_at)}.` : ""} Paper trades: no money is staked.</p>`.replace('class="note"> ', 'class="note">');
  const trades = set?.trades || [];
  const s = set?.summary || { trades: 0 };
  const banner = set?.error ? `<div class="banner">${esc(set.error)}</div>` : set?.note && cur.status === "live" ? `<p class="note">${esc(set.note)}</p>` : "";
  if (!trades.length) {
    const empty = cur.id === "corners" && state.pfView === "live"
      ? "No corner trades yet. One opens when a team's corner line shows a 12% edge against Pinnacle's price."
      : cur.status === "testing"
      ? "No trades yet. They start once this strategy passes its tests."
      : cur.status === "retired" && state.pfView === "live"
        ? "No live trades: this strategy was retired before any opened. Its testing history is under Backtest."
        : state.pfView === "live"
          ? (rule.threshold == null ? "No new paper trades: no minimum edge has beaten the market in past bets." : `No paper trades yet. One opens when a pick reaches a ${pct(rule.threshold)} edge.`)
          : set ? "The backtest found no trades at this threshold." : "No backtest yet.";
    return `${top}${ruleNote}${banner}<div class="empty">${empty}</div>${state.pfView === "backtest" && set ? pfBacktestExtras(set) : ""}${retiredLinks}${foot}`;
  }
  const settled = trades.filter((t) => t.status === "won" || t.status === "lost").slice().reverse()
    .map((t) => [t.kickoff.slice(0, 10), t.home, t.away, t.market, t.odds, t.edge, tradeClv(t), t.profit]);
  const ci = s.roi_ci95 ? `<p class="note">95% interval on ROI: ${signedPct(s.roi_ci95[0])} to ${signedPct(s.roi_ci95[1])} (resampling match weeks). Win rate ${pct(s.win_rate, 1)} vs ${pct(s.breakeven, 1)} needed to break even.</p>` : "";
  return `
    ${top}
    ${ruleNote}
    ${banner}
    ${pfTiles(s, trades)}
    ${ci}
    <div class="section-title">Running profit ($)</div>
    <div class="card">${profitChart(settled, "$") || '<p class="muted">Nothing settled yet.</p>'}</div>
    ${state.pfView === "live" ? pfOpen(trades) : ""}
    ${impliedVsRealizedHtml(trades)}
    ${pfSettled(trades)}
    ${pfBreakdowns(set.breakdowns || {})}
    ${state.pfView === "backtest" ? edgeBucketsHtml(cur.id, lg) + pfBacktestExtras(set) : ""}
    ${retiredLinks}
    ${foot}`;
}

// Who prices the player shot lines (FanDuel; match odds come from DraftKings).
const playerBook = () => state.data?.players_status?.bookmaker || "FanDuel";
const playerBookShort = () => (playerBook() === "FanDuel" ? "FD" : playerBook());
// trades.PLAYER_PAPER_TRADES, published as players_status.paper_trades.
const playerTradesOff = () => state.data?.players_status?.paper_trades === false;

// Player trades: the bookmaker's implied chance and the model's chance against what happened.
function impliedVsRealizedHtml(trades) {
  const done = trades.filter((t) => t.bet_type === "player" && (t.status === "won" || t.status === "lost"));
  if (!done.length) return "";
  const withImp = done.filter((t) => t.implied != null);
  const avg = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
  const hit = avg(done.map((t) => (t.status === "won" ? 1 : 0)));
  // Same groups as the calibration table on Record → Player shots (most lines are long shots).
  const buckets = [[0, 0.05], [0.05, 0.1], [0.1, 0.2], [0.2, 0.35], [0.35, 0.5], [0.5, 0.7], [0.7, 1.01]];
  const rows = buckets.map(([lo, hi]) => {
    const g = withImp.filter((t) => t.implied >= lo && t.implied < hi);
    if (!g.length) return "";
    const pl = g.reduce((a, t) => a + t.profit, 0), st = g.reduce((a, t) => a + t.stake, 0);
    return `<tr><td>${Math.round(lo * 100)}–${Math.min(100, Math.round(hi * 100))}%</td><td>${g.length}</td><td>${pct(avg(g.map((t) => t.implied)))}</td><td>${pct(avg(g.map((t) => t.model_p)))}</td><td>${pct(avg(g.map((t) => (t.status === "won" ? 1 : 0))))}</td><td class="${plClass(pl)}">${signedPct(pl / st)}</td></tr>`;
  }).join("");
  const byLine = {};
  for (const t of done) {
    const k = `${lineText(t.side, t.line)} ${t.market === "player_shots" ? "shots" : "on target"}`;
    (byLine[k] ||= []).push(t);
  }
  const lineRows = Object.entries(byLine).sort((a, b) => b[1].length - a[1].length).map(([k, g]) => {
    const pl = g.reduce((a, t) => a + t.profit, 0), st = g.reduce((a, t) => a + t.stake, 0);
    return `<tr><td>${esc(k[0].toUpperCase() + k.slice(1))}</td><td>${g.length}</td><td>${avg(g.map((t) => t.actual))?.toFixed(2) ?? "–"}</td><td>${pct(avg(g.map((t) => (t.status === "won" ? 1 : 0))))}</td><td class="${plClass(pl)}">${usd(pl, 0)}</td></tr>`;
  }).join("");
  return `
    <div class="section-title">Implied vs realized (player trades)</div>
    <p class="note" style="margin-top:0">At entry, ${playerBook()}'s price implied a ${pct(avg(withImp.map((t) => t.implied)))} chance on average (1 / odds for over-only lines, so their margin is included) and the model said ${pct(avg(done.map((t) => t.model_p)))}. The bets actually won ${pct(hit)} of the time.</p>
    <div class="card" style="padding:8px 14px">
      <table><thead><tr><th>${playerBook()} implied</th><th>Bets</th><th>Book</th><th>Model</th><th>Won</th><th>ROI</th></tr></thead><tbody>${rows}</tbody></table>
    </div>
    <p class="note">Grouped by ${playerBook()}'s implied chance. If "Won" tracks the book more closely than the model, the market was right and the model's edge wasn't real.</p>
    <div class="card" style="padding:8px 14px">
      <table><thead><tr><th>Bet</th><th>Bets</th><th>Avg had</th><th>Won</th><th>Profit</th></tr></thead><tbody>${lineRows}</tbody></table>
    </div>
    <p class="note">Avg had = the shots (or shots on target) the players actually recorded on those bets, to compare with the line.</p>`;
}

function playerModelHtml(pm) {
  if (!pm || !pm.before_lineups) return "";
  const g = pm.gate || {};
  const row = (name, sc, c) => `<tr><td>${name}</td><td>${sc[c].model.toFixed(4)}</td><td>${sc[c].baseline.toFixed(4)}</td><td>${sc[c].beats_baseline ? "Yes" : "No"}</td></tr>`;
  const ab = (pm.ablation?.groups || []).map((r) => `<tr><td>${esc(r.without)}</td><td>${r.log_loss.toFixed(4)}</td><td>${signed(r.change, 4)}</td><td>${r.kept ? "Kept" : "Dropped"}</td></tr>`).join("");
  const tt = pm.team_totals;
  return `
    <div class="section-title">Player shot model (no odds)</div>
    <div class="card" style="padding:8px 14px">
      <table><thead><tr><th></th><th>Model</th><th>Baseline</th><th>Better</th></tr></thead><tbody>
        ${row("Shots, before lineups", pm.before_lineups, "shots")}${row("On target, before lineups", pm.before_lineups, "sot")}
        ${pm.lineup_known ? row("Shots, lineup known", pm.lineup_known, "shots") + row("On target, lineup known", pm.lineup_known, "sot") : ""}
      </tbody></table>
    </div>
    <p class="note">Log loss of the chance of over 0.5, 1.5 and 2.5, lower is better; the baseline is each player's season average. ${g.passed ? `The model beats it for both, so ${playerBook()} player lines and player paper trades are on.` :"Player odds and trades stay off until the model beats the baseline for both."}${tt?.ratio_to_expected ? ` Team check: players' expected shots add up to ${(tt.ratio_to_expected * 100).toFixed(0)}% of the team's (tolerance ±${(tt.tolerance * 100).toFixed(0)}%).` : ""}</p>
    ${ab ? `<div class="section-title">Factor groups (ablation)</div>
    <div class="card" style="padding:8px 14px"><table><thead><tr><th>Without</th><th>Log loss</th><th>Change</th><th></th></tr></thead><tbody>${ab}</tbody></table></div>
    <p class="note">A group stays only if removing it makes predictions worse. On-target method: ${esc(pm.sot_method === "count" ? "its own count model" : "a share of his shots")}.</p>` : ""}`;
}

// Trade sheet rows: [label, value, which bets ("match", "player" or both when omitted)].
// A label may be a function of the trade.
const isPlayer = (t) => t.bet_type === "player";
const hoursText = (h) => (h >= 48 ? `${(h / 24).toFixed(1)} days` : h >= 1 ? `${h.toFixed(1)} h` : `${Math.max(1, Math.round(h * 60))} min`);
const TRADE_FIELDS = [
  ["Match", (t) => `${t.home} v ${t.away}`],
  ["Kickoff", (t) => kickoffText(t.kickoff)],
  ["Pick", (t) => tradeLabel(t)],
  ["Player", (t) => `${t.player} (${t.team || "?"}${t.position ? `, ${t.position}` : ""})`, "player"],
  ["Result", (t) => (t.actual != null ? `${countWord(t.market, t.actual)}${t.started != null ? (t.started ? ", started" : ", came on") : ""}` : "–"), "player"],
  ["Result", (t) => (t.actual != null ? `${t.team} had ${t.actual} corners${t.push ? ": exactly the line, a push (stake back)" : ""}` : "–"), "corners"],
  ["Model's expected corners", (t) => (t.model_mean != null ? `${t.team} ${Number(t.model_mean).toFixed(1)}` : "–"), "corners"],
  ["Entry odds", (t) => `${american(t.odds)} (${t.odds.toFixed(2)})`],
  ["Odds fetched", (t) => (t.odds_fetched_at ? kickoffText(t.odds_fetched_at) : "–")],
  ["Opened", (t) => `${kickoffText(t.opened_at)} (${hoursText(t.hours_to_kickoff)} before kickoff)`],
  [(t) => (isPlayer(t) && t.model_p_base != null && Math.abs(t.model_p_base - t.model_p) > 1e-4 ? "Chance used (blend)" : "Model chance"), (t) => pct(t.model_p, 1)],
  ["Raw model chance", (t) => pct(t.model_p_base, 1), (t) => isPlayer(t) && t.model_p_base != null && Math.abs(t.model_p_base - t.model_p) > 1e-4],
  ["Push chance", (t) => pct(t.p_push, 1), (t) => isCorner(t) && t.p_push > 0],
  ["Without team news", (t) => pct(t.model_p_base, 1), "match"],
  ["Team news applied", (t) => (t.news_applied ? "Yes" : "No"), "match"],
  ["Edge at entry", (t) => signedPct(t.edge)],
  ["Threshold", (t) => pct(t.threshold)],
  ["Rule", (t) => (isCorner(t) ? "Fixed edge, model (f) vs Pinnacle (live test)" : t.rule === "fixed_raw" ? "Fixed edge, model alone (live test)" : "Learned minimum, blended chance"), (t) => !isPlayer(t) && !!t.rule],
  ["Stake", (t) => `$${t.stake}`],
  [(t) => (t.status === "open" ? "Latest odds" : "Closing odds"), (t) => priceBoth(t.close_odds)],
  [(t) => (t.status === "open" ? "Latest price taken" : "Close taken"), (t) => { const m = closeMinutes(t); return m == null ? "–" : `${minutesText(m)} before kickoff${closeApprox(t) ? " (approximate close)" : ""}`; }, (t) => !isPlayer(t)],
  ["Bookmaker", (t) => ({ pinnacle: "Pinnacle", draftkings: "DraftKings", fanduel: "FanDuel" })[t.bookmaker] || t.bookmaker || (isPlayer(t) ? "FanDuel" : "DraftKings")],
  ["Book implied chance", (t) => (t.implied != null ? pct(t.implied, 1) : "–")],
  [(t) => (isPlayer(t) ? "Price move to close" : t.status === "open" ? "CLV so far" : isCorner(t) ? "CLV vs Pinnacle's close" : "CLV vs DraftKings"), (t) => signedPct(tradeClv(t))],
  ["Beat the close", (t) => { const b = tradeBeat(t), c = tradeClv(t); return b == null ? (c == null || t.status === "open" ? "–" : c > 0 ? "Yes" : "No") : b ? "Yes" : "No"; }, (t) => !isPlayer(t)],
  ["CLV vs Pinnacle", (t) => signedPct(t.clv_pinnacle), "match"],
  ["Status", (t) => t.status[0].toUpperCase() + t.status.slice(1)],
  ["Score", (t) => t.score || "–", (t) => !isPlayer(t)],
  ["Profit", (t) => (t.profit == null ? "–" : usd(t.profit, 2))],
  ["Season", (t) => seasonName(t.season)],
  ["Look", (t) => (isCorner(t) ? ({ look: "about a day before kickoff", close: "just before kickoff" })[t.look] || "–" : t.look === "lineup" ? "after lineups" : t.look ? `${t.look} before kickoff` : "–")],
  ["Model", (t) => [t.model_ref?.commit, t.model_ref?.xg_weight != null ? `xG weight ${t.model_ref.xg_weight}` : null, t.model_ref?.matches_fit ? `${t.model_ref.matches_fit} matches` : null].filter(Boolean).join(" · ") || "–"],
  ["Trade id", (t) => t.id],
];

function tradeHtml(t) {
  const kind = isPlayer(t) ? "player" : isCorner(t) ? "corners" : "match";
  const rows = TRADE_FIELDS.filter(([, , only]) => (typeof only === "function" ? only(t) : !only || only === kind))
    .map(([k, f]) => `<tr><td>${esc(typeof k === "function" ? k(t) : k)}</td><td>${esc(f(t))}</td></tr>`).join("");
  return `
    <div class="detail">
      <p class="muted" style="margin:0;font-size:13px">${t.source === "live" ? "Live paper trade" : "Backtest trade"}${isPlayer(t) || isCorner(t) ? ` · ${esc(t.home)} v ${esc(t.away)}` : ""}</p>
      <h2 id="sheet-title">${tradeTitle(t)}</h2>
      <div class="card" style="padding:8px 14px;margin-top:10px">
        <table class="kv"><tbody>${rows}</tbody></table>
      </div>
    </div>`;
}

const VIEWS = { matches: viewMatches, ratings: viewRatings, record: viewRecord, portfolio: viewPortfolio, explore: viewExplore };

function render() {
  chartData = null;
  devData = null;
  $("#view").innerHTML = VIEWS[state.tab]();
  document.querySelectorAll(".tabbar button").forEach((b) => {
    const on = b.dataset.tab === state.tab;
    b.classList.toggle("active", on);
    b.setAttribute("aria-current", on ? "page" : "false");
  });
  bindChart();
  bindDevChart();
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
  // One league: its name; several: how many (the app covers more than the Premier League).
  const n = leaguesPresent().length;
  $("#updated").textContent = `${n > 1 ? `${n} leagues ·` : d.league} ${d.season} · updated ${ago}`;
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
    state.exploreEdge = t.dataset.edge === "reset" ? null : Number(t.dataset.edge);
    render();
  } else if (t.dataset.pf) {
    state.pfView = t.dataset.pf; state.pfShown = 15; state.pfMarket = ""; state.pfSeason = "";
    render();
  } else if (t.dataset.recbet) {
    state.recBet = t.dataset.recbet;
    render();
  } else if (t.dataset.ebrow !== undefined) {
    const el = document.getElementById(`eb-readout-${t.dataset.eb}`);
    const src = edgeChartSource(t.dataset.eb, t.dataset.eb === "moneyline" ? leagueOn() : "");
    if (el && src) el.innerHTML = ebReadout(src.et, Number(t.dataset.ebrow));
    t.parentElement.querySelectorAll(".eb-row").forEach((r) => r.classList.toggle("sel", r === t));
  } else if (t.dataset.gotoPf) {
    state.pfId = t.dataset.gotoPf; state.pfView = "live"; state.pfShown = 15; state.pfMarket = ""; state.pfSeason = "";
    store.set("pfId", state.pfId);
    setTab("portfolio");
  } else if (t.dataset.rcrow !== undefined) {
    const rows = rcCharts[t.dataset.rc];
    const el = document.getElementById(`rc-readout-${t.dataset.rc}`);
    if (rows && el) el.innerHTML = rows[Number(t.dataset.rcrow)]?.readout || "";
    t.parentElement.querySelectorAll(".rc-row").forEach((r) => r.classList.toggle("sel", r === t));
  } else if (t.dataset.recdk) {
    state.recDk = t.dataset.recdk;
    render();
  } else if (t.dataset.recstrat) {
    state.recStrat = t.dataset.recstrat;
    render();
  } else if (t.dataset.league !== undefined) {
    state.league = t.dataset.league; state.pfShown = 15; state.pfMarket = ""; state.pfSeason = "";
    state.pl.team = ""; state.pl.season = ""; state.pl.shown = 40; // clubs and seasons differ by league
    store.set("league", state.league);
    render();
  } else if (t.dataset.pfid) {
    state.pfId = t.dataset.pfid; state.pfShown = 15; state.pfMarket = ""; state.pfSeason = "";
    store.set("pfId", state.pfId);
    render();
    window.scrollTo(0, 0);
  } else if (t.dataset.plview) {
    state.pl.view = t.dataset.plview;
    render();
  } else if (t.id === "pl-active") {
    state.pl.active = !state.pl.active; state.pl.shown = 40;
    render();
  } else if (t.dataset.plmode) {
    state.pl.mode = t.dataset.plmode; state.pl.shown = 40;
    render();
  } else if (t.dataset.tv) {
    state.teamsView = t.dataset.tv;
    render();
  } else if (t.dataset.player !== undefined) {
    const pid = t.dataset.player;
    openSheet(playerHtml(pid));
    // Opened from Season stats: the model record (players_backtest.json) may not be loaded yet.
    if (state.data.players_backtest && !state.pb) {
      loadPlayersBacktest().then(() => {
        if (!$("#sheet").hidden && $("#sheet-body [data-pid]")?.dataset.pid === pid) $("#sheet-body").innerHTML = playerHtml(pid);
      });
    }
  } else if (t.id === "pl-more") {
    state.pl.shown += 40;
    refreshPlayerList();
  } else if (t.id === "pf-more") {
    state.pfShown += 25;
    render();
  } else if (t.dataset.trade !== undefined) {
    const tr = [...(pfSet()?.trades || []), ...allTrades()].find((x) => x.id === t.dataset.trade);
    if (tr) openSheet(tradeHtml(tr));
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
  if (ev.target.id === "pl-season") {  // clubs differ by season: re-render the filters too
    state.pl.season = ev.target.value; state.pl.team = ""; state.pl.shown = 40;
    render();
  } else if (ev.target.id === "pl-metric") {
    state.pl.metric = ev.target.value;
    refreshPlayerList();
  } else if (["pl-team", "pl-pos", "pl-sort", "pl-ssort"].includes(ev.target.id)) {
    state.pl[ev.target.id.slice(3)] = ev.target.value; state.pl.shown = 40;
    refreshPlayerList();
  }
  if (ev.target.id === "pf-market") { state.pfMarket = ev.target.value; state.pfShown = 15; render(); }
  if (ev.target.id === "pf-season") { state.pfSeason = ev.target.value; state.pfShown = 15; render(); }
});
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeSheet(); });
document.addEventListener("input", (ev) => {
  if (ev.target.id === "pl-q") {  // update the list only, so the search box keeps focus
    state.pl.q = ev.target.value; state.pl.shown = 40;
    refreshPlayerList();
  }
});

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
