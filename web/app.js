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
  pfView: "live", // Portfolio: "live" paper trades, "backtest" or "model"
  pfMarket: "",
  pfSeason: "",
  pfShown: 15,
  pfId: store.get("pfId", "moneyline"), // Portfolio tab: which strategy's portfolio
  pfLevel: "home", // Portfolio tab: "home" (the folders), "portfolio" (its strategies) or "strategy"
  pfStrat: store.get("pfStrat", ""), // Moneyline live: which match strategy ("" = the first, Blend Lean)
  pfStratCorners: store.get("pfStratCorners", ""), // Team corners: which book's strategy ("" = the first, Pinnacle)
  pfDriver: "", // Blend Lean: "" = every pick, "market" or "model" (live trades and backtest bets)
  lbShown: 20, // Blend Lean backtest: bet rows shown
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
// The app's version, kept in step with sw.js's cache name (soccer-model-vNN); shown in the
// header and compared with the deployed sw.js on ↻.
const APP_VERSION = "v61";
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

// ---------- Blend Lean (trades.lean_pick): the second Moneyline strategy, listed first ----------
// Cards carry `z` per home/draw/away = (p_bet − 1/DraftKings' decimal) / σ and `lean_pick`
// (publish.add_lean; null on low-data cards); σ per league is in data.match_blends: how far
// the blend usually strays from the market there. Lean z ≥ 1, Strong z ≥ 2. Match trades
// carry `strategy` ("lean" or "edge12"; missing = the 12% test), one trade per strategy.
const stratOf = (t) => t.strategy || "edge12";
const leanOn = () => (state.data?.portfolio?.strategy_order || []).includes("lean");
const leanRule = (lg) => leagueRule(lg)?.lean || null;
const leanSigma = (lg) => state.data?.match_blends?.leagues?.[lg || "E0"]?.sigma ?? leanRule(lg)?.sigma ?? null;
// No pick once the match has kicked off: paper trades only open before kickoff.
const leanPick = (fx) => (leanOn() && !fx.low_data && kickoffPhase(fx.kickoff) === "upcoming" ? fx.lean_pick || null : null);
// Blend Lean's odds range (owner, 24 Oct; trades.LEAN_ODDS_RANGE): a pick outside it opens no
// trade. `in_range` false on the card's pick; the range and its start from the Lean rule.
const leanOut = (lp) => lp?.in_range === false;
const rangeWords = (r) => (r ? `${american(r[0])} to ${american(r[1])}` : "");
function leanRangeInfo(lg = "") {
  const r = leanRule(lg) || (pfById("moneyline")?.live?.strategies || []).find((x) => x.key === "lean")?.rule || {};
  return r.odds_range ? { range: r.odds_range, words: rangeWords(r.odds_range), from: r.odds_range_from, note: r.odds_range_note } : null;
}
const sigmaText = (z) => (z == null ? "–" : `${z < 0 ? "−" : z > 0 ? "+" : ""}${Math.abs(z).toFixed(1)}σ`);
const ptsText = (x) => `${(x * 100).toFixed(1)} points`;
const tierName = (pick) => (pick.tier === "strong" ? "Strong" : "Lean");
// What carried a Lean pick (trades.lean_driver): "market" = it still clears 1σ with the
// model's term taken out of the blend (z_market), "model" = only with it. Unknown = null.
const DRIVER_NAME = { market: "Market-driven", model: "Model-driven", unknown: "Driver unknown" };
const driverOf = (x) => (x?.driver === "market" || x?.driver === "model" ? x.driver : null);
const driverChip = (x) => { const d = driverOf(x); return d ? `<span class="drv drv-${d}">${d === "model" ? "Model" : "Market"}</span>` : ""; };
const driverNote = () => (pfById("moneyline")?.live?.strategies || []).find((x) => x.key === "lean")?.driver_note || state.data?.portfolio?.strategy_backtests?.lean?.rule?.driver_note || "";
// Lead review (PR #43): most Lean picks come from the blend's price weight b > 1 (fitted on
// Pinnacle's closes, it sharpens DraftKings' price: favourites shorter, long shots longer),
// not from the model; with b capped at 1 only 2 of the 12 matches that reached 1σ from
// 1 to 10 Oct still would. Say so wherever a Lean pick is shown.
const LEAN_CAVEAT = "Lean picks lean mostly on the part of the blend that sharpens the market's own price (favourites a little likelier, long shots less), not on the model, and they haven't proven themselves.";
// Open Moneyline match trades by match and strategy (a match can hold one of each).
const tradeKey = (home, away, strat) => `${home}|${away}|${strat}`;
function openMatchTrades() {
  const open = (pfById("moneyline")?.live?.trades || []).filter((t) => t.status === "open" && t.bet_type !== "player" && !isCorner(t));
  return new Map(open.map((t) => [tradeKey(t.home, t.away, stratOf(t)), t]));
}
const openTradeFor = (fx, strat) => openMatchTrades().get(tradeKey(fx.home, fx.away, strat));

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
// Team corners run one paper strategy per book (Pinnacle first, DraftKings from 20 Oct):
// a trade's own `strategy`, else its `bookmaker`, else Pinnacle (trades.corner_strategy_of).
const CORNER_BOOKS = ["pinnacle", "draftkings"];
const BOOK_NAME = { pinnacle: "Pinnacle", draftkings: "DraftKings" };
const BOOK_SHORT = { pinnacle: "PIN", draftkings: "DK" };
const BOOK_POSS = { pinnacle: "Pinnacle's", draftkings: "DraftKings'" };
const cornerBook = (t) => ((t.strategy || t.bookmaker) === "draftkings" ? "draftkings" : "pinnacle");
const isDkCorner = (t) => isCorner(t) && cornerBook(t) === "draftkings";
// Closing line value: a corner trade against its own book's close (DraftKings' is None when
// that close was one-sided), match bets against DraftKings'.
const tradeClv = (t) => (isCorner(t) && !isDkCorner(t) ? t.clv_pinnacle : t.clv_dk);
const tradeBeat = (t) => (isCorner(t) && !isDkCorner(t) ? t.beat_close_pinnacle : t.beat_close_dk);
const closeWho = (t) => (isCorner(t) ? `${BOOK_POSS[cornerBook(t)]} close` : "Close");
// Where an unsettled trade stands, from its kickoff and the clock: "upcoming" (kickoff
// ahead), "inplay" (kicked off under IN_PLAY_MIN ago) or "awaiting" (the match is over and
// the result hasn't reached the ledger yet; results land some hours after full time).
// Settled trades are "settled". Prices after kickoff aren't logged, so once a match starts
// the trade's last logged price is its close and its CLV is final.
const IN_PLAY_MIN = 135; // about 2 h 15 min: 90 minutes, half-time and stoppage time
const minsSinceKickoff = (iso) => { const k = Date.parse(iso); return Number.isFinite(k) ? (Date.now() - k) / 60000 : null; };
function kickoffPhase(iso) {
  const m = minsSinceKickoff(iso);
  return m == null || m < 0 ? "upcoming" : m < IN_PLAY_MIN ? "inplay" : "finished";
}
function tradePhase(t) {
  if (t.status !== "open") return "settled";
  const k = kickoffPhase(t.kickoff);
  return k === "finished" ? "awaiting" : k;
}
const PHASE_LABEL = { upcoming: "Upcoming", inplay: "In play", awaiting: "Finished · awaiting result" };
const KICKED_OFF_TEXT = "This match has kicked off, and paper trades only open before kickoff.";
const startedTradeHead = (t) => (tradePhase(t) === "inplay" ? "paper trade in play" : "match finished, paper trade awaiting result");
const startedTradeNote = (t) => (tradePhase(t) === "inplay"
  ? "Prices after kickoff aren't logged, so the close and its CLV are final; the trade settles after full time."
  : AWAIT_NOTE);
const AWAIT_NOTE = "Results usually come in within a few hours of full time, and each trade then settles automatically.";
// "Close +140 (2.40) · CLV +3.1%" for a trade whose match has started ("no close logged" without one).
function startedCloseText(t, html = true) {
  const clv = tradeClv(t);
  const who = closeWho(t);
  if (t.close_odds == null) return `No close logged${clv != null ? ` · CLV ${signedPct(clv)}` : ""}`;
  const c = html ? `<span class="nowrap">CLV <span class="${plClass(clv)}">${signedPct(clv)}</span></span>` : `CLV ${signedPct(clv)}`;
  return `${who} ${american(t.close_odds)} (${Number(t.close_odds).toFixed(2)})${clv != null ? ` · ${c}` : ""}`;
}

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
      <span class="lbl">Model</span>${fx.no_model ? '<span class="none">No model chance</span>' : keys.map((k) => cell(k, fx.p[k], true)).join("")}
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

// The match sheet for a card without a model chance: DraftKings' odds and the reason.
function noModelHtml(fx) {
  const { home, away, kickoff, odds: o } = fx;
  const imp = o ? impliedFor(fx) : {};
  const rows = [["home", `${esc(home)} win`], ["draw", "Draw"], ["away", `${esc(away)} win`]]
    .map(([k, name]) => `<tr><td>${name}</td><td>${pct(imp[k])}</td><td>${price(o?.[k])}</td></tr>`).join("");
  const when = new Date(kickoff).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const dk = isDK() ? "DK" : "Book";
  return `
    <div class="detail">
      <p class="muted" style="margin:0;font-size:13px">${esc(when)} · ${esc(leagueName(fxLeague(fx)))}</p>
      <h2 id="sheet-title">${esc(home)} v ${esc(away)}</h2>
      <div class="explain">${esc(fx.no_model_reason || "No model chance for this match.")} No paper trade opens on it.</div>
      ${teamNewsSection(fx)}
      <div class="section-title">Markets</div>
      <div class="card" style="padding:8px 14px">
        <table class="mkts"><thead><tr><th></th><th>${dk}</th><th>Odds</th></tr></thead><tbody>${rows}</tbody></table>
      </div>
      <p class="note">${dk} = ${bookPoss()} odds as a chance, margin removed.${o?.home == null ? ` No ${bookName()} odds yet: they're fetched from 48 hours before kickoff.` : ""}</p>
    </div>`;
}

function detailHtml(fx) {
  if (fx.no_model) return noModelHtml(fx);
  const { home, away, kickoff, xg, p, odds: o, top, matrix, lowData } = fx;
  const imp = o ? impliedFor(fx) : {};
  const hasOdds = o && Object.values(o).some((v) => v != null);
  const blend = hasOdds && fx.p_bet ? fx.p_bet : null;
  const zs = hasOdds && leanOn() && fx.z ? fx.z : null; // Blend Lean: σ above break-even
  const zms = zs && fx.z_market ? fx.z_market : null; // the same without the model's term
  const lp = leanPick(fx);
  const rows = MARKETS.map(([k, label]) => {
    const name = k === "home" ? `${esc(home)} win` : k === "away" ? `${esc(away)} win` : label;
    const odds_ = o?.[k];
    const pb = (leanOn() ? fx.p_bet || p : pickProbs(fx))[k]; // Lean: the blend; else the chance bestPick uses
    const edge = odds_ != null && pb != null ? pb * odds_ - 1 : null;
    return `<tr><td>${name}</td><td>${pct(p[k])}</td>${
      hasOdds
        ? `${blend ? `<td class="${fixedRule() ? "" : "blend-col"}">${pct(blend[k])}</td>` : ""}<td>${pct(imp[k])}</td><td>${price(odds_)}</td><td class="${edge > 0 ? "edge-pos" : ""}">${signedPct(edge)}</td>${zs ? `<td class="${zs[k] != null && zs[k] >= 1 - 1e-9 ? "edge-pos" : ""}${lp?.market === k ? " lean-cell" : ""}">${zs[k] != null ? sigmaText(zs[k]) : ""}${zms?.[k] != null ? `<span class="zm">mkt ${sigmaText(zms[k])}</span>` : ""}</td>` : ""}`
        : `<td>${odds(1 / p[k])}</td>`
    }</tr>`;
  }).join("");
  const when = kickoff ? new Date(kickoff).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "Any fixture, first team at home";
  const dk = isDK() ? "DK" : "Book";
  const marketNote = !hasOdds
    ? "Fair odds = the price that would exactly match the model's chance."
    : leanOn()
      ? `Model = the model alone. ${blend ? (zs ? "Blend = the model mixed with the market: the chance Blend Lean trades on. " : `Blend = the model mixed with ${bookPoss()} price, shown for comparison (Blend Lean has no fit for this competition yet). `) : ""}${dk} = ${bookPoss()} odds as a chance, margin removed. Edge = ${blend ? "blend" : "model chance"} × payout − 1, against the quoted odds.${zs ? ` σ = how far the blend sits above the quoted price's break-even (1 ÷ decimal odds), in σ: Lean needs +1σ, Strong +2σ. Lean ranks the sides by σ, not by edge, so a long shot can show a bigger edge than the Lean side.${zms ? " mkt = the same with the model's term taken out of the blend: a Lean pick whose mkt is still +1σ or more is market-driven, otherwise model-driven." : ""}` : ""}`
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
        <table class="mkts${blend ? " with-blend" : ""}${zs ? " with-z" : ""}">
          <thead><tr><th></th><th>Model</th>${hasOdds ? `${blend ? `<th class="${fixedRule() ? "" : "blend-col"}">Blend</th>` : ""}<th>${dk}</th><th>Odds</th><th>Edge</th>${zs ? "<th>σ</th>" : ""}` : "<th>Fair odds</th>"}</tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <p class="note">${marketNote}</p>
      ${hasOdds && leanOn() ? `<div class="explain lean-explain">${leanPickText(fx)}</div>` : ""}
      ${leanOn() ? "" : hasOdds && fixedRule() ? `<div class="explain">${fixedPickText(fx, pick)} ${esc(fixedNote(fxLeague(fx)))}</div>` : hasOdds && fxEdge(fx) == null ? `<div class="explain"><b>Why no value bet here?</b> ${noEdgeText("moneyline", fxLeague(fx))}</div>` : blend && !pick ? `<div class="explain"><b>Why no value bet here?</b> ${blendWhyText(imp.margin)}</div>` : ""}
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
  const e1Note = e1 ? " These are less reliable in the Championship: it runs on goals only, and in testing its goal chances were too confident." : ["E2", "E3"].includes(lg) ? ` ${esc(leagueName(lg))} wasn't part of that test; it runs on goals only, like the Championship, where the goal chances were too confident.` : "";
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

// Match sheet "Corners": each team's corner lines, model (f) beside each book it is
// paper-traded against (owner's live test, portfolio "corners"): Pinnacle, and from 20 Oct
// DraftKings. corners = {model, home: {mean, cdf}, away, pinnacle?, draftkings?}; each
// quoted line has p_over/p_under/p_push from the model and `pick` at the 12% rule. A
// one-sided DraftKings line has the missing price (and its fair chance) null.
const WEAK_CORNER_FIT = new Set(["SP1", "D1", "E1"]);
// League One and League Two (on from 24 Oct, Pinnacle only): model (f) was never tested there.
const UNTESTED_CORNER_FIT = new Set(["E2", "E3"]);
// DraftKings lists no team corners in these leagues.
const NO_DK_CORNERS = new Set(["E1", "E2", "E3"]);
// The open corner trade on this match, team line and book (one per team line and book,
// ever), so a DraftKings trade never marks Pinnacle's line or the other way round.
function cornerTrade(fx, side, line, book = "pinnacle") {
  return (pfById("corners")?.live?.trades || []).find((t) => t.status === "open" && t.home === fx.home && t.away === fx.away && t.team_side === side && Number(t.line) === Number(line) && cornerBook(t) === book);
}
// The better side's edge at this book's price: model chance × odds (+ push chance) − 1.
function cornerEdge(q) {
  const eo = q.p_over != null && q.over != null ? q.p_over * q.over + (q.p_push || 0) - 1 : null;
  const eu = q.p_under != null && q.under != null ? q.p_under * q.under + (q.p_push || 0) - 1 : null;
  if (eo == null && eu == null) return null;
  return (eu ?? -9) > (eo ?? -9) ? ["under", eu] : ["over", eo];
}
function cornersSection(fx) {
  const co = fx.corners;
  const lg = fxLeague(fx);
  if (!co?.home || !co?.away) {
    const err = state.data.corners_model?.[lg]?.error;
    return err && fx.kickoff ? `<div class="section-title">Corners</div><p class="note">No corners model for ${esc(theLeague(lg))} in this update: there weren't enough recent matches with corner counts.</p>` : "";
  }
  const books = CORNER_BOOKS.filter((b) => co[b]);
  const rule = pfById("corners")?.live?.rule;
  const level = rule?.threshold ?? PAPER_EDGE;
  const started = kickoffPhase(fx.kickoff) !== "upcoming";
  const picks = [];
  const team = (side) => {
    const m = co[side];
    const name = esc(fx[side]);
    const lines = [...new Set(books.flatMap((b) => (co[b][side] || []).map((q) => Number(q.line))))].sort((x, y) => x - y);
    if (!lines.length) {
      const rows = [3.5, 4.5, 5.5].map((line) => `<tr><td>Over ${line}</td><td>${pct(overFrom(m.cdf, line))}</td></tr>`).join("");
      return `<div class="goals-team"><b>${name}</b> <span class="muted small">model expects ${Number(m.mean).toFixed(1)}</span>
        <table class="mkts goals"><thead><tr><th>Corners</th><th>Model</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    }
    const rows = lines.map((line) => {
      const quotes = books.map((b) => [b, (co[b][side] || []).find((q) => Number(q.line) === line)]).filter(([, q]) => q);
      return quotes.map(([b, q], i) => {
        const best = cornerEdge(q);
        // One trade per team line and book, ever: an open trade is marked by its own side
        // and entry price (the pick may have moved or lapsed since); otherwise the pick.
        const trade = cornerTrade(fx, side, line, b);
        const pick = started ? null : q.pick; // nothing new opens after kickoff
        if (trade || pick) picks.push({ side, book: b, q: { ...q, pick }, trade });
        const first = i === 0 ? `<td rowspan="${quotes.length}">${line}</td><td rowspan="${quotes.length}">${pct(q.p_over)}</td>` : "";
        return `<tr class="${pick || trade ? "pick" : ""}${i ? " book2" : ""}" data-book="${b}" data-line="${line}">${first}<td class="bk">${BOOK_SHORT[b]}</td><td>${pct(q.fair_over)}</td><td>${american(q.over)}</td><td>${american(q.under)}</td><td class="${pick ? "edge-pos" : ""}">${best ? `${signedPct(best[1], 0)}<span class="mk-range">${best[0]}</span>` : "–"}</td></tr>`;
      }).join("");
    }).join("");
    return `<div class="goals-team"><b>${name}</b> <span class="muted small">model expects ${Number(m.mean).toFixed(1)}</span>
      <table class="mkts corners-t"><thead><tr><th>Line</th><th>Model</th><th>Book</th><th>Fair</th><th>Over</th><th>Under</th><th>Edge</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  };
  const body = team("home") + team("away");
  const pickHtml = picks.map(({ side, book, q, trade }) => (trade
    ? `<span class="badge paper" data-book="${book}">${CHECK}${({ inplay: "In play", awaiting: "Awaiting result" })[tradePhase(trade)] || "Paper trade open"} (${BOOK_NAME[book]}): ${esc(fx[side])} ${trade.side} ${Number(q.line)} @ ${american(trade.odds)} <span class="num">(${signedPct(trade.edge)} at entry)</span>${tradePhase(trade) === "upcoming" ? "" : ` · ${startedCloseText(trade)}`}</span>`
    : `<span class="badge" data-book="${book}">${CHECK}Live-test pick (${BOOK_NAME[book]}): ${esc(fx[side])} ${q.pick.side} ${Number(q.line)} @ ${american(q.pick.odds)} <span class="num">(${signedPct(q.pick.edge)})</span></span>`)).join("");
  const stamps = books.map((b) => co[b].fetched_at).filter(Boolean).sort();
  const when = stamps.length ? espnTime(stamps[stamps.length - 1]) : null;
  const weak = WEAK_CORNER_FIT.has(lg) ? ` Model (f) fits less well in ${esc(theLeague(lg))}: in testing its chances there were too spread out, so this league isn't in the February test and its live record is the only check.` : "";
  const untested = UNTESTED_CORNER_FIT.has(lg) ? ` Model (f) has never been tested in ${esc(theLeague(lg))}: no development run covered it (it fits on about 1,100 matches here), so treat these picks as untested.` : "";
  const noDk = NO_DK_CORNERS.has(lg) ? ` DraftKings lists no team corners in ${esc(theLeague(lg))}.` : "";
  return `
    <div class="section-title">Corners</div>
    <div class="explain live-test"><b>Live test.</b> Team corners are paper-traded: model (f) against Pinnacle's price and, separately, DraftKings' price, a $10 trade at ${pct(level)}+ edge on each. No edge is proven yet; the February test (Premier League, Serie A and Ligue 1) decides. <button class="linkish" data-goto-pf="corners">Team corners portfolio</button></div>
    <div class="card" style="padding:8px 14px">${body}${pickHtml ? `<div class="corner-picks">${pickHtml}</div>` : ""}</div>
    <p class="note">${books.length ? `Model = model (f)'s chance of the over; on a whole line (say 5) exactly 5 is a push and the stake comes back. Book: PIN = Pinnacle, DK = DraftKings. Fair = that book's chance of the over, margin removed. Over/Under = its odds${when ? `, logged ${esc(when)}` : ""}; "–" where it prices only one side. Edge = the better side's model chance × that book's odds (+ push chance) − 1. Each book is its own paper strategy, so a line can hold one trade per book. A pick here uses each book's latest logged price; a paper trade also needs a price from the last 3 hours, so not every pick becomes a trade.` : "Model = model (f)'s chance of each team going over the line. No Pinnacle or DraftKings price for this match yet: team corners are logged about a day before kickoff and again just before it."}${noDk} Teams with little top-flight history are pulled toward the league average.${weak}${untested}</p>`;
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
const LEAGUES = [["E0", "Premier League", "Premier"], ["SP1", "La Liga", "La Liga"], ["D1", "Bundesliga", "Bundesliga"], ["I1", "Serie A", "Serie A"], ["F1", "Ligue 1", "Ligue 1"], ["E1", "Championship", "Championship"], ["E2", "League One", "League One"], ["E3", "League Two", "League Two"], ["UCL", "Champions League", "Champions Lg"]];
const leagueName = (c) => state.data?.leagues?.find((l) => l.code === c)?.name || LEAGUES.find(([k]) => k === c)?.[1] || c;
// "the Premier League", "the Championship", "the Bundesliga", but "La Liga", "Serie A", "Ligue 1", "League One".
const theLeague = (c) => (["E0", "E1", "D1", "UCL"].includes(c) ? `the ${leagueName(c)}` : leagueName(c));
const leagueShort = (c) => LEAGUES.find(([k]) => k === c)?.[2] || leagueName(c);
const fxLeague = (fx) => fx.league || "E0"; // older data: Premier League only
// Understat's xG covers these; elsewhere (the Championship) the model runs on goals only,
// so a card's expected goals aren't labelled "xG".
const XG_LEAGUES = new Set(["E0", "SP1", "D1", "I1", "F1"]);
const xgLabel = (fx) => (XG_LEAGUES.has(fxLeague(fx)) ? "xG" : "exp. goals");
// A card without a model chance (a Champions League club no football-data league covers,
// or the cup's results still loading: publish's `no_model`) shows DraftKings' odds only.
const xgText = (fx, i) => (fx.no_model || fx.xg?.[i] == null ? "" : `${fx.xg[i].toFixed(1)} ${xgLabel(fx)}`);
const tLeague = (t) => t.league || "E0";
// Competitions in the data: fixtures plus Moneyline's trades, in LEAGUES order.
function leaguesPresent() {
  const ml = pfById("moneyline");
  const co = pfById("corners");
  // On Moneyline's Backtest and Model, also every competition the per-league backtests cover (League
  // One between matchdays, the Champions League), so each can be chosen there.
  const sb = state.data.portfolio?.strategy_backtests || {};
  const bt = state.tab === "portfolio" && (state.pfView === "backtest" || state.pfView === "model" || pfLevelOn() === "portfolio") && pfCurrent()?.id === "moneyline"
    ? [sb.lean, sb.edge12].flatMap((b) => (b?.available ? Object.keys(b.leagues || {}) : [])) : [];
  const codes = new Set([...(state.data.fixtures || []).map(fxLeague), ...(state.data.leagues || []).filter((l) => l.fixtures > 0).map((l) => l.code), ...[...(ml?.live?.trades || []), ...(ml?.backtest?.trades || []), ...(co?.live?.trades || [])].map(tLeague), ...bt]);
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
const leanNote = (lg) => leanRule(lg)?.note || (pfById("moneyline")?.live?.strategies || []).find((x) => x.key === "lean")?.rule?.note || "";
// The match sheet's Blend Lean line: the pick (blend vs the price's break-even, in σ),
// the open Lean trade, or why there is none.
// "Market-driven: without the model's term it is still +1.4σ." (pick or trade; z_market)
function driverText(x) {
  const d = driverOf(x);
  if (!d || x.z_market == null) return "";
  return d === "market"
    ? `Market-driven: with the model's term taken out of the blend it is still ${sigmaText(x.z_market)}, so the price alone carries it. `
    : `Model-driven: with the model's term taken out of the blend it would be ${sigmaText(x.z_market)}, so the model pushed it over 1σ. `;
}
function leanPickText(fx) {
  const lg = fxLeague(fx);
  const sd = leanSigma(lg);
  if (sd == null) return `<b>Blend Lean: no pick.</b> ${esc(state.data.match_blends?.note || leanRule(lg)?.note || "No blend fit for this competition yet.")}`;
  const sdText = `${esc(leagueName(lg))} σ = ${ptsText(sd)}: how far the blend usually strays from the market in this competition.`;
  if (fx.low_data) return `<b>Blend Lean: no pick.</b> One team has few matches in the model, so paper trades skip this match. ${sdText}`;
  const z = fx.z;
  if (!z || !fx.p_bet) return `<b>Blend Lean: no pick.</b> No blended chance for this match yet. ${sdText}`;
  const lp = leanPick(fx);
  const t = openTradeFor(fx, "lean");
  const side = (m) => {
    const be = 1 / fx.odds[m];
    return `${esc(PICK_LABEL[m])} at ${price(fx.odds[m])}: the blend gives ${pct(fx.p_bet[m], 1)} against the ${pct(be, 1)} this price needs to break even, ${sigmaText(z[m])}`;
  };
  let head;
  const started = kickoffPhase(fx.kickoff) !== "upcoming";
  if (t && started) {
    head = `<b>Blend Lean: ${startedTradeHead(t)}:</b> ${esc(tradeLabel(t))} at ${price(t.odds)}${t.z != null ? ` (${sigmaText(t.z)} when it opened${driverOf(t) ? `, ${DRIVER_NAME[driverOf(t)].toLowerCase()}` : ""})` : ""}. ${startedCloseText(t)}. ${startedTradeNote(t)}`;
  } else if (started) {
    head = `<b>Blend Lean: no pick.</b> ${KICKED_OFF_TEXT}`;
  } else if (t) {
    const same = lp && PICK_LABEL[lp.market] === PICK_LABEL[t.market];
    head = `<b>Blend Lean: paper trade already open:</b> ${esc(tradeLabel(t))} at ${price(t.odds)}${t.z != null ? ` (${sigmaText(t.z)} when it opened${driverOf(t) ? `, ${DRIVER_NAME[driverOf(t)].toLowerCase()}` : ""})` : ""}. ${driverText(t)}One Lean trade per match, so nothing new opens here.${same ? ` Still the pick today: ${side(lp.market)}.` : lp ? ` At today's price Lean would pick ${side(lp.market)}.` : " At today's price no side reaches 1σ any more; the trade stays open until it settles."} ${LEAN_CAVEAT}`;
  } else if (leanOut(lp)) {
    const ri = leanRangeInfo(lg);
    const m = lp.market;
    head = `<b>No Lean trade:</b> the pick is ${esc(PICK_LABEL[m])} at ${price(lp.odds)}, outside Lean's ${ri ? ri.words : "odds"} range${ri?.from ? ` since ${monthDay(ri.from)}` : ""}. The blend gives it ${pct(fx.p_bet[m], 1)} against the ${pct(1 / fx.odds[m], 1)} this price needs to break even, ${sigmaText(z[m])}, but Lean no longer trades at these odds. ${ri?.note ? esc(ri.note) : ""}`;
  } else if (lp) {
    head = `<b>Blend Lean pick (${tierName(lp)}, ${DRIVER_NAME[driverOf(lp) || "unknown"].toLowerCase()}):</b> ${side(lp.market)}. ${driverText(lp)}A $10 paper trade opens on it at the next update with fresh odds. ${LEAN_CAVEAT}`;
  } else {
    const best = ["home", "draw", "away"].filter((m) => z[m] != null && fx.odds?.[m]).sort((a, b) => z[b] - z[a])[0];
    head = best ? `<b>Blend Lean: no pick.</b> Closest is ${side(best)}; a Lean pick needs +1σ.` : "<b>Blend Lean: no pick.</b>";
  }
  return `${head} ${sdText}`;
}
// The match sheet's live-test line. One paper trade per match: once one is open, a later
// price move can flag another outcome (or none), but no second trade opens.
function fixedPickText(fx, pick) {
  const t = openTradeFor(fx, "edge12"); // the 12% test's own trade, never the Lean one
  const now = pick ? `${esc(PICK_LABEL[pick.market] || pick.market)} at ${price(pick.odds)}, ${signedPct(pick.edge)} edge on the model alone` : "";
  const l = leanOn();
  const started = kickoffPhase(fx.kickoff) !== "upcoming";
  if (t && started) return `<b>${l ? "Model 12%: " : ""}${startedTradeHead(t)}:</b> ${esc(tradeLabel(t))} at ${price(t.odds)}. ${startedCloseText(t)}. ${startedTradeNote(t)}`;
  if (started) return `<b>${l ? "Model 12%: no pick" : "No pick here"}.</b> ${KICKED_OFF_TEXT}`;
  if (t) {
    const same = pick && PICK_LABEL[pick.market] === PICK_LABEL[t.market];
    return `<b>${l ? "Model 12%: paper trade already open" : "Paper trade already open"}:</b> ${esc(tradeLabel(t))} at ${price(t.odds)}. Only one ${l ? "12% " : ""}trade per match, so nothing new opens here.${same ? ` It is still the ${l ? "12%" : "live-test"} pick at today's price: ${now}.` : pick ? ` At today's price the ${l ? "12% rule" : "live test"} would pick ${now}.` : !pick ? ` At today's price no outcome reaches the ${l ? "12% rule's" : "live test's"} edge any more; the trade stays open until it settles.` : ""}`;
  }
  if (pick) return `<b>${l ? "Model 12% pick" : "Live-test pick"}:</b> ${now}. A $10 paper trade opens on it at the next update with fresh odds.`;
  if (fixedSkips(fx)) return `<b>${l ? "Model 12%: no pick" : "No pick here"}.</b> One team has few matches in the model, so the live test skips this match.`;
  return `<b>${l ? "Model 12%: no pick" : "No pick here"}.</b> No outcome reaches the ${pct(fxEdge(fx))} edge your live test needs.`;
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
// Blend Lean's line in the edge panel: the 1σ rule and the league's σ (or each league's).
function leanEdgeRec(lg, labelled = true) {
  const sd = leanSigma(lg);
  const one = lg || !multiLeague();
  const list = one ? "" : leaguesPresent().map((c) => `${esc(leagueShort(c))} ${leanSigma(c) == null ? "no fit yet" : ptsText(leanSigma(c))}`).join(" · ");
  const head = one
    ? `<b>Blend Lean: 1σ${sd != null ? ` (${esc(leagueName(lg || "E0"))} σ = ${ptsText(sd)})` : ""}.</b>`
    : "<b>Blend Lean: 1σ.</b>";
  const body = one && sd == null
    ? ` ${esc(leanNote(lg) || "No blend fit for this competition yet, so nothing is flagged.")}`
    : ` Flagging the side where the blend of model and DraftKings' price beats the price's break-even by at least one σ (Strong at 2σ), one $10 paper trade per match. σ is how far the blend usually strays from the market in that competition. ${LEAN_CAVEAT}`;
  return `<div class="edge-rec">${labelled ? '<span class="meta">First strategy</span>' : ""}${head}${body}${list ? `<div class="meta">σ by competition: ${list}.</div>` : ""}</div>`;
}
function fixedEdgePanel(lg = "") {
  const th = edgeRule("moneyline", lg);
  const level = leagueRule(lg)?.threshold ?? PAPER_EDGE;
  const exploring = th.source === "explore";
  return `
    <div class="edge-panel">
      ${leanOn() ? leanEdgeRec(lg) : ""}
      <div class="edge-rec${leanOn() ? " second" : ""}">${leanOn() ? '<span class="meta">Second strategy</span>' : ""}<b>Fixed ${pct(level)} (your live test).</b> Flagging every pick where the model alone, without the market blend, shows at least ${aPct(level)} edge against DraftKings' price: exactly what paper trades take. This rule lost money in backtests; the live test measures it on real prices.${leagueEdgeList("moneyline", lg)}</div>
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
    return `<div class="eb-row${above ? " above" : ""}" data-stop="${i}" data-ebrow="${i}" data-eb="${esc(pfId)}">
      <span class="eb-label">${label(r)}<span class="meta">${r.n} bets</span>${r.roi != null ? `<span class="meta ${plClass(r.roi)}">ROI ${signedPct(r.roi, 0)}</span>` : ""}</span>
      <span class="eb-plot">${range}${dot(r.implied, "imp")}${dot(r.model, "mod")}${dot(r.realized, `real ${beat ? "pos" : "neg"}`)}</span>
    </div>`;
  }).join("");
  const axis = `<div class="eb-axis"><span></span><span class="eb-plot">${ticks.map((v) => `<span style="left:${x(v)}%">${pct(v)}</span>`).join("")}</span></div>`;
  scrubCharts[`eb-${pfId}`] = { n: rows.length, rows: true, readoutId: `eb-readout-${pfId}`, readout: (i) => ebReadout(et, i) };
  return `
    <div class="section-title">Claimed edge vs what happened</div>
    <div class="card eb-card scrub-rows" data-ebcard="${esc(pfId)}" ${scrubAttrs(`eb-${esc(pfId)}`, rows.length, "Point at or drag across the rows, or use the arrow keys")}>
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
  const openTrades = openMatchTrades(); // keyed by match and strategy: never mix Lean and 12%
  const lean = leanOn();
  const nLean = shown.filter(([fx]) => leanPick(fx) && !leanOut(leanPick(fx))).length;
  const nOut = shown.filter(([fx]) => leanOut(leanPick(fx))).length;
  const ri = leanRangeInfo(lg);
  const leanNoFit = lean && (state.data.match_blends?.note || (lg && leanSigma(lg) == null ? leanRule(lg)?.note : ""));
  const leanCount = lean ? `<b>${nLean}</b> of ${shown.length} matches have a Blend Lean pick (the blend beats ${bookPoss()} break-even by 1σ or more${ri ? `, at odds ${ri.words}` : ""}).${nOut ? ` ${nOut} more pick${nOut === 1 ? " is" : "s are"} outside Lean's odds range, so no trade opens (shown greyed).` : ""}${leanNoFit ? ` ${esc(leanNoFit)}` : ""} ` : "";
  const countText = lean
    ? `${leanCount}Each Lean pick${ri ? " in range" : ""} opens one $10 paper trade per match. The Model 12% test still runs alongside; its picks and trades are under Portfolio → Moneyline.`
    : `<b>${nValue}</b> of ${shown.length} matches have a live-test pick: the model alone shows ${minE != null ? `${pct(minE)}+` : "enough"} edge against ${bookPoss()} price. A $10 paper trade opens on each match that doesn't have one yet (one per match).`;
  const ruleBox = lean
    ? `<div class="explain live-test"><b>Blend Lean:</b> ${esc(leanNote(lg))}${ri?.note ? ` <b>Odds range:</b> ${esc(ri.note)}` : ""}</div>`
    : fixedRule() && fixedNote(lg) ? `<div class="explain live-test">${esc(fixedNote(lg))}</div>` : "";
  let html = `${leagueFilter()}<p class="note">Chances of each result: the model vs ${bookName()}. ${oddsAge()} ${lg && shown.every(([fx]) => !fx.odds) ? `No ${bookName()} odds for ${esc(leagueName(lg))} matches yet: odds for this league are fetched from 48 hours before a kickoff, so there are no value bets here.` : fixedRule() || lean ? countText : `${nValue ? `<b>${nValue}</b> of ${shown.length} matches have a value bet${minE != null ? ` at ${pct(minE)}+ edge` : ""}.` : allNull ? noEdgeText("moneyline", lg) : d.match_blend?.live ? `No value bets right now. Value picks use the model blended with ${bookPoss()} price, and in past matches the price already held what the model knows, so the blend rarely beats ${bookPoss()} margin. Tap a match to see model, blend and ${bookName()} side by side.` : "No value bets right now."}`}</p>${ruleBox}`;
  for (const [day, items] of byDay) {
    html += `<div class="section-title">${esc(day)}</div>`;
    for (const [fx, idx] of items) {
      const phase = kickoffPhase(fx.kickoff);
      // With Lean on, Matches shows Lean only; once a match kicks off nothing new opens.
      const pick = lean || phase !== "upcoming" ? null : bestPick(fx, fxEdge(fx));
      const time = new Date(fx.kickoff).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
      const phaseTag = phase === "inplay" ? ' · <b class="phase-tag">In play</b>' : phase === "finished" ? ' · <b class="phase-tag">Finished</b>' : "";
      html += `
        <button class="card match" data-fixture="${idx}">
          <div class="match-head"><span>${esc(time)}${phaseTag}${!lg && multiLeague() ? ` · ${esc(leagueName(fxLeague(fx)))}` : ""}</span><span>Details ›</span></div>
          <div class="teams">
            <span>${esc(fx.home)}</span><span class="xg num">${xgText(fx, 0)}</span>
            <span>${esc(fx.away)}</span><span class="xg num">${xgText(fx, 1)}</span>
          </div>
          ${compareTable(fx, pick)}
          ${newsLine(fx)}
          ${lineupConfirmed(fx) ? `<span class="badge lineup">${CHECK}Lineups confirmed</span>` : ""}
          ${lean ? leanBadges(fx, openTrades.get(tradeKey(fx.home, fx.away, "lean"))) : ""}
          ${pick ? `<span class="badge${lean ? " second" : ""}">${CHECK}${pickBadge(pick, openTrades.get(tradeKey(fx.home, fx.away, "edge12")))}: ${esc(PICK_LABEL[pick.market])} @ ${price(pick.odds)} <span class="num">(${signedPct(pick.edge)})</span></span>` : ""}
          ${!lean && openTrades.has(tradeKey(fx.home, fx.away, "edge12")) ? openBadge(openTrades.get(tradeKey(fx.home, fx.away, "edge12"))) : ""}
          ${fx.no_model ? '<div class="warn">No model chance: odds only</div>' : fx.low_data ? '<div class="warn">⚠ Few matches for one team</div>' : ""}
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
    <p class="note" style="margin-top:0">${bookName()} = ${isDK() ? "DraftKings'" : "the bookmaker's"} odds turned into chances, with the built-in margin taken out so the three add up to 100%. A value bet is where the model rates an outcome high enough that the odds pay more than it's worth.${lean ? " Blend Lean mixes the model with DraftKings' price and picks the side whose blended chance beats the price's break-even by at least 1σ." : fixedRule() ? " Your live test checks value with the model alone, not blended with the price." : d.match_blend?.live ? " The value check uses the model blended with DraftKings' price, weighted by how much the model added in past matches." : ""}</p>
    <div class="section-title">${lean ? "Blend Lean rule" : "Minimum edge"}</div>
    ${lean ? `<div class="edge-panel">${leanEdgeRec(lg, false)}</div>` : edgePanel("moneyline", lg)}`;
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
    return `<g class="dev-row" data-stop="${i}">
      <rect class="hit" x="0" y="${yy}" width="${W}" height="${RH}"/>
      <text class="dev-name" x="${LBL - 8}" y="${yy + RH / 2 + 4}" text-anchor="end">${esc(name)}</text>
      <path class="${x.d >= 0 ? "dev-pos" : "dev-neg"}" d="${path}"/>
    </g>`;
  }).join("");
  // SD labels only when there's room beside the average's label.
  const roomy = sx(sd) - x0 > 46;
  const ticks = [-1, 0, 1].filter((k) => k === 0 || roomy).map((k) => `<text class="xlab" x="${sx(k * sd)}" y="${H - 8}" text-anchor="${k < 0 ? "end" : k > 0 ? "start" : "middle"}" dx="${k * 4}">${k === 0 ? `avg ${mean.toFixed(dp)}${unit}` : `${k > 0 ? "+" : "−"}1 SD`}</text>`).join("");
  // Each bar is a scrub stop (row chart): point at, drag or tap; arrows step.
  scrubCharts.dev = {
    n: rows.length, rows: true, readoutId: "dev-readout",
    readout: (i) => {
      const x = rows[i], r = x.r;
      return `<b>${esc(r.player)}</b> <span class="muted">${esc(r.team)} · ${esc(r.position || "")}</span><br>${esc(label)} ${x.v.toFixed(dp)}${unit} · <span class="${x.d >= 0 ? "gain" : "loss"}">${x.d >= 0 ? "+" : "−"}${Math.abs(x.d).toFixed(dp)}${unit} vs average (${x.z >= 0 ? "+" : "−"}${Math.abs(x.z).toFixed(1)} SD)</span> · ${r.shots} shots, ${r.minutes} min`;
    },
  };
  const ps = curPs();
  const scope = [seasonName(ps.seasons.includes(state.pl.season) ? state.pl.season : ps.seasons[0]), state.pl.team || "all clubs", state.pl.pos || "all positions"].join(" · ");
  return `
    <p class="note" style="margin-top:0"><b>${esc(label)}</b>, each player against the average of this group (${esc(scope)}, ${group.length} players${state.pl.metric === "shots" ? "" : " who qualify"}). Blue = above average, red = below; the gray band is ±1 standard deviation (${sd.toFixed(dp)}${unit}).${cut ? ` Showing the top and bottom ${DEV_MAX_ROWS / 2}.` : ""}</p>
    <div class="dev-readout" id="dev-readout" aria-live="polite">Point at, drag across or tap a bar for that player's numbers.</div>
    <div class="chart scrub" id="dev-chart" ${scrubAttrs("dev", rows.length, "Players against the average: point at or drag across the bars, or use the arrow keys")}>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}: deviation from the group average of ${mean.toFixed(dp)}${unit}">
        ${band}
        <line class="zero" x1="${x0}" x2="${x0}" y1="${T - 6}" y2="${H - B + 2}"/>
        ${bars}
        ${ticks}
      </svg>
    </div>`;
}

function refreshPlayerList() {
  $("#pl-list").innerHTML = state.pl.mode === "stats" ? statsBodyHtml() : playerListHtml();
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
  const cupNote = state.league === "UCL" ? `<p class="note">The Champions League has no ratings table of its own: its clubs come from many leagues. This shows ${esc(theLeague(lg))}; pick a club's league to see its rating.</p>` : "";
  if (!block?.ratings?.length) return `${teamsToggle()}${teamsLeagueChips()}${cupNote}<div class="empty">No ratings for ${esc(theLeague(lg))} yet.</div>`;
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
    ${teamsLeagueChips()}${cupNote}
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
  // Scrub stops: one per betting day; the readout line under the chart follows.
  const pr = (p) => `<b>${new Date(p.date).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}</b> · ${p.n} ${unit === "$" ? "trade" : "bet"}${p.n > 1 ? "s" : ""}, ${fmtAmount(p.day, unit, 2)} · running total ${fmtAmount(p.cum, unit)}`;
  scrubCharts.pc = {
    readoutId: "pc-readout",
    xs: daily.map((p) => x(Date.parse(p.date))),
    points: (i) => [{ x: x(Date.parse(daily[i].date)), y: y(daily[i].cum) }],
    readout: (i) => pr(daily[i]),
    data: daily,
  };
  return `
    <div class="chart scrub" id="profit-chart" ${scrubAttrs("pc", daily.length, "Running profit: point at or drag along the line, or use the arrow keys")}>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Running profit in ${unit === "$" ? "dollars" : "units"}, ending at ${fmtAmount(cum, unit)}">
        <g class="grid">${grid}</g>
        <line class="zero" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}"/>
        <path class="line" d="${path}" vector-effect="non-scaling-stroke"/>
        ${xl}
        <line class="cursor" x1="0" x2="0" y1="${T}" y2="${H - B}" style="display:none"/>
        <circle class="cdot" r="4" cx="0" cy="0" style="display:none"/>
        ${scrubPad(W, H)}
      </svg>
      <div class="eb-readout" id="pc-readout" aria-live="polite">${pr(daily[daily.length - 1])}</div>
    </div>`;
}
function fmtAmount(v, unit, d = 1) {
  if (unit !== "$") return `${signed(v, d)} units`;
  return `${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(d === 1 ? 0 : 2)}`;
}
function niceStep(raw) {
  const p = 10 ** Math.floor(Math.log10(raw || 1));
  const f = raw / p;
  return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p;
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
// range is above 0, red when it is all below, grey when it could be 0. Point at, drag or
// tap a row to read it (a row scrub chart).
function rangeChart(id, rows, fmt) {
  rows = rows.filter((r) => r.v != null && r.lo != null && r.hi != null);
  if (!rows.length) return "";
  scrubCharts[id] = { n: rows.length, rows: true, readoutId: `rc-readout-${id}`, readout: (i) => rows[i].readout };
  const vals = rows.flatMap((r) => [r.lo, r.hi]).concat(0);
  const span = Math.max(...vals) - Math.min(...vals) || 1;
  const lo = Math.min(...vals) - span * 0.08, hi = Math.max(...vals) + span * 0.08;
  const x = (v) => ((v - lo) / (hi - lo)) * 100;
  const tone = (r) => (r.lo > 0 ? "pos" : r.hi < 0 ? "neg" : "mid");
  const body = rows.map((r, i) => `
    <div class="eb-row rc-row" data-stop="${i}" data-rcrow="${i}" data-rc="${esc(id)}">
      <span class="eb-label">${esc(r.label)}${r.sub ? `<span class="meta">${esc(r.sub)}</span>` : ""}</span>
      <span class="eb-plot"><span class="rc-zero" style="left:${x(0)}%"></span><span class="eb-range ${tone(r)}" style="left:${x(r.lo)}%;width:${Math.max(0.5, x(r.hi) - x(r.lo))}%"></span><span class="eb-dot real ${tone(r)}" style="left:${x(r.v)}%"></span></span>
    </div>`).join("");
  // Ticks at 0 and near each end, skipping any that would crowd the 0 label.
  const ticks = [lo + (hi - lo) * 0.1, hi - (hi - lo) * 0.1].filter((v) => Math.abs(x(v) - x(0)) > 18);
  const axis = `<div class="eb-axis"><span></span><span class="eb-plot">${ticks.map((v) => `<span style="left:${x(v)}%">${fmt(v)}</span>`).join("")}<span style="left:${x(0)}%">0</span></span></div>`;
  return `<div class="card eb-card scrub-rows" ${scrubAttrs(id, rows.length, "Point at or drag across the rows, or use the arrow keys")}>${body}${axis}<div class="eb-readout" id="rc-readout-${esc(id)}" aria-live="polite">${rows[0].readout}</div></div>`;
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
  const link = live ? `<div class="explain live-test"><b>Now in a live test.</b> The owner is paper-trading model (f), a newer team-corner model, against Pinnacle's live prices in every competition and, as a second track since 20 Oct, against DraftKings'. <button class="linkish" data-goto-pf="corners">Open the ${esc(live.name)} portfolio</button></div>` : "";
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
// Record → Match bets → Blend: the blend trades through Blend Lean now, so its tiles are
// Blend Lean's Premier League backtest (portfolio.strategy_backtests.lean, leagues.E0),
// in the same four tiles as Model alone, at $10 a bet (the file is per $1).
const leanDkOn = () => !!state.data.portfolio?.strategy_backtests?.lean;
function leanDkTiles() {
  const lb = state.data.portfolio?.strategy_backtests?.lean;
  if (!lb) return "";
  if (lb.available === false) return `<div class="explain"><b>Blend Lean backtest:</b> ${esc(lb.note || "not available yet.")}</div>`;
  const e = lb.leagues?.E0;
  const l = e?.lean;
  if (!l?.bets) return `<div class="explain"><b>Blend Lean backtest:</b> ${esc(e?.error || "no Premier League bets in the file yet.")}</div>`;
  const seasons = e.seasons_included || [];
  const span = seasons.length ? ` ${esc(seasons[0])}${seasons.length > 1 ? `–${esc(seasons[seasons.length - 1])}` : ""}` : "";
  return `
    <p class="note lean-dk-label"><b>Blend Lean (1σ+), the rule the blend trades on now.</b> Premier League, Pinnacle's early prices${span}, $10 a bet. At the old ${pct(PAPER_EDGE)} edge the blend made no bets against DraftKings, as the table below shows.</p>
    <div class="tiles lean-dk" style="margin-top:8px">
      <div class="tile"><div class="label">Profit</div><div class="value ${plClass(l.profit)}">${usd(l.profit * 10)}</div><div class="sub">from ${l.bets} bets at 1σ+</div></div>
      <div class="tile"><div class="label">Return per bet</div><div class="value">${signedPct(l.roi)}</div><div class="sub">${l.roi_ci95 ? `95%: ${signedPct(l.roi_ci95[0], 0)} to ${signedPct(l.roi_ci95[1], 0)}` : "ROI"}</div></div>
      <div class="tile"><div class="label">Won</div><div class="value">${pct(l.win_rate, 1)}</div><div class="sub">${pct(l.breakeven, 1)} needed to break even</div></div>
      <div class="tile"><div class="label">Beat the close</div><div class="value">${l.beat_close != null ? pct(l.beat_close) : "–"}</div><div class="sub">avg price move ${l.clv != null ? signedPct(l.clv) : "–"} vs Pinnacle's close</div></div>
    </div>`;
}
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
    ${cur === "blend" ? leanDkTiles() : ""}
    ${noBets && cur === "blend" && leanDkOn() ? `<div class="explain"><b>No bets at a ${pct(th)} edge</b> against DraftKings: the blend stays too close to DraftKings' own chance to clear their margin.</div>` : noBets ? `<div class="explain"><b>No bets at a ${pct(th)} edge.</b> ${cur === "blend" ? "The blend stays so close to DraftKings' own chance that it never clears their margin, so it doesn't bet. That is the result: the model adds little that DraftKings' price doesn't already know." : "Nothing reached the threshold."}</div>` : `
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
const allTrades = () => portfolios().flatMap((p) => [...(p.live?.trades || []), ...(p.live?.shadow_trades || []), ...(p.backtest?.trades || [])]);
// A shadow entry (Blend Lean's old rule): what the old rule would have bet; never real P/L.
const shadowChip = (t) => (t.shadow ? `<span class="chip-shadow">Shadow: no trade opened${t.excluded_by === "odds_range" ? " (outside the odds range)" : ""}</span>` : "");
const PF_STATUS = { live: "Live", testing: "In testing", retired: "Retired", closed: "Closed" };

// Current DraftKings price for an open trade: the latest price in data.json, else the
// last one the ledger saw, else the entry price.
function currentPrice(t) {
  const fx = state.data.fixtures.find((f) => f.home === t.home && f.away === t.away);
  let cur = null;
  if (isCorner(t)) {
    const q = fx?.corners?.[cornerBook(t)]?.[t.team_side]?.find((x) => Number(x.line) === Number(t.line));
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
  return `<div class="meta">${closeWho(t)} ${priceBoth(t.close_odds)}${m != null ? ` · ${minutesText(m)} before` : ""} · <span class="nowrap">CLV <span class="${plClass(clv)}">${signedPct(clv)}</span></span>${closeApprox(t) ? ' · <span class="approx">approx.</span>' : ""}</div>${isDkCorner(t) && t.clv_pinnacle != null ? `<div class="meta">CLV <span class="${plClass(t.clv_pinnacle)}">${signedPct(t.clv_pinnacle)}</span> vs Pinnacle's close</div>` : ""}`;
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
  const profit = `<div class="tile"><div class="label">Settled profit</div><div class="value ${plClass(s.profit)}">${usd(s.profit)}</div><div class="sub">${s.staked ? `on $${s.staked.toLocaleString()} staked` : s.counts_only && s.settled ? "not kept per competition for this filter" : "nothing settled yet"}</div></div>`;
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
    let curEdge = null, curZ = null;
    if (isCorner(t)) {
      const q = fx?.corners?.[cornerBook(t)]?.[t.team_side]?.find((x) => Number(x.line) === Number(t.line));
      const pw = q?.[t.side === "over" ? "p_over" : "p_under"];
      if (pw != null && live) curEdge = pw * cur + (q.p_push || 0) - 1;
    } else {
      const p = t.bet_type === "player" ? null : (isLean(t) ? fx?.p_bet : fx?.p)?.[t.market];
      curEdge = p != null ? p * cur - 1 : null;
      if (isLean(t) && p != null && t.sigma) curZ = (p - 1 / cur) / t.sigma;
    }
    const mtm = markToMarket(t, cur);
    const move = cur < t.odds ? "shortened" : cur > t.odds ? "drifted" : "unchanged";
    return `
      <button class="bet-row trade" data-trade="${esc(t.id)}">
        <span>${tradeTitle(t)}${isLean(t) ? driverChip(t) : ""}<div class="meta">${esc(kickoffText(t.kickoff))} · ${t.bet_type === "player" || isCorner(t) ? `${esc(t.home)} v ${esc(t.away)}` : esc(tradeLabel(t))} · $${t.stake} @ ${american(t.odds)}</div><div class="meta">${tradePhase(t) !== "upcoming" ? `<b class="phase-tag ${tradePhase(t)}">${PHASE_LABEL[tradePhase(t)]}</b> · ${startedCloseText(t)}` : `Now ${american(cur)} (${move}${live ? "" : ", last seen"})${curZ != null ? ` · now ${sigmaText(curZ)}` : curEdge != null ? ` · edge now ${signedPct(curEdge)}` : ""}`}</div></span>
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
      <span>${tradeTitle(t)}${isLean(t) ? driverChip(t) : ""}<div class="meta">${esc(shortDate(t.kickoff))} · ${t.bet_type === "player" || isCorner(t) ? `${esc(t.home)} v ${esc(t.away)}` : esc(tradeLabel(t))} @ ${american(t.odds)} · edge ${signedPct(t.edge)}${t.score ? ` · ${esc(t.score)}` : ""}${t.actual != null ? ` · had ${isCorner(t) ? `${t.actual} corners` : countWord(t.market, t.actual)}` : ""}</div>${closeLine(t)}</span>
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
  const body = rows.map((r) => `<tr><td>${esc(label(r.group))}</td><td>${r.trades}</td><td>${signedPct(r.roi)}</td><td>${signedPct(pfCurrent()?.id === "corners" && pfStratKey(pfCurrent()) !== "draftkings" ? r.clv_pinnacle : r.clv_dk)}</td></tr>`).join("");
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
const pickBadge = (pick, t) => {
  if (!fixedRule()) return "Value";
  const moved = t && PICK_LABEL[t.market] !== PICK_LABEL[pick.market];
  if (leanOn()) return moved ? "12% would pick now" : "12% pick";
  return moved ? "Would pick now" : "Live-test pick";
};
// An open match trade on a card: its bet, entry price, today's price and value if cashed out.
// Once the match has started there is no "now" price: "In play" or "Awaiting result" with the close.
function openBadge(t) {
  const tag = leanOn() ? (stratOf(t) === "lean" ? " (Lean)" : " (12%)") : "";
  const ph = tradePhase(t);
  if (ph === "inplay" || ph === "awaiting") {
    return `<span class="badge paper ${ph}">${ph === "inplay" ? "In play" : "Awaiting result"}${tag}: ${esc(tradeLabel(t))} @ ${american(t.odds)} · ${startedCloseText(t)}</span>`;
  }
  const { cur } = currentPrice(t);
  const m = markToMarket(t, cur);
  return `<span class="badge paper">Paper trade open${tag}: ${esc(tradeLabel(t))} @ ${american(t.odds)} · now ${american(cur)} <b class="${plClass(m)}">${usd(m, 2)}</b></span>`;
}
// The headline Blend Lean badge ("Lean · 1.3σ" or "Strong · 2.4σ"), then its open trade.
function leanBadges(fx, t) {
  const lp = leanPick(fx);
  const moved = lp && t && PICK_LABEL[t.market] !== PICK_LABEL[lp.market];
  if (leanOut(lp)) {
    const ri = leanRangeInfo(fxLeague(fx));
    const out = `<span class="badge lean-out">Lean pick outside its odds range, no trade: ${esc(PICK_LABEL[lp.market])} @ ${price(lp.odds)}${ri ? ` (Lean trades ${ri.words} only)` : ""}</span>`;
    return `${out}${t ? openBadge(t) : ""}`;
  }
  const pickB = lp ? `<span class="badge lean">${CHECK}${moved ? "Would pick now, " : ""}${tierName(lp)} · ${sigmaText(lp.z).replace("+", "")}: ${esc(PICK_LABEL[lp.market])} @ ${price(lp.odds)}${driverOf(lp) ? ` · ${DRIVER_NAME[driverOf(lp)]}` : ""}</span>` : "";
  return `${pickB}${t ? openBadge(t) : ""}`;
}
const isMoneylineFixed = (pf) => pf?.id === "moneyline" && fixedRule();
// One match strategy's view of Moneyline's live section: its trades (trade_ids, else by
// `strategy`), summary, breakdowns and by_league, with the section's error and note.
function stratSet(set, st) {
  const ids = st.trade_ids ? new Set(st.trade_ids) : null;
  const trades = (set.trades || []).filter((t) => (ids ? ids.has(t.id) : stratOf(t) === st.key));
  return { ...set, trades, summary: st.summary || { trades: trades.length }, breakdowns: st.breakdowns || {}, by_league: st.by_league || null, rule: st.rule, note: st.key === "lean" ? null : set.note };
}
// ---------- Blend Lean backtest (portfolio.strategy_backtests.lean; lean_backtest.py) ----------
// A replay at Pinnacle's early prices from football-data, $1 a bet (units), CLV against
// Pinnacle's margin-free close. `all` pools the leagues; leagues[code] has the same shape.
const unitsText = (x, d = 1) => (x == null ? "–" : `${x >= 0 ? "+" : "−"}${Math.abs(x).toFixed(d)} units`);
const ciText = (ci, d = 1) => (ci && ci[0] != null ? `${signedPct(ci[0], d)} to ${signedPct(ci[1], d)}` : "");
const lbBets = (bt) => (bt.bets || []).map((r) => Object.fromEntries((bt.bet_fields || []).map((k, i) => [k, r[i]])));
// Per-strategy backtest files (lean_backtest.json, edge12_backtest.json): domestic leagues
// at Pinnacle's early price (kind "early"); the Champions League at DraftKings' close
// (kind "close"), kept out of `all` and of the per-league charts.
const BT_NAME = { lean: "Lean", edge12: "Model 12%" };
const isClose = (l) => l?.kind === "close";
const btWhere = (l) => (isClose(l) ? "at DraftKings' close" : "at Pinnacle");
function btVerdict(bt, key = "lean") {
  const a = bt.all?.[key] || {};
  const lgs = Object.values(bt.leagues || {}).filter((l) => !isClose(l) && l[key]?.clv != null);
  const negClv = lgs.filter((l) => l[key].clv < 0).length;
  const anyRoiClear = lgs.some((l) => l[key].roi_ci95?.[0] > 0);
  const roiAllBelow = a.roi_ci95 && a.roi_ci95[1] < 0;
  const roiWord = a.roi == null ? "had no bets" : a.roi < 0 ? (roiAllBelow ? "lost money" : "lost slightly") : "made a little";
  const roiRange = a.roi_ci95 && a.roi_ci95[0] < 0 && a.roi_ci95[1] > 0 ? " (a range that includes break-even)" : roiAllBelow ? " (its whole range is below break-even)" : "";
  const clvWord = negClv === lgs.length ? "got worse prices than the close, on average, in every league" : `got worse prices than the close in ${negClv} of ${lgs.length} leagues`;
  return `In the replay at Pinnacle's prices ${BT_NAME[key]} ${roiWord}${roiRange} and ${clvWord}.${anyRoiClear ? "" : " No league's return range clears zero."}`;
}
// One early-price league: its own return and CLV, each with its range.
function leagueVerdict(l, key, lg) {
  const m = l[key] || {};
  if (!m.bets) return `No ${BT_NAME[key]} bets in ${leagueName(lg)}'s replay.`;
  const side = (ci) => (!ci ? "" : ci[0] > 0 ? ", its whole range above 0" : ci[1] < 0 ? ", its whole range below 0" : ", a range that includes 0");
  return `In ${leagueName(lg)}'s replay at Pinnacle's prices ${BT_NAME[key]} made ${m.bets.toLocaleString()} bets: ROI ${signedPct(m.roi)}${side(m.roi_ci95)}; CLV ${signedPct(m.clv)}${side(m.clv_ci95)}.`;
}
// The Champions League, at DraftKings' close: what its CLV does and doesn't measure.
function closeVerdict(l, key) {
  const m = l[key] || {};
  if (!m.bets) return "";
  return `At DraftKings' close ${BT_NAME[key]} made ${m.bets} bet${m.bets === 1 ? "" : "s"}: ROI ${signedPct(m.roi)}${m.roi_ci95 ? ` (range ${ciText(m.roi_ci95, 0)})` : ""}, CLV ${signedPct(m.clv)} against Pinnacle's close at the same moment. That CLV mostly measures the gap between the two books, not how the price moved.`;
}
function lbTiles(m) {
  if (!m) return "";
  return `<div class="tiles" style="margin-top:12px">
    <div class="tile"><div class="label">Bets</div><div class="value">${(m.bets || 0).toLocaleString()}</div><div class="sub">$1 each · avg odds ${m.avg_odds != null ? m.avg_odds.toFixed(2) : "–"}</div></div>
    <div class="tile"><div class="label">Won</div><div class="value">${pct(m.win_rate, 1)}</div><div class="sub">needed ${pct(m.breakeven, 1)} to break even</div></div>
    <div class="tile"><div class="label">ROI</div><div class="value ${plClass(m.roi)}">${signedPct(m.roi)}</div><div class="sub">${m.roi_ci95 ? `range ${ciText(m.roi_ci95)}` : "too few bets for a range"} · ${unitsText(m.profit)}</div></div>
    <div class="tile"><div class="label">Closing line value</div><div class="value ${plClass(m.clv)}">${signedPct(m.clv)}</div><div class="sub">${m.clv_ci95 ? `range ${ciText(m.clv_ci95)}` : "vs Pinnacle's close"} · beat it ${pct(m.beat_close)}</div></div>
  </div>`;
}
// Two or three groups side by side (Lean vs Strong, market vs model).
function lbCompare(cols) {
  const keys = cols.filter(([, m]) => m);
  if (!keys.length) return "";
  const row = (label, f) => `<tr><td>${label}</td>${keys.map(([, m]) => `<td>${f(m)}</td>`).join("")}</tr>`;
  return `<div class="card" style="padding:8px 14px"><table class="mkts mk-table drv-table">
    <thead><tr><th></th>${keys.map(([l]) => `<th>${esc(l)}</th>`).join("")}</tr></thead>
    <tbody>
      ${row("Bets", (m) => (m.bets || 0).toLocaleString())}
      ${row("Won", (m) => (m.win_rate == null ? "–" : `${pct(m.win_rate, 1)}<span class="mk-range">needs ${pct(m.breakeven, 1)}</span>`))}
      ${row("ROI", (m) => `<span class="${plClass(m.roi)}">${signedPct(m.roi)}</span>${m.roi_ci95 ? `<span class="mk-range">${ciText(m.roi_ci95, 0)}</span>` : ""}`)}
      ${row("CLV", (m) => `<span class="${plClass(m.clv)}">${signedPct(m.clv)}</span>${m.clv_ci95 ? `<span class="mk-range">${ciText(m.clv_ci95)}</span>` : ""}`)}
      ${row("Avg odds", (m) => (m.avg_odds != null ? m.avg_odds.toFixed(2) : "–"))}
      ${row("Mostly", (m) => { const e = Object.entries(m.by_market || {}).sort((a, b) => b[1] - a[1])[0]; return e && m.bets ? `${esc(PICK_LABEL[e[0]] || e[0])} ${pct(e[1] / m.bets)}` : "–"; })}
    </tbody></table></div>`;
}
function lbLeagueCharts(bt, key = "lean") {
  const rows = Object.entries(bt.leagues || {}).filter(([, l]) => !isClose(l) && l[key]?.bets);
  if (!rows.length) return "";
  const ids = key === "lean" ? ["lbroi", "lbclv"] : [`${key}roi`, `${key}clv`];
  const mk = (k, ci) => rows.map(([c, l]) => {
    const m = l[key];
    return {
      label: leagueShort(c), sub: `${m.bets} bets`, v: m[k], lo: m[ci]?.[0], hi: m[ci]?.[1],
      readout: `<b>${esc(l.name || leagueName(c))}</b>, ${m.bets} bets: ROI ${signedPct(m.roi)} (range ${ciText(m.roi_ci95)}), ${unitsText(m.profit)}; CLV ${signedPct(m.clv)} (range ${ciText(m.clv_ci95)}); won ${pct(m.win_rate, 1)} vs ${pct(m.breakeven, 1)} needed. Seasons ${esc((l.seasons_included || []).join(", "))}${l.sigma ? `; σ ${ptsText(l.sigma.min)} to ${ptsText(l.sigma.max)}` : ""}.`,
    };
  });
  const cup = Object.values(bt.leagues || {}).some(isClose);
  return `
    <div class="section-title">Return by league</div>
    ${rangeChart(ids[0], mk("roi", "roi_ci95"), (v) => signedPct(v, 0))}
    <div class="section-title">Closing line value by league</div>
    ${rangeChart(ids[1], mk("clv", "clv_ci95"), (v) => signedPct(v, 1))}
    <p class="note">Dot = the league's result, bar = its 95% range. Blue when the whole range is above 0, red when below, grey when it could be either. Tap a league for the numbers.${cup ? " The Champions League is priced at DraftKings' close, not early, so it is shown on its own: choose it above." : ""}</p>`;
}
function lbSeasons(rows, key = "lean") {
  if (!rows?.length) return "";
  const lean = key === "lean";
  return `<details class="fold mk-fold"><summary>By season</summary>
    <div class="card" style="padding:8px 14px"><table class="mkts mk-table">
      <thead><tr><th></th><th>Bets</th><th>ROI</th><th>CLV</th><th>${lean ? "Market / model" : "Won"}</th></tr></thead>
      <tbody>${rows.map((r) => `<tr><td>${esc(r.label || seasonName(r.season))}</td><td>${r.bets ?? 0}</td><td class="${plClass(r.roi)}">${signedPct(r.roi)}</td><td class="${plClass(r.clv)}">${signedPct(r.clv)}</td><td>${lean ? `${r.market_bets ?? "–"} / ${r.model_bets ?? "–"}` : r.win_rate == null ? "–" : pct(r.win_rate, 1)}</td></tr>`).join("")}</tbody>
    </table></div></details>`;
}
function lbBetList(bt, lg, key = "lean") {
  const lean = key === "lean";
  const drv = lean ? state.pfDriver : "";
  const all = lbBets(bt).filter((b) => (!lg || b.league === lg) && (!drv || b.driver === drv));
  if (!all.length) return `<div class="section-title">Bets</div><p class="note">No bets in the newest ${bt.bets_shown || 0} shown here${lg || drv ? " for this choice" : ""}.</p>`;
  const shown = all.slice(0, state.lbShown);
  const rows = shown.map((b) => `
    <div class="bet-row">
      <span>${esc(b.home)} v ${esc(b.away)}${lean ? driverChip(b) : ""}<div class="meta">${esc(shortDate(b.date))}${!lg ? ` · ${esc(leagueShort(b.league))}` : ""} · ${esc(PICK_LABEL[b.market] || b.market)} @ ${american(b.odds)} (${Number(b.odds).toFixed(2)}) · ${lean ? `${sigmaText(b.z)}${b.tier === "strong" ? " Strong" : ""}` : `edge ${signedPct(b.edge, 0)}`}</div><div class="meta">${lean ? `Blend ${pct(b.p_bet, 1)} · model ${pct(b.p_model, 1)}` : `Model ${pct(b.model_p, 1)}`} · CLV <span class="${plClass(b.clv)}">${signedPct(b.clv)}</span></div></span>
      <span class="pl ${b.profit > 0 ? "win" : b.profit < 0 ? "loss" : ""}">${b.won ? "Won" : "Lost"}<div class="meta">${unitsText(b.profit, 2)}</div></span>
    </div>`).join("");
  return `
    <div class="section-title">Bets, newest first</div>
    <div class="card">${rows}</div>
    ${all.length > shown.length ? `<button class="more" data-lbmore>Show more (${all.length - shown.length} left)</button>` : ""}
    <p class="note">${lg ? `${esc(leagueName(lg))}'s bets among the newest ${(bt.bets_shown || 0).toLocaleString()} of ${(bt.bets_total || 0).toLocaleString()} kept for the app` : `The newest ${(bt.bets_shown || 0).toLocaleString()} of ${(bt.bets_total || 0).toLocaleString()} bets are listed`}; every figure above uses all of them.</p>`;
}
const leanBacktestHtml = (bt, lg = "") => strategyBacktestHtml(bt, lg, "lean");
// One strategy's backtest file, all leagues or one: verdict, units tiles, per-league range
// plots (All), seasons, coverage and fits, the bet list. Lean adds Strong and the drivers.
// `opts.brief` (Model 12% under the Premier League, beside the DraftKings backtest) stops
// after the seasons and coverage.
function strategyBacktestHtml(bt, lg = "", key = "lean", opts = {}) {
  const name = key === "lean" ? "Lean" : "Model 12%";
  if (!bt || !bt.available) return `<div class="empty">${esc(bt?.note || `The ${name} backtest isn't in this update.`)}</div>`;
  const part = lg ? bt.leagues?.[lg] : bt.all;
  if (lg && !part) return `<div class="empty">No ${name} backtest for ${esc(leagueName(lg))}.</div>`;
  if (part?.error) return `<div class="empty">${esc(leagueName(lg))}: ${esc(part.error)}</div>`;
  const lean = key === "lean";
  const close = isClose(part);
  const m = part[key] || {};
  const lgCount = (bt.all?.leagues || Object.values(bt.leagues || {}).filter((l) => !isClose(l))).length;
  const title = close ? `${name} replayed at DraftKings' close` : bt.title || `${name} replayed on Pinnacle's early prices`;
  const head = `<p class="note">${esc(title)}${lg ? `, ${esc(leagueName(lg))}` : `, all ${lgCount} leagues priced early`}. Money is in units: $1 a bet.</p>`;
  if (close && !m.bets) {
    return `${head}<div class="explain">${esc(part.note || `No ${name} bets in this backtest.`)}</div>${lbLeagueDetails(part)}`;
  }
  const verdict = close ? closeVerdict(part, key) : lg ? leagueVerdict(part, key, lg) : btVerdict(bt, key);
  const top = `
    <div class="verdict ${m.roi_ci95?.[0] > 0 ? "" : m.roi < 0 || m.clv < 0 ? "neg" : "mid"}">${esc(verdict)}</div>
    ${head}
    ${lbTiles(m)}
    ${close ? `<p class="note">${esc(part.note || "")}</p>` : ""}`;
  const bands = `${sizingHtml(`bz-${key}-${lg || "all"}`, part.sizing)}${backtestBandsHtml(bt, part, key, lg)}`;
  if (opts.brief) return `${top}${bands}${lbSeasons(part.by_season, key)}${lg ? lbLeagueDetails(part) : ""}`;
  const by = m.by_driver || {};
  const dnote = bt.rule?.driver_note || driverNote();
  const dk = bt.draftkings_replay;
  const dkSum = dk?.summary;
  const dkFold = !dk || lg ? "" : lean
    ? `<details class="fold mk-fold"><summary>${esc(dk.label || "DraftKings replay")}</summary><p class="note">${esc(dk.note || "")} ${dk.lean ? `Lean: ${dk.lean.bets} bet${dk.lean.bets === 1 ? "" : "s"}${dk.lean.roi != null ? `, ROI ${signedPct(dk.lean.roi)}` : ""}${dk.lean.clv_dk != null ? `, CLV vs DraftKings ${signedPct(dk.lean.clv_dk)}` : ""}.` : ""} ${dk.strong ? `Strong: ${dk.strong.bets} bets.` : ""}</p></details>`
    : `<details class="fold mk-fold"><summary>${esc(dk.label || "DraftKings backtest")}</summary><p class="note">${esc(dk.note || "")}${dkSum ? ` ${dkSum.trades ?? 0} bets, ROI ${signedPct(dkSum.roi)}${dkSum.clv_dk != null ? `, CLV vs DraftKings' close ${signedPct(dkSum.clv_dk)}` : ""}. Choose Premier League above to see it in full.` : ""}</p></details>`;
  const leanParts = lean ? `
    <div class="section-title">Lean and Strong</div>
    ${lbCompare([["Lean (1σ+, every bet)", m], ["Strong (2σ+)", part.strong]])}
    <p class="note">Strong picks are the ones at 2σ or more; they are also counted in Lean.</p>
    <div class="section-title">Market-driven and model-driven</div>
    ${lbCompare([["Market-driven", by.market], ["Model-driven", by.model]])}
    ${dnote ? `<p class="note">${esc(dnote)}</p>` : ""}` : "";
  const drvSeg = lean ? `
    <div class="segmented small-seg drv-seg" role="group" aria-label="Which backtest bets">
      ${[["", "All bets"], ["market", "Market-driven"], ["model", "Model-driven"]].map(([k, l]) => `<button data-pfdriver="${k}" class="${state.pfDriver === k ? "on" : ""}" aria-pressed="${state.pfDriver === k}">${esc(l)}</button>`).join("")}
    </div>` : "";
  return `
    ${top}
    ${lg ? "" : lbLeagueCharts(bt, key)}
    ${leanParts}
    ${bands}
    ${lbSeasons(part.by_season, key)}
    ${lg ? lbLeagueDetails(part) : ""}
    ${dkFold}
    ${drvSeg}
    ${lbBetList(bt, lg, key)}
    <p class="note">${esc(bt.note || "")}${bt.generated_at ? ` Run ${esc(shortDate(bt.generated_at))}.` : ""}</p>`;
}
// ---------- Backtest bands (owner via the Research lab, 23 Oct): by_odds_band, by_edge_band and
// by_clv_band in each league and `all` of both backtest files; `bands` names the cuts. ----------
const oddsWords = (d) => `${american(d)}`.replace(/^\+100$/, "even");
function oddsBandLabel(b) {
  if (b.lo == null) return `${oddsWords(b.hi)} or shorter (up to ${b.hi.toFixed(2)})`;
  if (b.hi == null) return `longer than ${oddsWords(b.lo)} (over ${b.lo.toFixed(2)})`;
  return `${oddsWords(b.lo)} to ${oddsWords(b.hi)} (${b.lo.toFixed(2)}–${b.hi.toFixed(2)})`;
}
const cutLabel = (b) => (b.lo == null ? `under ${signedPct(b.hi)}` : b.hi == null ? `${signedPct(b.lo)} and up` : `${signedPct(b.lo)} to ${signedPct(b.hi)}`);
const rngText = (ci) => (ci && isNum(ci[0]) ? ` (range ${ciText(ci)})` : "");
// A direction in words for y against the bands' order, from the bet-weighted slope and the
// first and last bands together; "no clear pattern" when they disagree or barely move.
function bandTrend(rows, xk, yk) {
  const r = rows.filter((b) => b.bets && isNum(b[xk]) && isNum(b[yk]));
  if (r.length < 2) return null;
  const w = r.reduce((a, b) => a + b.bets, 0);
  const mx = r.reduce((a, b) => a + b.bets * b[xk], 0) / w, my = r.reduce((a, b) => a + b.bets * b[yk], 0) / w;
  const sxx = r.reduce((a, b) => a + b.bets * (b[xk] - mx) ** 2, 0);
  const slope = sxx ? r.reduce((a, b) => a + b.bets * (b[xk] - mx) * (b[yk] - my), 0) / sxx : 0;
  const first = r[0], last = r[r.length - 1], step = last[yk] - first[yk];
  const dir = Math.abs(step) < 0.005 ? 0 : Math.sign(step) === Math.sign(slope) ? Math.sign(step) : 0;
  return { dir, slope, first, last, r };
}
// 1. Where the bets sit on the odds: share of bets (bar) and CLV (dot, 95% range, 0 marked).
function oddsBandChart(id, bands, what = "bets") {
  const withClv = bands.filter((b) => b.bets && isNum(b.clv));
  const vals = withClv.flatMap((b) => [b.clv_ci95?.[0] ?? b.clv, b.clv_ci95?.[1] ?? b.clv]).concat(0);
  const span = Math.max(...vals) - Math.min(...vals) || 0.01;
  const lo = Math.min(...vals) - span * 0.1, hi = Math.max(...vals) + span * 0.1;
  const x = (v) => ((v - lo) / (hi - lo)) * 100;
  const top = Math.max(...bands.map((b) => b.share || 0), 0.01);
  const read = (b) => (b.bets
    ? `<b>${esc(oddsBandLabel(b))}</b>: ${num(b.bets)} ${what} (${pct(b.share, 0)} of them); ${isNum(b.clv) ? `CLV ${signedPct(b.clv)}${rngText(b.clv_ci95)}${isNum(b.clv_trades) ? ` on ${b.clv_trades} with a final close` : ""}` : "no final close yet"}; ${isNum(b.win_rate) ? `won ${pct(b.win_rate, 1)} vs ${pct(b.breakeven, 1)} needed${isNum(b.settled) ? ` (${b.settled} settled)` : ""}; ROI ${signedPct(b.roi)}${rngText(b.roi_ci95)}` : "none settled yet"}.`
    : `<b>${esc(oddsBandLabel(b))}</b>: no ${what}.`);
  scrubCharts[id] = { n: bands.length, rows: true, readoutId: `rc-readout-${id}`, readout: (i) => read(bands[i]) };
  const tone = (b) => (b.clv_ci95?.[0] > 0 ? "pos" : b.clv_ci95?.[1] < 0 ? "neg" : b.clv > 0 ? "pos" : "neg");
  const rows = bands.map((b, i) => `
    <div class="eb-row band-row" data-stop="${i}">
      <span class="eb-label">${esc(oddsBandLabel(b))}</span>
      <span class="band-share">${b.bets ? `<span class="share-bar" style="width:${((b.share || 0) / top) * 100}%"></span><span class="share-num">${pct(b.share, 0)}</span>` : `<span class="muted">no ${what}</span>`}</span>
      <span class="eb-plot"><span class="rc-zero" style="left:${x(0)}%"></span>${b.bets && isNum(b.clv) ? `${b.clv_ci95 ? `<span class="eb-range ${tone(b)}" style="left:${x(b.clv_ci95[0])}%;width:${Math.max(0.5, x(b.clv_ci95[1]) - x(b.clv_ci95[0]))}%"></span>` : ""}<span class="eb-dot real ${tone(b)}" style="left:${x(b.clv)}%"></span>` : ""}</span>
    </div>`).join("");
  const ticks = [lo + (hi - lo) * 0.12, hi - (hi - lo) * 0.12].filter((v) => Math.abs(x(v) - x(0)) > 18);
  const axis = `<div class="eb-axis band-row"><span></span><span class="band-share muted">Share</span><span class="eb-plot">${ticks.map((v) => `<span style="left:${x(v)}%">${signedPct(v, 1)}</span>`).join("")}<span style="left:${x(0)}%">0</span></span></div>`;
  const busiest = bands.filter((b) => b.bets).sort((a, c) => c.share - a.share)[0];
  return `<div class="card eb-card scrub-rows" ${scrubAttrs(id, bands.length, "Odds bands: point at or drag across the rows, or use the arrow keys")}>
      <div class="eb-legend"><span><i class="eb-key share"></i>Share of ${what}</span><span><i class="eb-key real pos"></i>CLV (range)</span></div>
      ${rows}${axis}
      <div class="eb-readout" id="rc-readout-${id}" aria-live="polite">${busiest ? read(busiest) : `No ${what}.`}</div></div>`;
}
const rngCell = (v, ci) => `<td class="${plClass(v)}">${signedPct(v)}${ci && isNum(ci[0]) ? `<span class="meta">${signedPct(ci[0], 1)} to ${signedPct(ci[1], 1)}</span>` : ""}</td>`;
const bandTable = (head, rows) => `<div class="card" style="padding:8px 14px"><table class="mkts mk-table band-table"><thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
// 2. Claimed edge (x, the band's mean) against CLV (y, with its range): the dashed line is
// where the points would sit if the claimed edge were real; y = 0 is the close.
function edgeClvChart(id, bands, lean) {
  const r = bands.filter((b) => b.bets && isNum(b.claimed) && isNum(b.clv));
  if (r.length < 2) return "";
  const W = 320, H = 240, L = 44, R = 14, T = 12, B = 32;
  const xmax = Math.max(...r.map((b) => b.claimed)) * 1.08, xmin = Math.min(0, ...r.map((b) => b.claimed));
  const ys = r.flatMap((b) => [b.clv_ci95?.[0] ?? b.clv, b.clv_ci95?.[1] ?? b.clv]);
  // The dashed line runs to 15% at most, so a big claim (Model 12%'s reach 50%) doesn't
  // squash the CLV points flat; past that it leaves the top, marked with an arrow.
  const ymin = Math.min(0, ...ys) - 0.01, ymax = Math.max(0.02, Math.min(xmax, 0.15), ...ys);
  const sx = (v) => L + ((v - xmin) / (xmax - xmin || 1)) * (W - L - R), sy = (v) => T + (1 - (v - ymin) / (ymax - ymin || 1)) * (H - T - B);
  const step = niceStep((ymax - ymin) / 4), xstep = niceStep((xmax - xmin) / 4);
  let grid = "";
  for (let v = Math.ceil(ymin / step) * step; v <= ymax + 1e-9; v += step) grid += `<line x1="${L}" x2="${W - R}" y1="${sy(v)}" y2="${sy(v)}"/><text x="${L - 4}" y="${sy(v) + 3}" text-anchor="end">${Math.abs(v) < 1e-9 ? "0%" : signedPct(v, 0)}</text>`;
  for (let v = Math.ceil(xmin / xstep) * xstep; v <= xmax + 1e-9; v += xstep) grid += `<text x="${sx(v)}" y="${H - 16}" text-anchor="middle">${Math.abs(v) < 1e-9 ? "0%" : signedPct(v, 0)}</text>`;
  const x1 = Math.max(xmin, ymin), x2 = Math.min(xmax, ymax);
  const pts = r.map((b) => `${b.clv_ci95 ? `<line class="rng" x1="${sx(b.claimed)}" x2="${sx(b.claimed)}" y1="${sy(b.clv_ci95[0])}" y2="${sy(b.clv_ci95[1])}"/>` : ""}<circle class="mk ${b.clv >= 0 ? "pos" : "neg"}" cx="${sx(b.claimed)}" cy="${sy(b.clv)}" r="4"/>`).join("");
  scrubCharts[id] = {
    readoutId: `rc-readout-${id}`,
    xs: r.map((b) => sx(b.claimed)),
    points: (i) => [{ x: sx(r[i].claimed), y: sy(r[i].clv) }],
    readout: (i) => { const b = r[i]; return `<b>Claimed ${signedPct(b.claimed)}</b> (edges ${esc(cutLabel(b))}${lean && isNum(b.z) ? `, average ${sigmaText(b.z)}` : ""}): CLV ${signedPct(b.clv)}${rngText(b.clv_ci95)}, ROI ${signedPct(b.roi)}${rngText(b.roi_ci95)}, ${num(b.bets)} bets.`; },
  };
  return `<div class="chart curve band-chart scrub" ${scrubAttrs(id, r.length, "Claimed edge against CLV: drag or use the arrow keys")}>
      <div class="eb-legend"><span><i class="ln-key diag"></i>If the claim were right</span><span><i class="eb-key real neg"></i>CLV by band (range)</span></div>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Claimed edge against closing line value by band">
        <g class="grid">${grid}</g>
        <line class="zero" x1="${L}" x2="${W - R}" y1="${sy(0)}" y2="${sy(0)}"/>
        <line class="diag" x1="${sx(x1)}" y1="${sy(x1)}" x2="${sx(x2)}" y2="${sy(x2)}"/>
        ${xmax > ymax ? `<text class="xlab" x="${sx(x2) + 4}" y="${sy(x2) + 10}">claims up to ${signedPct(r[r.length - 1].claimed, 0)} ↑</text>` : ""}
        ${pts}
        <line class="cursor" x1="0" x2="0" y1="${T}" y2="${H - B}" style="display:none"/>
        <circle class="cdot" r="5.5" cx="0" cy="0" style="display:none"/>
        ${scrubPad(W, H)}
        <text class="xlab" x="${(L + W - R) / 2}" y="${H - 2}" text-anchor="middle">Edge it claimed when it bet</text>
      </svg>
      <div class="eb-readout" id="rc-readout-${id}" aria-live="polite">Point at or drag along the chart for each band's numbers.</div>
    </div>`;
}
function edgeVerdict(bands) {
  const t = bandTrend(bands, "claimed", "clv");
  if (!t) return "";
  const all = t.r.every((b) => b.clv < 0) ? " Every band got a worse price than the close." : t.r.every((b) => b.clv > 0) ? " Every band beat the close." : "";
  const span = `from ${signedPct(t.first.clv)} at a claimed ${signedPct(t.first.claimed)} to ${signedPct(t.last.clv)} at ${signedPct(t.last.claimed)}`;
  const lead = t.dir < 0 ? "The bigger the edge it claimed, the worse it did against the close"
    : t.dir > 0 ? "The bigger the edge it claimed, the better it did against the close"
      : "Its result against the close barely changed with the edge it claimed";
  return `${lead} (${span}).${all}`;
}
function clvProfitFold(id, bands) {
  const rows = bands.filter((b) => b.bets && isNum(b.roi)).map((b) => ({
    label: `CLV ${cutLabel(b)}`, sub: `${num(b.bets)} bets`, v: b.roi, lo: b.roi_ci95?.[0], hi: b.roi_ci95?.[1],
    readout: `<b>CLV ${esc(cutLabel(b))}</b> (average ${signedPct(b.clv)}): ${num(b.bets)} bets, ROI ${signedPct(b.roi)}${rngText(b.roi_ci95)}; won ${pct(b.win_rate, 1)} vs ${pct(b.breakeven, 1)} needed.`,
  }));
  if (!rows.length) return "";
  const t = bandTrend(bands, "clv", "roi");
  const words = !t ? "" : t.dir > 0 ? `Yes: the better the price against the close, the better the return (ROI ${signedPct(t.first.roi)} in the worst CLV band, ${signedPct(t.last.roi)} in the best).`
    : t.dir < 0 ? `Not here: the return fell as CLV rose (ROI ${signedPct(t.first.roi)} to ${signedPct(t.last.roi)}).`
      : `No clear pattern: ROI ${signedPct(t.first.roi)} in the worst CLV band, ${signedPct(t.last.roi)} in the best.`;
  return `<details class="fold mk-fold band-fold"><summary>Does beating the close predict profit?</summary>
    <p class="note">${words} Each row groups the bets by their CLV; dot = the return, bar = its 95% range.</p>
    ${rangeChart(id, rows, (v) => signedPct(v, 0))}</details>`;
}
// The three together for one strategy and competition (all = `all`).
function backtestBandsHtml(bt, part, key, lg) {
  const ob = part?.by_odds_band, eb = part?.by_edge_band, cb = part?.by_clv_band;
  if (!ob?.length && !eb?.length) return "";
  const lean = key === "lean", sfx = `${key}-${lg || "all"}`;
  const close = isClose(part);
  const oddsTable = bandTable(["Odds", "Bets", "Won vs needed", "ROI, 95% range"], ob.map((b) => `<tr><td>${esc(oddsBandLabel(b))}</td><td>${num(b.bets)}</td><td>${b.bets ? `${pct(b.win_rate, 1)} vs ${pct(b.breakeven, 1)}` : "no bets"}</td>${b.bets ? rngCell(b.roi, b.roi_ci95) : "<td>–</td>"}</tr>`));
  const edgeRows = (eb || []).filter((b) => b.bets);
  const edgeTable = edgeRows.length ? bandTable(["Claimed", ...(lean ? ["σ"] : []), "CLV, 95% range", "ROI, 95% range", "Bets"], edgeRows.map((b) => `<tr><td>${signedPct(b.claimed)}<span class="meta">${esc(cutLabel(b))}</span></td>${lean ? `<td>${isNum(b.z) ? b.z.toFixed(2) : "–"}</td>` : ""}${rngCell(b.clv, b.clv_ci95)}${rngCell(b.roi, b.roi_ci95)}<td>${num(b.bets)}</td></tr>`)) : "";
  const verdict = edgeVerdict(eb || []);
  const cuts = bt.bands || {};
  return `
    <div class="section-title">Where the bets sit on the odds</div>
    ${ob?.length ? oddsBandChart(`ob-${sfx}`, ob) : ""}
    <p class="note">Bar = the share of ${lg ? "this competition's" : "all"} bets in each odds band; dot = their CLV against ${close ? "Pinnacle's close at the same moment" : "Pinnacle's close"}, bar = its 95% range, line = 0. The return's ranges are too wide to plot, so they sit in the table.</p>
    ${ob?.length ? oddsTable : ""}
    <div class="section-title">Claimed edge vs what the price did</div>
    ${verdict ? `<div class="verdict ${/worse/.test(verdict) ? "neg" : "mid"}">${esc(verdict)}</div>` : ""}
    ${edgeClvChart(`ce-${sfx}`, eb || [], lean)}
    <p class="note">Each point is a fifth of the pooled bets, grouped by the edge ${lean ? "the blend" : "the model"} claimed when it bet${cuts.edge_from ? ` (worked out as ${esc(cuts.edge_from.replace(/\s*\(.*\)\s*$/, ""))})` : ""}. If the claim were right, the points would sit on the dashed line; a real edge would at least lift CLV as the claim grows.</p>
    ${edgeTable}
    ${cb?.length ? clvProfitFold(`cb-${sfx}`, cb) : ""}
    ${cuts.note ? howFold(cuts.note) : ""}`;
}
const num = (v) => (isNum(v) ? v.toLocaleString() : "–");
function lbLeagueDetails(l) {
  const close = isClose(l);
  const cov = (l.coverage || []).map((c) => close
    ? `<tr><td>${esc(c.label || seasonName(c.season))}</td><td>${num(c.matches)}</td><td>${num(c.model)}</td><td>${num(c.draftkings)}</td><td>${num(c.pinnacle)}</td><td>${num(c.candidates)}</td></tr>`
    : `<tr><td>${esc(c.label || seasonName(c.season))}</td><td>${num(c.matches)}</td><td>${isNum(c.early_share) ? pct(c.early_share) : "–"}</td><td>${isNum(c.close_share) ? pct(c.close_share) : "–"}</td><td>${c.included ? "Yes" : "No"}</td></tr>`).join("");
  const head = close
    ? "<th></th><th>Played</th><th>Model</th><th>DK</th><th>PIN</th><th>Bet on</th>"
    : "<th></th><th>Matches</th><th>Early</th><th>Close</th><th>Used</th>";
  const bits = [
    isNum(l.training_matches) && l.training_matches ? `${l.training_matches.toLocaleString()} matches to fit on` : "",
    isNum(l.min_rows) ? `the blend needs ${l.min_rows.toLocaleString()} before its first fit` : "",
    isNum(l.fits) && l.fits ? `${l.fits} fit${l.fits === 1 ? "" : "s"} (refitted every 28 days)` : "",
    isNum(l.scored) ? `${l.scored.toLocaleString()} match${l.scored === 1 ? "" : "es"} after a fit` : "",
    l.sigma ? (l.sigma.min === l.sigma.max ? `σ ${ptsText(l.sigma.min)}` : `σ ${ptsText(l.sigma.min)} to ${ptsText(l.sigma.max)}`) : "",
    isNum(l.candidates) ? `${l.candidates.toLocaleString()} bettable matches` : "",
    l.seasons_excluded?.length ? `left out (${close ? "no bettable matches" : "too few Pinnacle prices"}): ${esc(l.seasons_excluded.join(", "))}` : "",
  ].filter(Boolean);
  const what = close ? "Played = finished with a 90-minute score; Model = clubs the model covers (not low-data); DK and PIN = with DraftKings' or Pinnacle's full close; Bet on = model and DraftKings' close both." : "";
  return `<details class="fold mk-fold"><summary>Seasons and fits</summary>
    ${bits.length ? `<p class="note">${bits.join(", ").replace(/^./, (c) => c.toUpperCase())}.</p>` : ""}
    ${cov ? `<div class="card" style="padding:8px 14px"><table class="mkts mk-table"><thead><tr>${head}</tr></thead><tbody>${cov}</tbody></table></div>` : ""}
    ${what ? `<p class="note">${what}</p>` : ""}
  </details>`;
}
// Blend Lean's live trades of one driver: their trades, and the strategy's by_driver
// summary for that group (a count only once a competition is also chosen: by_driver isn't
// split by league).
function driverSet(set, st, drv, lg) {
  const trades = (set.trades || []).filter((t) => (driverOf(t) || "unknown") === drv);
  const summary = !lg && st.by_driver?.[drv] ? st.by_driver[drv] : { trades: trades.length, open: trades.filter((t) => t.status === "open").length, settled: trades.filter((t) => t.status === "won" || t.status === "lost").length, void: trades.filter((t) => t.status === "void").length, counts_only: true };
  return { ...set, trades, summary, breakdowns: {}, by_league: null };
}
// Live: market-driven vs model-driven, side by side, with a filter for the rest of the view.
function leanDriverPanel(st, lg) {
  const by = st.by_driver || {};
  const keys = ["market", "model", ...(by.unknown ? ["unknown"] : [])];
  const cell = (k, f) => `<td>${f(by[k] || {})}</td>`;
  const row = (label, f) => `<tr><td>${label}</td>${keys.map((k) => cell(k, f)).join("")}</tr>`;
  const table = `
    <div class="card" style="padding:8px 14px"><table class="mkts mk-table drv-table">
      <thead><tr><th></th>${keys.map((k) => `<th>${esc(DRIVER_NAME[k])}</th>`).join("")}</tr></thead>
      <tbody>
        ${row("Trades", (g) => `${g.trades || 0}<span class="mk-range">${g.open || 0} open · ${g.settled || 0} settled</span>`)}
        ${row("Settled profit", (g) => `<span class="${plClass(g.profit)}">${usd(g.profit || 0)}</span>`)}
        ${row("ROI", (g) => signedPct(g.roi))}
        ${row("Win rate", (g) => (g.win_rate == null ? "–" : `${pct(g.win_rate)}<span class="mk-range">needs ${pct(g.breakeven)}</span>`))}
        ${row("CLV vs DraftKings' close", (g) => `<span class="${plClass(g.clv_dk)}">${signedPct(g.clv_dk)}</span>`)}
      </tbody>
    </table></div>`;
  const opts = [["", "All Lean"], ...keys.map((k) => [k, DRIVER_NAME[k]])];
  const note = driverNote();
  return `
    <div class="section-title">What carried each pick</div>
    ${table}
    <p class="note">${lg ? "Every competition: this split isn't kept per competition. " : ""}Few trades so far, so these are counts, not a verdict.</p>
    ${note ? `<details class="fold mk-fold"><summary>Market-driven or model-driven?</summary><p class="note">${esc(note)}</p></details>` : ""}
    <div class="segmented small-seg drv-seg" role="group" aria-label="Which Lean picks">
      ${opts.map(([k, l]) => `<button data-pfdriver="${k}" class="${state.pfDriver === k ? "on" : ""}" aria-pressed="${state.pfDriver === k}">${esc(l)}</button>`).join("")}
    </div>`;
}
// " (Premier League 0.6 points, La Liga 1.9 points)" from the Lean rule's sigma per league.
function sigmaList(by) {
  const items = Object.entries(by || {}).filter(([, v]) => v != null).map(([c, v]) => `${esc(leagueName(c))} ${ptsText(v)}`);
  return items.length ? ` (${items.join(", ")})` : "";
}
function pfClassic(topIn) {
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
  const retiredLinks = "";
  const toggle = `
    <div class="segmented small-seg" role="group" aria-label="Which trades">
      ${[["live", "Live paper"], ["backtest", "Backtest"]].map(([k, l]) => `<button data-pf="${k}" class="${state.pfView === k ? "on" : ""}" aria-pressed="${state.pfView === k}">${l}</button>`).join("")}
    </div>`;
  // Moneyline's live record per match strategy (Blend Lean first, then Model 12%): each its
  // own rule, summary, breakdowns, by_league; trade_ids point into the combined trades.
  // The switch shows on Backtest too: Blend Lean's backtest is its own replay at Pinnacle
  // (portfolio.strategy_backtests.lean); Model 12%'s is the DraftKings backtest.
  const strats = cur.id === "moneyline" ? (cur.live?.strategies || []).filter((x) => !x.variant_of) : [];
  const strat = strats.find((x) => x.key === state.pfStrat) || strats[0] || null;
  const leanBt = state.pfView === "backtest" && strat?.key === "lean";
  const fullSet = state.pfView === "live" && strat ? stratSet(pfSet(), strat) : pfSet();
  const stratSwitch = strats.length > 1 ? `
    <div class="segmented small-seg strat-seg" role="group" aria-label="Match strategy">
      ${strats.map((x) => `<button data-pfstrat="${esc(x.key)}" class="${x.key === strat.key ? "on" : ""}" aria-pressed="${x.key === strat.key}">${esc(x.label)}</button>`).join("")}
    </div>` : "";
  // Moneyline and Team corners: filtered by competition (by_league summaries from paper.py).
  const byComp = cur.id === "moneyline" || cur.id === "corners";
  const research = state.pfView === "backtest" && fullSet?.kind === "research";
  const lg = byComp && !research ? leagueOn() : "";
  const lgSet = fullSet && lg ? leagueSet(fullSet, lg) : fullSet;
  // Blend Lean live: narrowed to market- or model-driven picks when one is chosen.
  const leanLive = state.pfView === "live" && strat?.key === "lean";
  const drv = leanLive ? state.pfDriver : "";
  const set = drv && lgSet ? driverSet(lgSet, strat, drv, lg) : lgSet;
  const top = topIn ?? `${switcher}${head}${byComp && !research ? leagueFilter() : ""}${toggle}${stratSwitch}`;
  if (research) return `${top}${researchBacktestHtml(fullSet)}${retiredLinks}`;
  if (leanBt) return `${top}${leanBacktestHtml(state.data.portfolio?.strategy_backtests?.lean, lg)}${retiredLinks}`;
  // Model 12%: the per-league replay (strategy_backtests.edge12) under All and every
  // league but the Premier League, whose body stays the DraftKings backtest with the
  // Pinnacle replay added below it (both labelled).
  const e12 = state.data.portfolio?.strategy_backtests?.edge12;
  const e12Bt = state.pfView === "backtest" && cur.id === "moneyline" && strat?.key === "edge12";
  if (e12Bt && lg !== "E0") {
    const body = e12?.available || !lg
      ? strategyBacktestHtml(e12, lg, "edge12")
      : `<div class="empty">No Model 12% backtest for ${esc(leagueName(lg))} yet. Its backtest uses DraftKings' historical prices, which we have for the Premier League only. Choose Premier League or All to see it; the live paper trades in ${esc(leagueName(lg))} are under Live paper.</div>`;
    return `${top}${body}${retiredLinks}`;
  }
  const e12E0 = e12Bt && lg === "E0" && e12?.available && e12.leagues?.E0 && !e12.leagues.E0.error
    ? `<div class="section-title">Replay at Pinnacle's early prices, Premier League ($1 a bet)</div>${strategyBacktestHtml(e12, "E0", "edge12", { brief: true })}` : "";
  const dkLabel = e12Bt && lg === "E0" ? '<div class="section-title">DraftKings backtest, Premier League ($10 a bet)</div>' : "";
  const isMoneyline = cur.id === "moneyline";
  const ruleNote = cur.id === "corners" && state.pfView === "live"
    ? `<div class="explain live-test"><b>Live test: fixed ${pct(set?.rule?.threshold ?? PAPER_EDGE)} on model (f) against Pinnacle.</b> A $${set?.rule?.stake ?? 10} paper trade opens on a team's corner line when the model's chance shows at least ${aPct(set?.rule?.threshold ?? PAPER_EDGE)} edge against Pinnacle's price, at most one per team and line, in every competition with team corners (League One and League Two since 24 Oct, where model (f) has never been tested, so treat their picks as untested). On a whole line (say 5), exactly 5 corners is a push: the stake comes back.</div>`
    : strat?.key === "lean"
    ? `<div class="explain live-test"><b>Blend Lean (1σ+), listed first.</b> A $${strat.rule?.stake ?? 10} paper trade opens on the side whose blended chance (model mixed with DraftKings' price) beats the price's break-even by at least one σ, Strong at 2σ, one per match, in every competition. σ is how far the blend usually strays from the market in that competition${sigmaList(strat.rule?.sigma)}. Be warned: most Lean picks come from the part of the blend that sharpens DraftKings' own price (it makes favourites a little likelier and long shots less likely, a pattern fitted on Pinnacle's prices), not from the model, which gets little weight. With that sharpening switched off, only 2 of the 12 matches that reached 1σ from 1 to 10 Oct would still have qualified. Lean hasn't proven itself, and neither has the baseball app's Lean tier it comes from. Expect few picks, mostly outside the Premier League, and judge it on closing line value rather than a few dozen results.</div>`
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
    const empty = strat?.key === "lean"
      ? `No Blend Lean trades yet. One opens when a side's blended chance beats DraftKings' break-even by 1σ.${strat.rule?.source === "none" && strat.rule?.note ? ` ${esc(strat.rule.note)}` : ""}`
      : cur.id === "corners" && state.pfView === "live"
      ? "No corner trades yet. One opens when a team's corner line shows a 12% edge against Pinnacle's price."
      : cur.status === "testing"
      ? "No trades yet. They start once this strategy passes its tests."
      : cur.status === "retired" && state.pfView === "live"
        ? "No live trades: this strategy was retired before any opened. Its testing history is under Backtest."
        : state.pfView === "live"
          ? (rule.threshold == null ? "No new paper trades: no minimum edge has beaten the market in past bets." : `No paper trades yet. One opens when a pick reaches a ${pct(rule.threshold)} edge.`)
          : set ? "The backtest found no trades at this threshold." : "No backtest yet.";
    return `${top}${ruleNote}${leanLive ? leanDriverPanel(strat, lg) : ""}${banner}<div class="empty">${drv ? `No ${DRIVER_NAME[drv].toLowerCase()} Lean trades yet.` : empty}</div>${state.pfView === "backtest" && set ? pfBacktestExtras(set) : ""}${e12E0}${retiredLinks}${foot}`;
  }
  const settled = trades.filter((t) => t.status === "won" || t.status === "lost").slice().reverse()
    .map((t) => [t.kickoff.slice(0, 10), t.home, t.away, t.market, t.odds, t.edge, tradeClv(t), t.profit]);
  const ci = s.roi_ci95 ? `<p class="note">95% interval on ROI: ${signedPct(s.roi_ci95[0])} to ${signedPct(s.roi_ci95[1])} (resampling match weeks). Win rate ${pct(s.win_rate, 1)} vs ${pct(s.breakeven, 1)} needed to break even.</p>` : "";
  return `
    ${top}
    ${ruleNote}
    ${leanLive ? leanDriverPanel(strat, lg) : ""}
    ${banner}
    ${dkLabel}
    ${pfTiles(s, trades)}
    ${ci}
    <div class="section-title">Running profit ($)</div>
    <div class="card">${profitChart(settled, "$") || '<p class="muted">Nothing settled yet.</p>'}</div>
    ${state.pfView === "live" ? pfOpen(trades) : ""}
    ${impliedVsRealizedHtml(trades)}
    ${pfSettled(trades)}
    ${pfBreakdowns(set.breakdowns || {})}
    ${state.pfView === "backtest" ? edgeBucketsHtml(cur.id, lg) + pfBacktestExtras(set) : ""}
    ${e12E0}
    ${retiredLinks}
    ${foot}`;
}


// ---------- Portfolio → Model: each strategy's equation and how it lined up with results ----------
// (owner, 22 Oct). portfolio.models (equations.py) gives each model's equation, its terms
// in plain English and the fitted numbers per league; the calibration blocks (calibration.py)
// come from the backtest files (every outcome, and the strategy's own bets), each live
// strategy section (settled paper trades) and, for corners, every logged quote.
const MODEL_KEYS = { lean: ["blend", "match"], edge12: ["match"], pinnacle: ["corners_f"], draftkings: ["corners_f"] };
const SERIES_NAME = { model: "Model", blend: "Blend", price: "Price" };
const num2 = (v, d = 2) => (isNum(v) ? `${v < 0 ? "−" : ""}${Math.abs(v).toFixed(d)}` : "–");
const sgn2 = (v, d = 2) => (isNum(v) ? `${v < 0 ? "−" : "+"}${Math.abs(v).toFixed(d)}` : "–");
const pctRange = (lo, hi) => (isNum(lo) && isNum(hi) ? `${pct(lo)}–${pct(hi)}` : "");
// The recalibration slope in words: 1 = right; below 1 the chances are too spread out.
function slopeWords(c) {
  if (!c || !isNum(c.slope)) return "too few results to fit a slope";
  const s = c.slope, ci = c.slope_ci95;
  const word = s < 0.9 ? "over-confident: its chances are too far from 50%" : s < 0.97 ? "a bit over-confident" : s <= 1.03 ? "about right" : s <= 1.1 ? "a bit timid" : "timid: its chances sit too close to 50%";
  const range = ci && isNum(ci[0]) ? ` (range ${num2(ci[0])} to ${num2(ci[1])}${ci[0] <= 1 && ci[1] >= 1 ? ", which includes 1" : ""})` : "";
  return `slope ${num2(s)}${range}: ${word}`;
}
function modelFor(key, lg) {
  const m = state.data.portfolio?.models?.[key];
  if (!m) return { m: null, part: null };
  return { m, part: lg ? m.leagues?.[lg] || null : null };
}
// The fitted numbers of one league, each in words.
function modelNumbers(key, f) {
  if (!f) return [];
  if (f.error) return [["Fit", esc(f.error)]];
  if (key === "match") {
    const ha = f.home_adv;
    return [
      ["Baseline", isNum(f.intercept) ? `${num2(Math.exp(f.intercept))} goals a game for an average away side (log ${num2(f.intercept, 3)})` : "–"],
      ["Home advantage", isNum(ha) ? `${sgn2(ha)} on the log scale: about ${pct(Math.exp(ha) - 1)} more goals at home` : "–"],
      ["ρ (low-score correction)", isNum(f.rho) ? `${num2(f.rho, 3)}${f.rho < 0 ? ": 0-0 and 1-1 a little likelier than plain Poisson" : f.rho > 0 ? ": 0-0 and 1-1 a little less likely" : ""}` : "–"],
      ["Time weight", isNum(f.half_life_days) ? `a match counts half after ${f.half_life_days} days (ξ ${num2(f.xi, 4)} a day)` : "–"],
      ["xG weight", isNum(f.xg_weight) ? (f.xg_weight ? `${pct(f.xg_weight)} of each match's goals come from xG` : "0: goals only (no xG for this competition)") : "–"],
      ...(f.ridge ? [["Ridge", `${num2(f.ridge)}: ratings pulled toward 0 (the cross-league fit)`]] : []),
      ["Fitted on", `${num(f.matches)} matches, ${num(f.teams)} teams${f.fit_from ? `, ${esc(shortDate(f.fit_from))} to ${esc(shortDate(f.fit_to))}` : ""}`],
    ];
  }
  if (key === "blend") {
    const c = f.coef || {};
    return [
      ["Price weight b", isNum(c.b) ? `${num2(c.b)}${c.b > 1.02 ? " (above 1: sharpens the price, favourites up, long shots down)" : c.b < 0.98 ? " (below 1: flattens the price)" : " (about 1: keeps the price as it is)"}` : "–"],
      ["Model weight c", isNum(c.c) ? `${sgn2(c.c)}${Math.abs(c.c) < 0.05 ? " (almost no weight on the model)" : c.c < 0 ? " (leans against the model)" : " (some weight on the model)"}` : "–"],
      ["Draw and away tilts", isNum(c.a_draw) ? `a_draw ${sgn2(c.a_draw, 3)}, a_away ${sgn2(c.a_away, 3)}` : "–"],
      ["σ", isNum(f.sigma) ? `${ptsText(f.sigma)}: how far the blend usually strays from the price` : "–"],
      ["Fitted on", `${num(f.matches)} matches${f.fit_from ? `, ${esc(shortDate(f.fit_from))} to ${esc(shortDate(f.fit_to))}` : ""}${f.fit_date ? ` (fit of ${esc(shortDate(f.fit_date))})` : ""}`],
    ];
  }
  return [
    ["League average", isNum(f.mean_home) ? `${num2(f.mean_home)} corners for the home side, ${num2(f.mean_away)} for the away side` : "–"],
    ["Shrinkage", isNum(f.k_for) ? `k_for ${num2(f.k_for, 1)}, k_against ${num2(f.k_against, 1)} pseudo-matches toward the average` : "–"],
    ["Spread (NB2 α)", isNum(f.alpha_home) ? `home ${num2(f.alpha_home, 3)}, away ${num2(f.alpha_away, 3)}${Math.max(f.alpha_home, f.alpha_away) < 0.05 ? ": close to Poisson" : ": a little wider than Poisson"}` : "not in this update"],
    ["Fitted on", `${num(f.matches)} matches${f.window_from ? `, ${esc(shortDate(f.window_from))} to ${esc(shortDate(f.window_to))}` : ""}`],
  ];
}
// Under All: one row per league with the numbers that matter most.
function modelTable(key, m) {
  const rows = Object.entries(m.leagues || {});
  if (!rows.length) return `<p class="note">No fitted numbers for this model in this update.</p>`;
  const cols = key === "match" ? ["Home adv.", "ρ", "xG wt", "Matches"] : key === "blend" ? ["b", "c", "σ", "Matches"] : ["Avg H / A", "k for / ag.", "α H / A", "Matches"];
  const cells = (f) => {
    if (f.error) return `<td colspan="4" class="muted">fit failed</td>`;
    if (key === "match") return `<td>${sgn2(f.home_adv)}</td><td>${num2(f.rho, 3)}</td><td>${isNum(f.xg_weight) ? pct(f.xg_weight) : "–"}</td><td>${num(f.matches)}</td>`;
    if (key === "blend") return `<td>${num2(f.coef?.b)}</td><td>${sgn2(f.coef?.c)}</td><td>${isNum(f.sigma) ? (f.sigma * 100).toFixed(1) : "–"}</td><td>${num(f.matches)}</td>`;
    return `<td>${num2(f.mean_home, 1)} / ${num2(f.mean_away, 1)}</td><td>${num2(f.k_for, 1)} / ${num2(f.k_against, 1)}</td><td>${num2(f.alpha_home, 2)} / ${num2(f.alpha_away, 2)}</td><td>${num(f.matches)}</td>`;
  };
  return `<div class="card" style="padding:8px 14px"><table class="mkts mk-table model-table"><thead><tr><th></th>${cols.map((c) => `<th>${c}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(([c, f]) => `<tr><td>${esc(leagueShort(c))}</td>${cells(f)}</tr>`).join("")}</tbody></table></div>
    ${key === "blend" ? '<p class="note">σ in points (percentage points of chance).</p>' : ""}`;
}
function equationCard(key, lg) {
  const { m, part } = modelFor(key, lg);
  if (!m) return `<div class="explain">This model's equation isn't in this update.</div>`;
  if (m.error) return `<div class="explain">The model view couldn't be built in this update (${esc(m.error)}).</div>`;
  const terms = Object.entries(m.terms || {}).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
  const nums = lg
    ? (part ? `<dl class="model-nums">${modelNumbers(key, part).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>` : `<p class="note">No ${esc(m.name || "model")} fit for ${esc(leagueName(lg))} in this update.</p>`)
    : modelTable(key, m);
  return `
    <div class="section-title">${esc(m.name || key)}</div>
    <div class="card model-card">
      <div class="eqn">${(m.equation || []).map((l) => `<div>${esc(l)}</div>`).join("")}</div>
      <details class="fold mk-fold"><summary>What each term means</summary><dl class="model-terms">${terms}</dl>${m.note ? `<p class="note">${esc(m.note)}</p>` : ""}</details>
    </div>
    <div class="section-title small-title">${lg ? `Fitted for ${esc(leagueName(lg))}` : "Fitted per competition"}</div>
    ${nums}`;
}
// The blend's shape: p_bet(home) against the market's fair home chance, with the draw and
// away sharing the rest in a fixed 45 : 55 ratio, for three model views of the home side.
function blendHome(coef, p, dm) {
  const share = [0.45, 0.55];
  const price = [p, (1 - p) * share[0], (1 - p) * share[1]];
  const mh = Math.min(0.97, Math.max(0.03, p + dm));
  const model = [mh, (1 - mh) * share[0], (1 - mh) * share[1]];
  const a = [0, coef.a_draw || 0, coef.a_away || 0];
  const s = price.map((pk, k) => a[k] + coef.b * Math.log(pk) + coef.c * Math.log(model[k]));
  const mx = Math.max(...s);
  const e = s.map((v) => Math.exp(v - mx));
  return e[0] / (e[0] + e[1] + e[2]);
}
function shapeChart(lg) {
  const code = lg || "E0";
  const f = state.data.portfolio?.models?.blend?.leagues?.[code];
  const coef = f?.coef;
  if (!coef || !isNum(coef.b)) return `<p class="note">No blend fit for ${esc(leagueName(code))}, so there is no shape to draw.</p>`;
  const W = 320, H = 250, L = 38, R = 12, T = 10, B = 30;
  const sx = (v) => L + v * (W - L - R), sy = (v) => T + (1 - v) * (H - T - B);
  const xs = Array.from({ length: 37 }, (_, i) => 0.04 + i * 0.025);
  const curve = (dm) => xs.map((p, i) => `${i ? "L" : "M"}${sx(p).toFixed(1)},${sy(blendHome(coef, p, dm)).toFixed(1)}`).join(" ");
  const ticks = [0, 0.25, 0.5, 0.75, 1];
  const grid = ticks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${sy(v)}" y2="${sy(v)}"/><text x="${L - 4}" y="${sy(v) + 3}" text-anchor="end">${pct(v)}</text><text x="${sx(v)}" y="${H - 14}" text-anchor="middle">${pct(v)}</text>`).join("");
  const at = [0.2, 0.5, 0.8].map((p) => `${pct(p)} → ${pct(blendHome(coef, p, 0), 1)}`).join(", ");
  // Scrub stops: the market's chance in 1-point steps, 4% to 94% (the drawn range).
  const steps = Array.from({ length: 91 }, (_, i) => (4 + i) / 100);
  const DMS = [0, 0.1, -0.1];
  const id = `shape-${code}`;
  scrubCharts[id] = {
    readoutId: `rc-readout-${id}`,
    xs: steps.map(sx),
    points: (i) => DMS.map((dm) => ({ x: sx(steps[i]), y: sy(blendHome(coef, steps[i], dm)) })),
    readout: (i) => `<b>Market ${pct(steps[i])}</b>: blend ${pct(blendHome(coef, steps[i], 0), 1)} with the model agreeing, ${pct(blendHome(coef, steps[i], 0.1), 1)} with the model 10 points higher, ${pct(blendHome(coef, steps[i], -0.1), 1)} with it 10 points lower.`,
  };
  return `
    <div class="section-title">The blend's shape${lg ? "" : ", Premier League"}</div>
    <div class="chart curve shape-chart scrub" ${scrubAttrs(id, steps.length, "The blend's shape: drag or use the arrow keys")}>
      <div class="eb-legend"><span><i class="ln-key diag"></i>Blend = market</span><span><i class="ln-key same"></i>Model agrees</span><span><i class="ln-key up"></i>Model 10 pts higher</span><span><i class="ln-key down"></i>Model 10 pts lower</span></div>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Blend's home chance against the market's: ${esc(at)}">
        <g class="grid">${grid}</g>
        <line class="diag" x1="${sx(0)}" y1="${sy(0)}" x2="${sx(1)}" y2="${sy(1)}"/>
        <path class="shape up" d="${curve(0.1)}"/>
        <path class="shape down" d="${curve(-0.1)}"/>
        <path class="shape same" d="${curve(0)}"/>
        <line class="cursor" x1="0" x2="0" y1="${T}" y2="${H - B}" style="display:none"/>
        ${["same", "up", "down"].map((k) => `<circle class="cdot ${k}" r="4" cx="0" cy="0" style="display:none"/>`).join("")}
        ${scrubPad(W, H)}
        <text class="xlab" x="${(L + W - R) / 2}" y="${H - 2}" text-anchor="middle">Market's fair home chance</text>
      </svg>
      <div class="eb-readout" id="rc-readout-${id}" aria-live="polite">With the model agreeing with the market, ${at}. Point at or drag along the chart for any price.</div>
    </div>
    <p class="note">Drawn for the home side, with the draw and away sharing the rest of the chance 45 : 55 (the model's split too). Above the diagonal the blend rates the home side higher than the market. ${coef.b > 1.02 ? `With b = ${num2(coef.b)} above 1 the solid line bends away from the diagonal: favourites get a little likelier and long shots a little less likely, whatever the model says.` : `With b = ${num2(coef.b)} the solid line stays close to the diagonal.`} The gap between the dashed lines is all the model can move it${isNum(coef.c) ? ` (c = ${sgn2(coef.c)})` : ""}.${lg ? "" : " Choose a competition above to see its own fit."}</p>`;
}
// Model (f)'s shape: the chance a team goes over 3.5, 4.5 and 5.5 corners against its
// expected corners, from NB2 with the league's home dispersion (alpha_home). Scrubbable.
function nbOver(mean, alpha, line) {
  const k = Math.floor(line); // P(X > line) = 1 - P(X <= k)
  let pmf, cdf = 0;
  if (!(alpha > 1e-6)) {
    pmf = Math.exp(-mean);
    for (let j = 0; j <= k; j++) { cdf += pmf; pmf *= mean / (j + 1); }
  } else {
    const r = 1 / alpha, q = r / (r + mean);
    pmf = Math.pow(q, r);
    for (let j = 0; j <= k; j++) { cdf += pmf; pmf *= ((j + r) / (j + 1)) * (1 - q); }
  }
  return Math.max(0, Math.min(1, 1 - cdf));
}
const CORNER_LINES = [3.5, 4.5, 5.5];
function cornerShapeChart(lg) {
  const code = lg || "E0";
  const f = state.data.portfolio?.models?.corners_f?.leagues?.[code];
  if (!f || f.error) return `<p class="note">No model (f) fit for ${esc(leagueName(code))}, so there is no shape to draw.</p>`;
  const alpha = isNum(f.alpha_home) ? f.alpha_home : 0;
  const W = 320, H = 230, L = 38, R = 12, T = 10, B = 30, X0 = 2, X1 = 9;
  const sx = (m) => L + ((m - X0) / (X1 - X0)) * (W - L - R), sy = (v) => T + (1 - v) * (H - T - B);
  const ms = Array.from({ length: 57 }, (_, i) => X0 + i * 0.125);
  const curve = (line) => ms.map((m, i) => `${i ? "L" : "M"}${sx(m).toFixed(1)},${sy(nbOver(m, alpha, line)).toFixed(1)}`).join(" ");
  const yt = [0, 0.25, 0.5, 0.75, 1], xt = [2, 3, 4, 5, 6, 7, 8, 9];
  const grid = yt.map((v) => `<line x1="${L}" x2="${W - R}" y1="${sy(v)}" y2="${sy(v)}"/><text x="${L - 4}" y="${sy(v) + 3}" text-anchor="end">${pct(v)}</text>`).join("")
    + xt.map((m) => `<text x="${sx(m)}" y="${H - 14}" text-anchor="middle">${m}</text>`).join("");
  const steps = Array.from({ length: 29 }, (_, i) => X0 + i * 0.25);
  const id = `cshape-${code}`;
  scrubCharts[id] = {
    readoutId: `rc-readout-${id}`,
    xs: steps.map(sx),
    points: (i) => CORNER_LINES.map((line) => ({ x: sx(steps[i]), y: sy(nbOver(steps[i], alpha, line)) })),
    readout: (i) => `<b>Expected ${steps[i].toFixed(2)} corners</b>: over 3.5 ${pct(nbOver(steps[i], alpha, 3.5))}, over 4.5 ${pct(nbOver(steps[i], alpha, 4.5))}, over 5.5 ${pct(nbOver(steps[i], alpha, 5.5))}.`,
  };
  const mid = isNum(f.mean_home) ? f.mean_home : 5;
  const at = (line) => pct(nbOver(mid, alpha, line));
  return `
    <div class="section-title">Model (f)'s shape${lg ? "" : ", Premier League"}</div>
    <div class="chart curve shape-chart cshape scrub" ${scrubAttrs(id, steps.length, "Model (f)'s shape: drag or use the arrow keys")}>
      <div class="eb-legend"><span><i class="ln-key l35"></i>Over 3.5</span><span><i class="ln-key l45"></i>Over 4.5</span><span><i class="ln-key l55"></i>Over 5.5</span></div>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Chance of over 3.5, 4.5 and 5.5 corners against expected corners">
        <g class="grid">${grid}</g>
        ${CORNER_LINES.map((line, k) => `<path class="shape l${k}" d="${curve(line)}"/>`).join("")}
        <line class="cursor" x1="0" x2="0" y1="${T}" y2="${H - B}" style="display:none"/>
        ${CORNER_LINES.map((_, k) => `<circle class="cdot l${k}" r="4" cx="0" cy="0" style="display:none"/>`).join("")}
        ${scrubPad(W, H)}
        <text class="xlab" x="${(L + W - R) / 2}" y="${H - 2}" text-anchor="middle">Team's expected corners</text>
      </svg>
      <div class="eb-readout" id="rc-readout-${id}" aria-live="polite">At the league's average for a home side (${num2(mid, 1)} corners): over 3.5 ${at(3.5)}, over 4.5 ${at(4.5)}, over 5.5 ${at(5.5)}. Drag along the chart for other teams.</div>
    </div>
    <p class="note">Each curve is the chance a team's corners go over that line, given how many it is expected to take (its for-ratio times the opponent's against-ratio times the league average). Drawn with the home side's spread (NB2 α ${num2(alpha, 3)}${isNum(f.alpha_away) ? `; away α ${num2(f.alpha_away, 3)}` : ""}): the bigger α, the flatter the curves, so the model is less sure either way. The curves are steepest where expected corners sit near the line, so a small change in a team's rating moves the chance most there.${lg ? "" : " Choose a competition above to see its own fit."}</p>`;
}
// (a) Reliability over every outcome: x = the series' chance, y = how often it happened.
function reliabilityChart(id, all, names = SERIES_NAME, unit = "outcomes") {
  const series = ["model", "blend", "price"].filter((k) => all?.[k]?.bins?.length);
  if (!series.length) return "";
  const pts = series.flatMap((k) => all[k].bins.map((b) => ({ k, b })));
  const top = Math.min(1, Math.max(0.4, Math.ceil((Math.max(...pts.flatMap(({ b }) => [b.predicted, b.realized_hi ?? b.realized])) + 0.02) * 10) / 10));
  const W = 320, H = 260, L = 38, R = 12, T = 10, B = 30;
  const sx = (v) => L + (v / top) * (W - L - R), sy = (v) => T + (1 - v / top) * (H - T - B);
  const step = top <= 0.5 ? 0.1 : 0.2;
  const ticks = [];
  for (let v = 0; v <= top + 1e-9; v += step) ticks.push(v);
  const grid = ticks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${sy(v)}" y2="${sy(v)}"/><text x="${L - 4}" y="${sy(v) + 3}" text-anchor="end">${pct(v)}</text><text x="${sx(v)}" y="${H - 14}" text-anchor="middle">${pct(v)}</text>`).join("");
  // One stop per bin of chances (lo–hi), holding every series' point in it, so one
  // readout compares them; a pointer picks the nearest point by x, then by distance.
  const binKey = (b) => `${b.lo}|${b.hi}`;
  const stops = [...new Set(pts.map(({ b }) => binKey(b)))].map((key) => pts.filter(({ b }) => binKey(b) === key))
    .sort((a, c) => a[0].b.lo - c[0].b.lo);
  const stopOf = new Map(stops.flatMap((g, i) => g.map((p) => [p, i])));
  const relLine = ({ k, b }) => `${names[k]} ${num(b.n)} ${unit}: said ${pct(b.predicted, 1)}, happened ${pct(b.realized, 1)}${isNum(b.realized_lo) ? ` (range ${pct(b.realized_lo, 1)} to ${pct(b.realized_hi, 1)})` : ""}`;
  scrubCharts[id] = {
    n: stops.length,
    readoutId: `rc-readout-${id}`,
    pick: (x, y) => {
      let best = 0, bd = Infinity;
      stops.forEach((g, i) => g.forEach(({ b }) => {
        const d = Math.abs(sx(b.predicted) - x) * 4 + Math.abs(sy(b.realized) - y);
        if (d < bd) { bd = d; best = i; }
      }));
      return best;
    },
    readout: (i) => `<b>Chances ${pctRange(stops[i][0].b.lo, stops[i][0].b.hi)}</b>: ${stops[i].map(relLine).join("; ")}.`,
  };
  const marks = pts.map((p) => {
    const { k, b } = p;
    const x = sx(b.predicted), y = sy(b.realized), st = stopOf.get(p);
    const range = isNum(b.realized_lo) ? `<line class="rng ${k}" data-stop="${st}" x1="${x}" x2="${x}" y1="${sy(b.realized_lo)}" y2="${sy(b.realized_hi)}"/>` : "";
    const mark = k === "price" ? `<line class="mk price" data-stop="${st}" x1="${x - 5}" x2="${x + 5}" y1="${y}" y2="${y}"/>` : `<circle class="mk ${k}" data-stop="${st}" cx="${x}" cy="${y}" r="${k === "model" ? 4 : 3.5}"/>`;
    return `${range}${mark}`;
  }).join("");
  const s = all[series[0]];
  return `
    <div class="chart curve rel-chart scrub" ${scrubAttrs(id, stops.length, "Predicted against happened: point at or drag across the chart, or use the arrow keys")}>
      <div class="eb-legend">${series.map((k) => `<span><i class="rel-key ${k}"></i>${names[k]}</span>`).join("")}<span><i class="ln-key diag"></i>Perfect</span></div>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Predicted chance against how often it happened">
        <g class="grid">${grid}</g>
        <line class="diag" x1="${sx(0)}" y1="${sy(0)}" x2="${sx(top)}" y2="${sy(top)}"/>
        ${marks}
        ${scrubPad(W, H)}
        <text class="xlab" x="${(L + W - R) / 2}" y="${H - 2}" text-anchor="middle">Predicted chance</text>
      </svg>
      <div class="eb-readout" id="rc-readout-${id}" aria-live="polite">${num(s.n)} ${unit}. Point at or drag across the chart for each group's numbers.</div>
    </div>
    <ul class="slopes">${series.map((k) => `<li><b>${names[k]}</b>: ${slopeWords(all[k])}. Brier ${num2(all[k].brier, 4)}, log loss ${num2(all[k].log_loss, 4)}.</li>`).join("")}</ul>`;
}
// (b)–(d) Per bin of the predicted chance: what it said (ring), the price's break-even
// (tick) and how often it won (dot with its range; blue when it won at least as often as
// the price implied, red when less).
function binRowsChart(id, cal, what) {
  const bins = (cal?.bins || []).filter((b) => b.n);
  if (!bins.length) return "";
  const vals = bins.flatMap((b) => [b.predicted, b.implied, b.realized_lo ?? b.realized, b.realized_hi ?? b.realized]).filter(isNum);
  const hi = Math.min(1, Math.ceil((Math.max(...vals) + 0.02) * 10) / 10);
  const lo = Math.max(0, Math.floor((Math.min(...vals) - 0.02) * 10) / 10);
  const x = (v) => ((v - lo) / (hi - lo || 1)) * 100;
  const step = hi - lo > 0.5 ? 0.2 : 0.1;
  const ticks = [];
  for (let v = Math.ceil(lo / step - 1e-9) * step; v <= hi + 1e-9; v += step) ticks.push(v);
  const binText = bins.map((b) => ({
    readout: `<b>Said ${pctRange(b.lo, b.hi)}</b>, ${num(b.n)} ${what}${b.few ? " (few: read loosely)" : ""}: said ${pct(b.predicted, 1)} on average${isNum(b.implied) ? `, the price's break-even ${pct(b.implied, 1)}` : ""}, won ${pct(b.realized, 1)}${isNum(b.realized_lo) ? ` (range ${pct(b.realized_lo, 0)} to ${pct(b.realized_hi, 0)})` : ""}.`,
  }));
  const body = bins.map((b, i) => {
    const beat = !isNum(b.implied) || b.realized >= b.implied;
    const tone = isNum(b.implied) ? (beat ? "pos" : "neg") : "mid";
    const range = isNum(b.realized_lo) ? `<span class="eb-range ${tone}" style="left:${x(b.realized_lo)}%;width:${Math.max(0.5, x(b.realized_hi) - x(b.realized_lo))}%"></span>` : "";
    const dot = (v, cls) => (isNum(v) ? `<span class="eb-dot ${cls}" style="left:${x(v)}%"></span>` : "");
    return `<div class="eb-row rc-row${b.few ? " few" : ""}" data-stop="${i}" data-rcrow="${i}" data-rc="${id}">
      <span class="eb-label">${pctRange(b.lo, b.hi)}<span class="meta">${num(b.n)} ${what}${b.few ? ` · few ${what}` : ""}</span></span>
      <span class="eb-plot">${range}${dot(b.implied, "imp")}${dot(b.predicted, "mod")}${dot(b.realized, `real ${tone}`)}</span>
    </div>`;
  }).join("");
  const axis = `<div class="eb-axis"><span></span><span class="eb-plot">${ticks.map((v) => `<span style="left:${x(v)}%">${pct(v)}</span>`).join("")}</span></div>`;
  scrubCharts[id] = { n: bins.length, rows: true, readoutId: `rc-readout-${id}`, readout: (i) => binText[i].readout };
  const sum = `All ${num(cal.n)} ${what}: said ${pct(cal.predicted, 1)}${isNum(cal.implied) ? `, the price's break-even ${pct(cal.implied, 1)}` : ""}, won ${pct(cal.realized, 1)}.`;
  return `
    <div class="card eb-card scrub-rows" ${scrubAttrs(id, bins.length, "Point at or drag across the rows, or use the arrow keys")}>
      <div class="eb-legend">${bins.some((b) => isNum(b.implied)) ? '<span><i class="eb-key imp"></i>Price\'s break-even</span>' : ""}<span><i class="eb-key mod"></i>What it said</span><span><i class="eb-key real pos"></i>Won (range)</span></div>
      ${body}
      ${axis}
      <div class="eb-readout" id="rc-readout-${id}" aria-live="polite">${sum}</div>
    </div>`;
}
function liveCalHtml(id, cal, lg, what) {
  if (!cal) return `<p class="note">The live record's calibration isn't in this update.</p>`;
  if (cal.error) return `<p class="note">The live calibration couldn't be worked out this time (${esc(cal.error)}).</p>`;
  const c = lg ? cal.by_league?.[lg] : cal;
  if (!c || !c.n) return `<p class="note">No settled ${what} ${lg ? `in ${esc(leagueName(lg))} ` : ""}yet.</p>`;
  return `${binRowsChart(id, c, what)}
    <p class="note">${num(c.n)} settled ${what}${cal.pushes ? `, ${cal.pushes} push${cal.pushes === 1 ? "" : "es"} left out` : ""}: a small sample, so the ranges are wide and one weekend can move every row.</p>${howFold(cal.note)}`;
}
const howFold = (note) => (note ? `<details class="fold mk-fold"><summary>How this is measured</summary><p class="note">${esc(note)}</p></details>` : "");
const calBlock = (bt, lg) => (bt?.available ? (lg ? bt.leagues?.[lg] : bt.all) : null);
function modelViewHtml(cur, x, lg) {
  const sb = state.data.portfolio?.strategy_backtests || {};
  const keys = MODEL_KEYS[x.key] || [];
  if (!state.data.portfolio?.models) return `<div class="empty">The model view isn't in this update. It comes with the next site update.</div>`;
  const cards = keys.map((k) => equationCard(k, lg)).join("");
  const shape = x.key === "lean" ? shapeChart(lg) : "";
  let results = "";
  const name = x.key === "lean" ? "Blend Lean" : x.key === "edge12" ? "Model 12%" : x.label;
  if (cur.id === "moneyline") {
    const bt = sb[x.key];
    const part = calBlock(bt, lg);
    const cal = part?.calibration;
    const close = isClose(part);
    const where = close ? "DraftKings' close" : "Pinnacle's early price";
    const a = !bt?.available ? `<p class="note">${esc(bt?.note || "The backtest isn't in this update.")}</p>`
      : !part ? `<p class="note">No ${esc(name)} backtest for ${esc(leagueName(lg))}.</p>`
      : part.error ? `<p class="note">${esc(part.error)}</p>`
      : !cal ? `<p class="note">This backtest has no calibration yet; it comes with the next backtest run.</p>`
      : `${reliabilityChart(`rel-${x.key}-${lg || "all"}`, cal.all_outcomes)}
        <p class="note">Every home, draw and away chance in the replay${lg ? "" : " (all early-price leagues; the Champions League is shown on its own)"}, grouped by what was predicted, against how often it happened. Points on the diagonal are right. The price is ${where}, margin removed.</p>${howFold(cal.note)}`;
    const b = cal?.bets?.n ? `${binRowsChart(`bets-${x.key}-${lg || "all"}`, cal.bets, "bets")}
        <p class="note">${slopeWords(cal.bets)}. The ring is the chance the ${x.key === "lean" ? "blend" : "model"} gave its picks, the tick the price's break-even (1/odds, margin included), the dot how often they won.${cal.bets.predicted - (cal.bets.implied ?? cal.bets.predicted) > 0.03 ? ` On these bets it claimed ${((cal.bets.predicted - cal.bets.implied) * 100).toFixed(1)} points more than the price, and they won ${pct(cal.bets.realized, 1)} against ${pct(cal.bets.implied, 1)} break-even.` : ""}</p>`
      : cal ? `<p class="note">No ${esc(name)} bets in this backtest${lg ? ` for ${esc(leagueName(lg))}` : ""}.</p>` : "";
    results = `
      <div class="section-title">Lined up with results: every outcome</div>${a}
      ${cal ? `<div class="section-title">Lined up with results: its own bets (backtest)</div>${b}` : ""}
      <div class="section-title">Lined up with results: live paper trades</div>
      ${liveCalHtml(`live-${x.key}-${lg || "all"}`, x.st?.calibration, lg, "trades")}`;
  } else if (cur.id === "corners") {
    const qc = cur.live?.quote_calibration;
    const q = qc?.[x.key];
    const quotes = !qc ? `<p class="note">The corner quotes' calibration isn't in this update.</p>`
      : qc.error ? `<p class="note">The corner quotes' calibration couldn't be worked out this time (${esc(qc.error)}).</p>`
      : !q || !q.settled ? `<p class="note">No settled ${BOOK_POSS[x.key] || ""} corner lines yet.</p>`
      : `${reliabilityChart(`qrel-${x.key}`, { model: q.model, price: q.price }, { model: "Model (f)", price: `${BOOK_POSS[x.key]} fair price` }, "lines")}
        <details class="fold mk-fold"><summary>Per bin, with the price beside the model</summary>${binRowsChart(`quotes-${x.key}`, q.model, "lines")}</details>
        <p class="note">Every half line ${esc(BOOK_NAME[x.key])} quoted before kickoff (its newest quote per team line), ${num(q.settled)} settled over ${num(q.matches)} matches${q.unsettled ? `, ${num(q.unsettled)} still waiting for a result` : ""}${q.whole_lines ? `; ${num(q.whole_lines)} whole lines left out (they can push)` : ""}. Each mark groups lines by the chance it gave the over, against how often the over won: ring = model (f), tick = ${esc(BOOK_POSS[x.key])} margin-free price; on the dashed diagonal is right. Every competition together.</p>${howFold(qc.note)}`;
    results = `
      ${cornerShapeChart(lg)}
      <div class="section-title">Lined up with results: every logged line</div>${quotes}
      <div class="section-title">Lined up with results: live paper trades</div>
      ${liveCalHtml(`live-${x.key}-${lg || "all"}`, x.st?.calibration, lg, "trades")}`;
  }
  return `
    <p class="note">${esc(name)}: the equation behind each chance, its fitted numbers, and how the chances lined up with what happened.</p>
    ${cards}${shape}${results}`;
}

// ---------- Portfolio, laid out like the baseball app's (owner, 11 Oct) ----------
// Switcher, then "Paper strategies": one row per strategy in the portfolio (Moneyline:
// Blend Lean then Model 12%; others: one row), the selected one marked; then that
// strategy's tiles, the caveat, profit over time, open and settled trades, the folded
// extras and the rule note. Backtest keeps its own views under the same list.
function pfStrategies(cur) {
  if (cur.id === CB_ID) return [cbStrat()];
  const st = cur.id === "moneyline" || cur.id === "corners" ? cur.live?.strategies || [] : [];
  if (st.length) {
    // Shadow-filter variants (variant_of) sit directly under their parent, whatever order
    // they arrive in; a variant whose parent is missing is dropped.
    const row = (x) => ({ key: x.key, label: stratLabel(x), st: x, rule: x.rule, variantOf: x.variant_of || null });
    return st.filter((x) => !x.variant_of).flatMap((x) => [row(x), ...st.filter((v) => v.variant_of === x.key).map(row)]);
  }
  const label = cur.id === "corners" ? "Model (f) 12% vs Pinnacle" : cur.name;
  return [{ key: cur.id, label, st: null, rule: cur.live?.rule || (cur.id === "moneyline" ? state.data.portfolio?.rule : null) }];
}
// A filtered corner line named for what its cut is of (owner, 25 Oct): "Model (f) vs Pinnacle:
// 12% edge, under 60% to win" = the track's 12%-edge picks that model (f) gives under a 60%
// chance to win (pushes aside). Anything else keeps paper.py's label.
const BOOK_LABEL = { pinnacle: "Pinnacle", draftkings: "DraftKings" };
function stratLabel(x) {
  const f = x.filter, book = BOOK_LABEL[x.rule?.book];
  if (x.variant_of && f?.field === "p_no_push" && isNum(f.max) && book && isNum(x.rule?.threshold)) return `Model (f) vs ${book}: ${pct(x.rule.threshold)} edge, under ${pct(f.max)} to win`;
  return x.label;
}
// Each portfolio keeps its own selection (Moneyline: Lean / 12%; Team corners: the book).
const pfStratKey = (cur) => (cur?.id === "corners" ? state.pfStratCorners : state.pfStrat);
const pfSelected = (strats, cur = pfCurrent()) => strats.find((x) => x.key === pfStratKey(cur)) || strats[0];
// A strategy's parent (itself when it isn't a variant) and the variants filed under a parent.
const pfParent = (strats, x) => (x.variantOf && strats.find((p) => p.key === x.variantOf)) || x;
const pfVariants = (strats, x) => strats.filter((v) => v.variantOf === x.key);
// "Below 60%" from the variant's filter (else the part of its label after the parent's).
function variantShort(x, parent) {
  const f = x.st?.filter;
  if (f && isNum(f.max)) return `Under ${pct(f.max)} to win`;
  if (f && isNum(f.min)) return `${pct(f.min)} and up`;
  const base = parent ? parent.label.replace(/\s*\(.*\)\s*$/, "") : "";
  const rest = base && x.label.startsWith(base) ? x.label.slice(base.length).replace(/^[,\s·]+/, "") : x.label;
  return rest ? rest[0].toUpperCase() + rest.slice(1) : x.label;
}
// One strategy's live trades, narrowed to a competition (and Lean's driver filter).
function strategyLiveSet(cur, x, lg, drv = "") {
  // A variant that adds shadow entries (Blend Lean's old rule) resolves its trade_ids
  // against the live trades and `shadow_trades` together (paper keeps shadows apart).
  const live = cur.live || {};
  const pool = x.st?.adds === "shadow" ? { ...live, trades: [...(live.trades || []), ...(live.shadow_trades || [])] } : live;
  let set = x.st ? stratSet(pool, x.st) : { ...live, trades: live.trades || [] };
  if (lg) set = leagueSet(set, lg);
  if (drv && x.st) set = driverSet(set, x.st, drv, lg);
  return set;
}
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const meanOf = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
// Record, money and averages straight from the trades (so every filter agrees).
function tradeStats(trades) {
  const by = (st) => trades.filter((t) => t.status === st);
  const won = by("won"), lost = by("lost"), voids = by("void"), open = by("open");
  const settled = [...won, ...lost];
  const profit = [...settled, ...voids].reduce((a, t) => a + (t.profit || 0), 0);
  const staked = settled.reduce((a, t) => a + (t.stake || 0), 0);
  return {
    won: won.length, lost: lost.length, void: voids.length, open: open.length, n: settled.length,
    profit, staked, roi: staked ? profit / staked : null,
    atRisk: open.reduce((a, t) => a + (t.stake || 0), 0),
    clv: meanOf(settled.map(tradeClv).filter(isNum)),
    avgEdge: meanOf(trades.map((t) => t.edge).filter(isNum)),
    avgZ: meanOf(trades.map((t) => t.z).filter(isNum)),
    since: trades.map((t) => t.opened_at).filter(Boolean).sort()[0] || null,
  };
}
const monthDay = (iso) => new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
const signCls = (v) => (v > 0.005 ? "pos-text" : v < -0.005 ? "neg-text" : "");
function ruleText(x) {
  if (x.key === "lean") return `${x.rule?.min_z ?? 1}σ+${x.rule?.odds_range ? ` · ${rangeWords(x.rule.odds_range)}` : ""}`;
  return x.rule?.threshold != null ? `${pct(x.rule.threshold)}+` : "";
}
// Backtest headline for a strategy row, for the chosen competition (lg "" = All): bets and
// return (units), "–" with why when that competition has no backtest, or "research only".
const NO_BT = (sub) => ({ v: "–", cls: "muted", sub });
// A per-league backtest file (strategy_backtests.lean / .edge12): the part for lg, or all
// leagues, and its metrics; null when that competition isn't in it or failed.
function btPart(bt, key, lg) {
  if (!bt?.available) return null;
  const part = lg ? bt.leagues?.[lg] : bt.all;
  if (!part || part.error) return null;
  const m = part[key] || part.metrics;
  return m && m.bets != null ? { m, part } : null;
}
function btHead(hit, lg) {
  const where = `${lg ? `${leagueName(lg)} ` : ""}${btWhere(hit.part)}`;
  if (!hit.m.bets) return NO_BT(`0 bets · ${where}`);
  return { v: signedPct(hit.m.roi), cls: signCls(hit.m.roi), sub: `${hit.m.bets.toLocaleString()} bets · ${where}` };
}
// Lean's backtest replays the rule at every price (the old rule). The new rule's odds range
// (1.50 to 3.00) is exactly the replay's 1.50–2.00 and 2.00–3.00 bands, so its own figure is
// those bands pooled: ROI per $1, weighted by bets. Null when the bands don't tile the range.
function leanRangeBt(part, range) {
  const bands = (part?.by_odds_band || []).filter((b) => b.lo != null && b.hi != null && b.lo >= range[0] - 1e-9 && b.hi <= range[1] + 1e-9);
  if (!bands.length || Math.abs(bands[0].lo - range[0]) > 1e-9 || Math.abs(bands[bands.length - 1].hi - range[1]) > 1e-9) return null;
  const bets = bands.reduce((a, b) => a + (b.bets || 0), 0);
  if (!bets) return { bets: 0, roi: null };
  return { bets, roi: bands.reduce((a, b) => a + (b.roi || 0) * (b.bets || 0), 0) / bets };
}
// Archived: an old rule kept for comparison (adds "shadow": Lean's all odds), or a whole track
// the owner moved out of the main list (25 Oct: corners' two plain 12% tracks; they still log
// every pick, and the under-60% lines are cut from them).
const PF_ARCHIVED = { corners: { keys: ["pinnacle", "draftkings"], since: "2026-10-25" } };
const archivedTrack = (x) => !x.variantOf && Object.values(PF_ARCHIVED).some((a) => a.keys.includes(x.key));
const archivedStrat = (x) => x.st?.adds === "shadow" || archivedTrack(x);
function backtestHeadline(cur, x, lg = "") {
  const sb = state.data.portfolio?.strategy_backtests || {};
  if (x.key === "lean" || (archivedStrat(x) && x.variantOf === "lean")) {
    if (!sb.lean?.available) return null;
    const hit = btPart(sb.lean, "lean", lg);
    if (!hit) return NO_BT("no backtest for this competition");
    const range = x.key === "lean" ? x.rule?.odds_range : null;
    const inRange = range ? leanRangeBt(hit.part, range) : null;
    if (!inRange) return btHead(hit, lg);
    const where = `${lg ? `${leagueName(lg)} ` : ""}${btWhere(hit.part)}`;
    if (!inRange.bets) return NO_BT(`0 bets, ${rangeWords(range)} · ${where}`);
    return { v: signedPct(inRange.roi), cls: signCls(inRange.roi), sub: `${inRange.bets.toLocaleString()} bets, ${rangeWords(range)} · ${where}` };
  }
  const bt = cur.backtest;
  if (bt?.kind === "research") return { v: "–", cls: "muted", sub: "research only" };
  if (cur.id === "moneyline") {
    // Model 12%: the DraftKings backtest is the Premier League's (E0_dk.json); the
    // per-league replay (strategy_backtests.edge12) covers All and the other leagues.
    const e12 = sb.edge12;
    if (lg !== "E0" && e12?.available) {
      const hit = btPart(e12, "edge12", lg);
      return hit ? btHead(hit, lg) : NO_BT("no backtest for this competition yet");
    }
    if (lg && lg !== "E0") return NO_BT("no backtest for this competition yet");
    const sm = bt?.by_league?.E0?.summary || bt?.summary;
    return sm ? { v: signedPct(sm.roi), cls: signCls(sm.roi), sub: `${(sm.trades || 0).toLocaleString()} bets · Premier League · DraftKings` } : null;
  }
  const sm = bt?.summary;
  return sm ? { v: signedPct(sm.roi), cls: signCls(sm.roi), sub: `${(sm.trades || 0).toLocaleString()} bets · DraftKings` } : null;
}
// The bet in words: "Barcelona to win", "Draw", "Valencia over 4.5 corners".
function betName(t) {
  if (isCorner(t) || isPlayer(t)) return tradeLabel(t);
  if (t.market === "home") return `${t.home} to win`;
  if (t.market === "away") return `${t.away} to win`;
  return PICK_LABEL[t.market] || t.market;
}
const rowKickoff = (iso) => new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
function pfTradeRow(t) {
  const open = t.status === "open";
  const dec = Number(t.odds);
  const clv = tradeClv(t);
  const how = isLean(t) && t.z != null ? sigmaText(t.z) : t.edge != null ? `edge ${signedPct(t.edge)}` : "";
  const ph = tradePhase(t);
  let extra = "";
  if (ph === "upcoming") { const { cur, live } = currentPrice(t); extra = cur && cur !== t.odds && live ? ` · now ${american(cur)}` : ""; }
  else if (open) extra = ` · ${startedCloseText(t)}`; // kicked off: no "now" price, the close is final
  else extra = `${t.score ? ` · ${esc(t.score)}` : ""}${clv != null ? ` · <span class="nowrap">CLV ${signedPct(clv)}${isDkCorner(t) ? " vs DraftKings' close" : ""}</span>` : isDkCorner(t) ? ` · <span class="nowrap">no DraftKings CLV (one-sided close)</span>` : ""}`;
  // A DraftKings corner trade's second yardstick: Pinnacle's close at the same team line.
  const pinLine = isDkCorner(t) && t.clv_pinnacle != null && !open ? `<span class="meta">CLV ${signedPct(t.clv_pinnacle)} vs Pinnacle's close</span>` : "";
  const right = open
    ? `<b>$${Number(t.stake).toFixed(0)}</b><small>to win $${(t.stake * (dec - 1)).toFixed(2)}</small>`
    : t.status === "void"
      ? `<b>${t.push ? "Push" : "Void"}</b><small>stake back</small>`
      : `<b class="${t.status === "won" ? "pos-text" : "neg-text"}">${usd(t.profit, 2)}</b><small>${t.status}</small>`;
  return `
    <button class="row-btn trade-row" data-trade="${esc(t.id)}">
      <span class="who"><b>${esc(betName(t))}</b>${isLean(t) ? driverChip(t) : ""}${shadowChip(t)}
        <span class="meta">${open && ph !== "upcoming" ? `<b class="phase-tag ${ph}">${PHASE_LABEL[ph]}</b> · ` : ""}${esc(t.home)} v ${esc(t.away)} · ${esc(rowKickoff(t.kickoff))}</span>
        <span class="meta">${american(dec)} (${dec.toFixed(2)})${how ? ` · ${how}` : ""}${extra}</span>${pinLine}</span>
      <span class="vals">${right}</span>
    </button>`;
}
// Settled profit by day (settled date, else kickoff), cumulative from $0.
function settledCurve(trades) {
  const days = new Map();
  for (const t of trades) {
    if (!["won", "lost", "void"].includes(t.status) || t.profit == null) continue;
    const d = String(t.settled_at || t.kickoff).slice(0, 10);
    const g = days.get(d) || { date: d, profit: 0, n: 0, won: 0, lost: 0, void: 0 };
    g.profit += t.profit; g.n += 1; g[t.status] += 1;
    days.set(d, g);
  }
  let cum = 0;
  return [...days.values()].sort((a, b) => a.date.localeCompare(b.date)).map((g) => ({ ...g, cum: (cum += g.profit) }));
}
// ---------- Scrubbable charts (owner, 23 Oct): point, drag or use the arrow keys ----------
// A chart wrapper with data-scrub="<id>"; scrubCharts[id] = {n (stops), readout(i) → HTML
// for the readout line (readoutId), at, and how a pointer finds a stop}:
// - line charts: xs (SVG x per stop; the nearest by x wins), points(i) → [{x, y}] for the
//   `.cdot` circles, and the SVG's `.cursor` line follows;
// - point charts: pick(x, y) → the stop nearest a point in SVG units (the reliability
//   charts: nearest by x first, then by distance);
// - row charts: rows: true, the stop whose `[data-stop]` element is nearest the pointer's
//   height (up and down arrows step too).
// Any element with data-stop="i" gets `.sel` while stop i is on. A mouse scrubs on hover,
// a finger or pen while pressed (the chart keeps touch-action: pan-y, so vertical swipes
// still scroll the page); release keeps the spot. No tooltips: the readout line follows.
const scrubCharts = {};
const plain = (html) => html.replace(/<[^>]+>/g, "");
const scrubLen = (sc) => sc.n ?? sc.xs?.length ?? 0;
function scrubTo(wrap, i) {
  const sc = scrubCharts[wrap.dataset.scrub];
  if (!sc || !scrubLen(sc)) return;
  i = Math.max(0, Math.min(scrubLen(sc) - 1, i));
  sc.at = i;
  const svg = wrap.querySelector("svg");
  const cur = svg?.querySelector(".cursor");
  if (cur && sc.xs) { cur.setAttribute("x1", sc.xs[i]); cur.setAttribute("x2", sc.xs[i]); cur.style.display = ""; }
  const pts = sc.points ? sc.points(i) : [];
  svg?.querySelectorAll(".cdot").forEach((d, k) => {
    if (!pts[k]) { d.style.display = "none"; return; }
    d.setAttribute("cx", pts[k].x); d.setAttribute("cy", pts[k].y); d.style.display = "";
  });
  wrap.querySelectorAll("[data-stop]").forEach((el) => el.classList.toggle("sel", Number(el.dataset.stop) === i));
  const html = sc.readout(i);
  const el = document.getElementById(sc.readoutId);
  if (el) el.innerHTML = html;
  wrap.setAttribute("aria-valuenow", String(i));
  wrap.setAttribute("aria-valuetext", plain(html));
}
function scrubIndex(wrap, clientX, clientY) {
  const sc = scrubCharts[wrap.dataset.scrub];
  if (!sc) return 0;
  if (sc.rows) {
    let best = 0, bd = Infinity;
    wrap.querySelectorAll("[data-stop]").forEach((el) => {
      const r = el.getBoundingClientRect();
      const d = Math.abs(clientY - (r.top + r.height / 2));
      if (d < bd) { bd = d; best = Number(el.dataset.stop); }
    });
    return best;
  }
  const svg = wrap.querySelector("svg");
  if (!svg) return 0;
  const r = svg.getBoundingClientRect(), vb = svg.viewBox.baseVal;
  const x = ((clientX - r.left) / (r.width || 1)) * vb.width;
  const y = ((clientY - r.top) / (r.height || 1)) * vb.height;
  if (sc.pick) return sc.pick(x, y);
  let best = 0;
  sc.xs.forEach((v, i) => { if (Math.abs(v - x) < Math.abs(sc.xs[best] - x)) best = i; });
  return best;
}
const scrubAttrs = (id, n, label) => `data-scrub="${id}" tabindex="0" role="slider" aria-label="${esc(label)}" aria-valuemin="0" aria-valuemax="${n - 1}" aria-valuenow="${n - 1}"`;

const dayName = (d) => new Date(`${d}T12:00:00Z`).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
// "Sat 18 Oct · +$23.40 to date · that day +$8.20 on 5 trades (3–2)".
function curveReadout(c) {
  if (c.origin) return `<b>${esc(dayName(c.date))}</b> · first trade opened · <b>${usd(0, 2)}</b>`;
  const rec = `${c.won ?? 0}–${c.lost ?? 0}${c.void ? `, ${c.void} void` : ""}`;
  return `<b>${esc(dayName(c.date))}</b> · <b class="${signCls(c.cum)}">${usd(c.cum, 2)}</b> to date · that day ${usd(c.profit, 2)} on ${c.n} trade${c.n > 1 ? "s" : ""} (${rec})`;
}
// The day of the first trade opened in a set (the curves' $0 origin), else the first
// settled day.
const firstOpened = (trades, curve = []) => trades.map((t) => t.opened_at).filter(Boolean).sort()[0]?.slice(0, 10) || curve[0]?.date || null;
// The scrub hit area covers the whole SVG, so a pointer past either end snaps to that end.
const scrubPad = (W, H) => `<rect class="scrub-pad" x="0" y="0" width="${W}" height="${H}" fill="transparent"/>`;
// The baseball app's profit chart: $0 line and the end label. Point at it (laptop) or drag
// a finger across it (phone) to trace the running total; the readout line follows. The $0
// origin is a stop too, dated by the first trade opened (owner, 23 Oct).
function curveChart(curve, start) {
  if (!curve.length) return "";
  start = start || curve[0].date;
  const pts = [{ date: start, cum: 0, origin: true }, ...curve];
  const W = 320, H = 170, L = 44, R = 52, T = 12, B = 22;
  const ys = pts.map((c) => c.cum);
  const lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
  const span = hi - lo || 1;
  const sx = (i) => L + (i / (pts.length - 1)) * (W - L - R);
  const sy = (v) => T + (1 - (v - lo) / span) * (H - T - B);
  const ticks = [lo, hi].filter((v) => Math.abs(v) > span * 0.12);
  const grid = ticks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${sy(v)}" y2="${sy(v)}"/><text x="${L - 4}" y="${sy(v) + 3}" text-anchor="end">${usd(v)}</text>`).join("");
  const path = pts.map((c, i) => `${i ? "L" : "M"}${sx(i).toFixed(1)},${sy(c.cum).toFixed(1)}`).join(" ");
  const last = pts.length - 1, end = pts[last].cum;
  const neg = end < 0 ? " neg" : "";
  const dots = curve.map((c, i) => `<circle class="pt${neg}" cx="${sx(i + 1)}" cy="${sy(c.cum)}" r="3"/>`).join("");
  scrubCharts.cv = {
    readoutId: "cv-readout",
    xs: pts.map((_, i) => sx(i)),
    points: (i) => [{ x: sx(i), y: sy(pts[i].cum) }],
    readout: (i) => curveReadout(pts[i]),
    data: pts,
  };
  return `
    <div class="chart curve scrub" ${scrubAttrs("cv", pts.length, "Profit over time: drag or use the arrow keys to move along the line")}>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Settled paper profit by day: ${usd(end, 2)} so far">
        <g class="grid">${grid}</g>
        <line class="zero" x1="${L}" x2="${W - R}" y1="${sy(0)}" y2="${sy(0)}"/>
        <text class="xlab" x="${L - 4}" y="${sy(0) + 3}" text-anchor="end">$0</text>
        <path class="line${neg}" d="${path}"/>
        ${dots}
        <circle class="dot${neg}" cx="${sx(last)}" cy="${sy(end)}" r="4.5"/>
        <text class="end ${end > 0 ? "pos" : end < 0 ? "neg" : ""}" x="${sx(last) + 8}" y="${sy(end) + 4}">${usd(end, Math.abs(end) < 10 ? 2 : 0)}</text>
        <text class="xlab" x="${L}" y="${H - 6}">${esc(monthDay(`${start}T12:00:00Z`))}</text>
        <text class="xlab" x="${sx(last)}" y="${H - 6}" text-anchor="end">${esc(monthDay(`${curve[curve.length - 1].date}T12:00:00Z`))}</text>
        <line class="cursor" x1="0" x2="0" y1="${T}" y2="${H - B}" style="display:none"/>
        <circle class="cdot${neg}" r="5" cx="0" cy="0" style="display:none"/>
        ${scrubPad(W, H)}
      </svg>
      <div class="eb-readout" id="cv-readout" aria-live="polite">${curveReadout(curve[curve.length - 1])}</div>
    </div>`;
}
// Several cumulative curves on one chart (a strategy and its shadow-filter variants,
// owner 23 Oct): the first solid, the rest dashed, each with its end label; a legend; a
// marker where the filter was set. Days are the union of every series' settled days, each
// series holding its total on days it didn't move. One scrub point per day; the readout
// gives every series that day and each variant's difference from the first.
const SERIES_CLS = ["s0", "s1", "s2", "s3"];
// opts (owner, 24 Oct, the Kelly bankroll chart): id (scrub id, default "cv"), base (the
// origin value, default $0), axis(v) / end(v) labels, readout(series, rows, i), aria.
function overlayCurveChart(series, definedAt, start, markLabel = "filter set (judged from here)", opts = {}) {
  const base = opts.base ?? 0;
  const axis = opts.axis || ((v) => usd(v));
  const endText = opts.end || ((v) => usd(v, Math.abs(v) < 10 ? 2 : 0));
  const days = [...new Set(series.flatMap((o) => o.curve.map((c) => c.date)))].sort();
  if (!days.length) return "";
  start = start || days[0];
  // Stop 0 is the $0 origin (the first trade opened), then one stop per settled day.
  const rows = series.map((o) => {
    const at = new Map(o.curve.map((c) => [c.date, c]));
    let cum = base;
    return [{ date: start, cum: base, origin: true }, ...days.map((d) => { const c = at.get(d); if (c) cum = c.cum; return { date: d, cum, day: c || null }; })];
  });
  const W = 320, H = 190, L = opts.base ? 50 : 44, R = 58, T = 16, B = 22;
  const ys = rows.flat().map((c) => c.cum);
  const lo = Math.min(base, ...ys), hi = Math.max(base, ...ys);
  const span = hi - lo || 1;
  const sx = (i) => L + (i / days.length) * (W - L - R); // index 0 = start ($0), day k at k + 1
  const sy = (v) => T + (1 - (v - lo) / span) * (H - T - B);
  const ticks = [lo, hi].filter((v) => Math.abs(v - base) > span * 0.12);
  const grid = ticks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${sy(v)}" y2="${sy(v)}"/><text x="${L - 4}" y="${sy(v) + 3}" text-anchor="end">${axis(v)}</text>`).join("");
  const paths = rows.map((r, k) => `<path class="line ser ${SERIES_CLS[k % 4]}${k ? " dash" : ""}" d="${r.map((c, i) => `${i ? "L" : "M"}${sx(i).toFixed(1)},${sy(c.cum).toFixed(1)}`).join(" ")}"/>`).join("");
  // End labels, nudged apart when two totals sit close together.
  const ends = rows.map((r, k) => ({ k, v: r[r.length - 1].cum, y: sy(r[r.length - 1].cum) })).sort((a, b) => a.y - b.y);
  for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 13) ends[i].y = ends[i - 1].y + 13;
  const last = days.length;
  const endLabels = ends.map((e) => `<circle class="dot ser ${SERIES_CLS[e.k % 4]}" cx="${sx(last)}" cy="${sy(e.v)}" r="4"/><text class="end ser ${SERIES_CLS[e.k % 4]}" x="${sx(last) + 8}" y="${e.y + 4}">${endText(e.v)}</text>`).join("");
  // The filter marker sits between the last day before it and the first day on or after it.
  let marker = "";
  if (definedAt) {
    const d0 = String(definedAt).slice(0, 10);
    const k = days.findIndex((d) => d >= d0);
    const mx = k < 0 ? sx(last) : k === 0 ? sx(0) : (sx(k) + sx(k + 1)) / 2;
    const anchor = mx > (L + W - R) / 2 ? "end" : "start";
    marker = `<line class="filter-mark" x1="${mx}" x2="${mx}" y1="${T - 6}" y2="${H - B}"/><text class="filter-lab" x="${mx + (anchor === "end" ? -4 : 4)}" y="${T - 6}" text-anchor="${anchor}">${esc(markLabel)}</text>`;
  }
  const id = opts.id || "cv", rid = `${id}-readout`;
  const readout = opts.readout || overlayReadout;
  scrubCharts[id] = {
    readoutId: rid,
    xs: rows[0].map((_, i) => sx(i)),
    points: (i) => rows.map((r) => ({ x: sx(i), y: sy(r[i].cum) })),
    readout: (i) => readout(series, rows, i),
    data: rows[0].map((c) => c.date),
  };
  const legend = series.map((o, k) => `<span class="lg-item"><svg class="lg-sw" viewBox="0 0 22 8" aria-hidden="true"><line class="ser ${SERIES_CLS[k % 4]}${k ? " dash" : ""}" x1="1" x2="21" y1="4" y2="4"/></svg>${esc(o.name)}${o.label ? `<span class="muted"> · ${esc(o.label)}</span>` : ""}</span>`).join("");
  return `
    <div class="curve-legend">${legend}</div>
    <div class="chart curve overlay scrub" ${scrubAttrs(id, last + 1, opts.aria || "Profit over time, every version: drag or use the arrow keys to move along the lines")}>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.aria ? `${opts.aria}: ` : "Settled paper profit by day: ")}${esc(series.map((o, k) => `${o.name} ${endText(rows[k][last].cum)}`).join(", "))}">
        <g class="grid">${grid}</g>
        <line class="zero" x1="${L}" x2="${W - R}" y1="${sy(base)}" y2="${sy(base)}"/>
        <text class="xlab" x="${L - 4}" y="${sy(base) + 3}" text-anchor="end">${axis(base)}</text>
        ${marker}
        ${paths}
        ${endLabels}
        <text class="xlab" x="${L}" y="${H - 6}">${esc((opts.dateLabel || ((d) => monthDay(`${d}T12:00:00Z`)))(start))}</text>
        <text class="xlab" x="${sx(last)}" y="${H - 6}" text-anchor="end">${esc((opts.dateLabel || ((d) => monthDay(`${d}T12:00:00Z`)))(days[last - 1]))}</text>
        <line class="cursor" x1="0" x2="0" y1="${T}" y2="${H - B}" style="display:none"/>
        ${rows.map((_, k) => `<circle class="cdot ser ${SERIES_CLS[k % 4]}" r="4.5" cx="0" cy="0" style="display:none"/>`).join("")}
        ${scrubPad(W, H)}
      </svg>
      <div class="eb-readout" id="${rid}" aria-live="polite">${readout(series, rows, last)}</div>
    </div>`;
}
// ---------- Kelly sizing beside the flat $10 record (owner, 24 Oct; paper/backtest `sizing`) ----------
// sizing = {flat, kelly, kelly_raw? (Model 12%), blend_from?}; each block {bankroll_start,
// bankroll_now, profit, max_drawdown, max_drawdown_pct, bets, bets_settled, bets_staked,
// bets_zero_stake, bets_no_chance?, median_stake, curve [{date, bankroll}], stakes? (live
// Kelly: {trade id: stake}), p_note?, note, label?}. Flat isn't held to the bankroll (it keeps
// staking $10 below $0, so its P/L equals the record); Kelly can't go below $0.
const bankUsd = (v, d = 0) => `${v < 0 ? "−" : ""}$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })}`;
const SIZING_LINES = [["flat", "Flat $10"], ["kelly", "¼ Kelly"], ["kelly_raw", "Kelly on the raw model (over-confident)"]];
const sizingName = (k, b) => (k === "kelly_raw" ? b?.label || SIZING_LINES[2][1] : SIZING_LINES.find(([kk]) => kk === k)?.[1] || k);
// "would have gone broke (fell $X below the $1,000 start)" for flat once it passes $0;
// "bust" for Kelly at about $0; else the drawdown as a share of the peak and in dollars.
function drawdownText(k, b) {
  const start = b.bankroll_start ?? 1000;
  const low = Math.min(b.bankroll_now, ...(b.curve || []).map((c) => c.bankroll));
  if (k === "flat" && (b.bankroll_now <= 0 || (b.max_drawdown_pct ?? 0) > 1)) return `would have gone broke (fell ${bankUsd(start - low)} below the ${bankUsd(start)} start)`;
  if (k !== "flat" && b.bankroll_now < 1) return "bust";
  return `${pct(b.max_drawdown_pct, 1)} (${bankUsd(b.max_drawdown, 0)})`;
}
// A backtest runs over seasons, so its dates carry the year.
const dayYear = (d) => new Date(`${d}T12:00:00Z`).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
const longSpan = (dates) => dates.length > 1 && Date.parse(dates[dates.length - 1]) - Date.parse(dates[0]) > 300 * 864e5;
function sizingReadout(series, rows, i) {
  const parts = series.map((o, k) => `${esc(o.name)} <b>${bankUsd(rows[k][i].cum, 2)}</b>`);
  const dates = rows[0].map((r) => r.date);
  return `<b>${esc(longSpan(dates) ? dayYear(rows[0][i].date) : dayName(rows[0][i].date))}</b>${rows[0][i].origin ? " · start" : ""} · ${parts.join(" · ")}`;
}
// The bankroll chart and tiles for one sizing block (live strategy or backtest part).
function sizingHtml(id, sz, where = "") {
  if (!sz) return "";
  if (sz.error) return `<div class="section-title">Bankroll: flat $10 vs ¼ Kelly</div><p class="note">The Kelly comparison couldn't be worked out this time (${esc(sz.error)}).</p>`;
  const lines = SIZING_LINES.map(([k]) => [k, sz[k]]).filter(([, b]) => b && Array.isArray(b.curve));
  if (!lines.length || !lines.some(([, b]) => b.bets)) return "";
  const series = lines.map(([k, b]) => ({ name: sizingName(k, b), curve: b.curve.map((c) => ({ date: c.date, cum: c.bankroll })) }));
  const first = series.flatMap((o) => o.curve.map((c) => c.date)).sort()[0];
  const chart = series.some((o) => o.curve.length)
    ? overlayCurveChart(series, null, first, "", { id, base: lines[0][1].bankroll_start ?? 1000, axis: (v) => bankUsd(v), end: (v) => bankUsd(v), readout: sizingReadout, aria: "Bankroll by day, flat $10 against quarter Kelly", dateLabel: longSpan(series.flatMap((o) => o.curve.map((c) => c.date)).sort()) ? (d) => new Date(`${d}T12:00:00Z`).toLocaleDateString(undefined, { month: "short", year: "numeric", timeZone: "UTC" }) : null })
    : `<p class="muted">The bankroll moves once a trade settles.</p>`;
  const f = sz.flat || {}, k = sz.kelly || {};
  const raw = sz.kelly_raw;
  const tile = (label, value, sub, cls = "") => `<div class="tile"><div class="label">${label}</div><div class="value ${cls}">${value}</div><div class="sub">${sub}</div></div>`;
  const kBust = drawdownText("kelly", k) === "bust";
  const tiles = `<div class="tiles pf-tiles sizing-tiles">
      ${tile("Bankroll now, ¼ Kelly", kBust ? "bust" : bankUsd(k.bankroll_now, 2), `flat $10: ${bankUsd(f.bankroll_now, 2)}`)}
      ${tile("Profit, ¼ Kelly", usd(k.profit, 2), `flat $10: ${usd(f.profit, 2)}`, signCls(k.profit))}
      ${tile("Max drawdown, ¼ Kelly", kBust ? "bust" : pct(k.max_drawdown_pct, 1), `flat $10: ${drawdownText("flat", f)}`)}
      ${tile("Bets staked", `${num(k.bets_staked)} of ${num(k.bets)}`, `${num(k.bets_zero_stake)} with no stake${k.bets_no_chance ? ` · ${num(k.bets_no_chance)} with no chance to size on` : ""}`)}
    </div>`;
  const rawLine = raw ? `<p class="note"><b>${esc(sizingName("kelly_raw", raw))}:</b> bankroll ${drawdownText("kelly_raw", raw) === "bust" ? "<b>bust</b>" : `${bankUsd(raw.bankroll_now, 2)}, max drawdown ${drawdownText("kelly_raw", raw)}`}, ${num(raw.bets_staked)} of ${num(raw.bets)} bets staked.</p>` : "";
  const notes = [k.p_note, raw?.p_note, k.note || f.note].filter(Boolean);
  return `
    <div class="section-title">Bankroll: flat $10 vs ¼ Kelly${where ? ` <span class="muted nocase">· ${esc(where)}</span>` : ""}</div>
    <p class="note sizing-frame">Flat $10 is the record. Kelly is shown for comparison and only helps once a strategy beats the closing price.</p>
    <div class="card bank-card">${chart}</div>
    ${tiles}
    ${rawLine}
    ${notes.length ? `<details class="fold mk-fold"><summary>How Kelly is sized here</summary>${notes.map((n) => `<p class="note">${esc(n)}</p>`).join("")}</details>` : ""}`;
}
// A trade's quarter-Kelly stake: from whichever live strategy's sizing holds its id.
function kellyStakeLine(t) {
  for (const p of portfolios()) {
    for (const st of p.live?.strategies || []) {
      const stakes = st.sizing?.kelly?.stakes;
      if (stakes && Object.hasOwn(stakes, t.id)) {
        const v = stakes[t.id];
        return v > 0 ? `¼ Kelly stake: ${bankUsd(v, 2)}` : `Kelly: no stake (no edge on the ${st.sizing.kelly.p_source === "model_f" ? "model (f) chance" : "calibrated chance"})`;
      }
    }
  }
  return "";
}
// "Sat 18 Oct · All +$23.40 · Below 60% +$31.10 (difference +$7.70)"; at the origin
// "Thu, Oct 9 · first trade opened · All $0.00 · Below 60% $0.00".
function overlayReadout(series, rows, i) {
  const base = rows[0][i].cum;
  const parts = series.map((o, k) => {
    const v = rows[k][i].cum;
    return `${esc(o.name)} <b class="${signCls(v)}">${usd(v, 2)}</b>${k && !rows[0][i].origin ? ` (difference ${usd(v - base, 2)})` : ""}`;
  });
  return `<b>${esc(dayName(rows[0][i].date))}</b>${rows[0][i].origin ? " · first trade opened" : ""} · ${parts.join(" · ")}`;
}
// A variant's own judgement (paper.variant_since): only trades opened at or after
// defined_at count, since the cut was chosen after seeing the earlier ones. since_defined =
// {defined_at, parent: {trades, summary}, variant: {trades, summary}, clv_diff: {clv_pinnacle,
// clv_dk: {trades, kept, matches, mean_all, mean_kept, diff, ci95, draws}}}; clv_pinnacle is
// the pre-registered measure. Pooled over every competition.
const JUDGE_MIN = 30;
const CLV_FIELD_NAME = { clv_pinnacle: "Pinnacle's close", clv_dk: "DraftKings' close" };
function sinceDefinedHtml(cur, x, parent, lg) {
  const sd = x.st?.since_defined;
  const at = sd?.defined_at || x.st?.defined_at;
  if (!sd || !at) return "";
  const name = variantShort(x, parent);
  // Which side is "kept" and which is "all": a filter keeps part of its parent (below 60%
  // vs all); a shadow variant (adds: "shadow", Blend Lean's old rule) adds the picks the
  // live rule now skips, so the live rule is the kept part and the variant is all of them.
  const shadow = x.st?.adds === "shadow";
  const keptName = shadow ? "Live rule" : name, allName = shadow ? name : "All";
  const cut = shadow ? "odds range" : `${pct(x.st?.filter?.max ?? 0.6)} cut`;
  // The trades the cut was chosen on: the parent's settled ones opened before defined_at.
  const before = (strategyLiveSet(cur, parent, "").trades || []).filter((t) => t.opened_at && t.opened_at < at && t.status !== "open").length;
  // Corners (both books) are judged on Pinnacle's close; Moneyline keeps DraftKings'.
  const clvKey = cur.id === "corners" ? "clv_pinnacle" : "clv_dk";
  const line = (label, part) => {
    const s = part?.summary;
    if (!part?.trades || !s) return `<tr><th>${esc(label)}</th><td>0</td><td colspan="4" class="muted">none yet</td></tr>`;
    const c = s[clvKey];
    return `<tr><th>${esc(label)}</th><td>${part.trades}</td><td>${s.settled ?? 0}</td><td class="${signCls(s.profit || 0)}">${usd(s.profit || 0, 2)}</td><td>${s.settled ? signedPct(s.roi) : "–"}</td><td>${isNum(c) ? signedPct(c) : "–"}</td></tr>`;
  };
  // The judged field first; DraftKings' own close only on the DraftKings track.
  const dkTrack = cur.id === "corners" && parent.key === "draftkings";
  const diffs = Object.entries(sd.clv_diff || {})
    .filter(([f, d]) => d && d.trades && (f === clvKey || dkTrack))
    .sort(([a], [b]) => (a === clvKey ? -1 : b === clvKey ? 1 : 0));
  const diffLine = ([f, d]) => {
    const where = CLV_FIELD_NAME[f] || f;
    const head = f === clvKey ? "<b>The judged measure</b>, CLV" : "Descriptive only, CLV";
    if (!isNum(d.diff)) return `<p class="note">${head} against ${where}: no ${esc(keptName.toLowerCase())} trade has a close yet (${d.trades} of ${esc(allName.toLowerCase())} ${d.trades === 1 ? "does" : "do"}).</p>`;
    return `<p class="note">${head} against ${where}: ${esc(keptName.toLowerCase())} ${signedPct(d.mean_kept)} vs ${esc(allName.toLowerCase())} ${signedPct(d.mean_all)}, a difference of <b class="${signCls(d.diff)}">${signedPct(d.diff)}</b>${d.ci95 ? ` (95% range ${ciText(d.ci95)})` : " (no range yet: it needs trades from at least 2 matches)"}; ${d.kept} kept of ${d.trades} trade${d.trades === 1 ? "" : "s"} with a close, from ${d.matches} match${d.matches === 1 ? "" : "es"}.</p>`;
  };
  const kept = sd.clv_diff?.[clvKey]?.kept ?? 0;
  const none = !sd.parent?.trades;
  const enough = none ? `<p class="note judge-few"><b>Nothing to judge yet:</b> no trade has opened since ${monthDay(at)}.</p>`
    : kept < JUDGE_MIN ? `<p class="note judge-few"><b>Not enough trades yet:</b> ${kept} ${esc(keptName.toLowerCase())} trade${kept === 1 ? "" : "s"} settled with ${CLV_FIELD_NAME[clvKey]} since ${monthDay(at)}; it takes a few dozen before the difference means much.</p>` : "";
  // Whole period (owner, 23 Oct): the same columns from each version's own full summary,
  // and the CLV difference computed here from the trades: won or lost with a value, the
  // same trades the summaries' CLV averages, so the two means match the CLV column.
  // Descriptive: it includes the trades the cut was chosen on.
  const allTrades = strategyLiveSet(cur, shadow ? x : parent, "").trades || [];
  const keptIds = new Set((shadow ? parent.st : x.st)?.trade_ids || []);
  const withClv = allTrades.filter((t) => (t.status === "won" || t.status === "lost") && isNum(t[clvKey]));
  const keptClv = withClv.filter((t) => keptIds.has(t.id));
  const meanAll = meanOf(withClv.map((t) => t[clvKey])), meanKept = meanOf(keptClv.map((t) => t[clvKey]));
  const from = firstOpened(strategyLiveSet(cur, parent, "").trades || []);
  const fullLine = (label, st) => line(label, st?.summary ? { trades: st.summary.trades, summary: st.summary } : null);
  let whole = `
      <div class="judged-head">Whole period, from ${from ? esc(dayName(from)) : "the first trade"}${lg ? ` <span class="muted">· every competition</span>` : ""}</div>
      <table class="judged-tab whole-tab"><thead><tr><th></th><th>Trades</th><th>Settled</th><th>Profit</th><th>ROI</th><th>CLV</th></tr></thead>
        <tbody>${fullLine(name, x.st)}${fullLine(shadow ? keptName : "All", parent.st)}</tbody></table>
      <p class="note">CLV against ${CLV_FIELD_NAME[clvKey]}, ${esc(keptName.toLowerCase())} minus ${esc(allName.toLowerCase())}: ${isNum(meanKept) && isNum(meanAll)
        ? `<b class="${signCls(meanKept - meanAll)}">${signedPct(meanKept - meanAll)}</b> (${signedPct(meanKept)} vs ${signedPct(meanAll)}; ${keptClv.length} kept of ${withClv.length} trade${withClv.length === 1 ? "" : "s"} with a close)`
        : "none yet (no trade with a close)"}.</p>
      <p class="note"><b>Descriptive only:</b> this includes the trades the ${cut} was chosen from, so it can't prove ${shadow ? "the range" : "the filter"}.</p>`;
  // A rule backcast to the strategy's start (Blend Lean, 24 Oct): the descriptive part is
  // before the rule, from paper's before_defined (same shape as since_defined), instead of
  // the whole period; the judged part below stays since the rule date.
  const bd = x.st?.before_defined;
  if (bd) {
    const bc = parent.st?.backcast;
    const startDay = bc?.lean_started ? String(bc.lean_started).slice(0, 10) : from;
    const preDiffs = Object.entries(bd.clv_diff || {}).filter(([f, d]) => d && d.trades && f === clvKey);
    whole = `
      <div class="judged-head">Before the rule, ${startDay ? `${esc(dayName(startDay))} to ` : ""}${esc(dayName(String(bd.until || at).slice(0, 10)))} <span class="muted">· backcast</span>${lg ? ` <span class="muted">· every competition</span>` : ""}</div>
      <table class="judged-tab whole-tab"><thead><tr><th></th><th>Trades</th><th>Settled</th><th>Profit</th><th>ROI</th><th>CLV</th></tr></thead>
        <tbody>${line(name, bd.variant)}${line(`${keptName} (backcast)`, bd.parent)}${bc?.cut ? line("Cut (outside the range)", { trades: bc.cut.trades, summary: bc.cut }) : ""}</tbody></table>
      ${preDiffs.map(([f, d]) => (isNum(d.diff)
        ? `<p class="note">CLV against ${CLV_FIELD_NAME[f]}, ${esc(keptName.toLowerCase())} minus ${esc(allName.toLowerCase())}: <b class="${signCls(d.diff)}">${signedPct(d.diff)}</b> (${signedPct(d.mean_kept)} vs ${signedPct(d.mean_all)}; ${d.kept} kept of ${d.trades} trade${d.trades === 1 ? "" : "s"} with a close${d.ci95 ? `, 95% range ${ciText(d.ci95)}` : ""}).</p>`
        : `<p class="note">CLV against ${CLV_FIELD_NAME[f]}: no ${esc(keptName.toLowerCase())} trade with a close before the rule.</p>`)).join("")}
      <p class="note"><b>Descriptive only:</b> ${esc((bd.note || `the ${cut} was chosen after seeing these trades, so they can't show that it helps.`).replace(/^before the rule, descriptive only:\s*/i, ""))}</p>`;
  }
  const oldCode = sd.old_code ? ` ${sd.old_code} trade${sd.old_code === 1 ? "" : "s"} the old code opened in that window ${sd.old_code === 1 ? "is" : "are"} left out of both.` : "";
  const why = `The ${cut} was picked after seeing the first ${before} settled trade${before === 1 ? "" : "s"}, so only trades opened from ${monthDay(at)} count when judging it; the earlier ones still show in the list and the chart.${oldCode}`;
  return `
    <div class="card judged">
      ${whole}
      <div class="judged-head judged-since">Judged from ${esc(dayName(String(at).slice(0, 10)))}${lg ? ` <span class="muted">· every competition</span>` : ""}</div>
      <table class="judged-tab"><thead><tr><th></th><th>Trades</th><th>Settled</th><th>Profit</th><th>ROI</th><th>CLV</th></tr></thead>
        <tbody>${line(name, sd.variant)}${line(shadow ? keptName : "All", sd.parent)}</tbody></table>
      ${diffs.map(diffLine).join("")}
      ${enough}
      <p class="note">${why}</p>
      ${x.st?.filter?.note || x.st?.note ? `<details class="fold"><summary>How this ${shadow ? "comparison" : "filter"} works</summary><p class="note">${esc(x.st.filter?.note || x.st.note)}</p></details>` : ""}
    </div>`;
}
// Live paper by odds band (owner, 24 Oct; paper.live_odds_bands): the backtest's odds chart
// on the strategy's live trades, the ROI table, and each band beside the same band in that
// strategy's backtest (Moneyline only: corners have no priced backtest).
function liveOddsBandsHtml(cur, x, lg, drv) {
  const st = x.st;
  if (!st) return "";
  const bands = lg ? st.by_league?.[lg]?.by_odds_band : st.by_odds_band;
  const title = `<div class="section-title">Where the bets sit on the odds</div>`;
  if (!bands) return lg && st.by_odds_band ? `${title}<p class="note">No ${esc(x.label)} trades in ${esc(leagueName(lg))} to split by odds yet.</p>` : "";
  if (bands.error) return `${title}<p class="note">The odds bands couldn't be worked out this time (${esc(bands.error)}).</p>`;
  if (!bands.some((b) => b.bets)) return "";
  const id = `lob-${x.key}-${lg || "all"}`;
  const clvField = st.odds_bands?.clv_field || (cur.id === "corners" ? "clv_pinnacle" : "clv_dk");
  const vs = clvField === "clv_pinnacle" ? "Pinnacle's close" : "DraftKings' close";
  const table = bandTable(["Odds", "Trades", "Won vs needed", "ROI, 95% range"], bands.map((b) => `<tr><td>${esc(oddsBandLabel(b))}</td><td>${num(b.bets)}${b.bets && isNum(b.settled) ? `<span class="meta">${b.settled} settled</span>` : ""}</td><td>${isNum(b.win_rate) ? `${pct(b.win_rate, 1)} vs ${pct(b.breakeven, 1)}` : b.bets ? "none settled" : "no trades"}</td>${isNum(b.roi) ? rngCell(b.roi, b.roi_ci95) : "<td>–</td>"}</tr>`));
  // The same strategy's backtest bands (Moneyline: lean / edge12 files), for this competition.
  const bt = cur.id === "moneyline" ? state.data.portfolio?.strategy_backtests?.[x.key] : null;
  const btPartBands = bt?.available ? (lg ? bt.leagues?.[lg] : bt.all)?.by_odds_band : null;
  const btClose = bt?.available && lg && isClose(bt.leagues?.[lg]);
  const cmpRows = (btPartBands || []).length ? bands.map((b, i) => {
    const k = btPartBands.find((r) => r.lo === b.lo && r.hi === b.hi) || btPartBands[i] || {};
    const live = isNum(b.clv) ? `${signedPct(b.clv)}<span class="meta">${b.clv_trades} trade${b.clv_trades === 1 ? "" : "s"}</span>` : b.bets ? "no final close yet" : "no trades";
    const back = k.bets && isNum(k.clv) ? `CLV was ${signedPct(k.clv)}<span class="meta">${num(k.bets)} bets, range ${ciText(k.clv_ci95)}</span>` : "no bets";
    return `<tr><td>${esc(oddsBandLabel(b))}</td><td>${live}</td><td>${back}</td></tr>`;
  }) : [];
  const compare = cmpRows.length ? `
    <p class="note"><b>Beside the backtest</b>${lg ? ` (${esc(leagueName(lg))})` : ""}: the same odds bands ${btClose ? "at DraftKings' close" : "at Pinnacle's early prices"}, a much larger sample than the live trades.</p>
    ${bandTable(["Odds", `Live CLV (vs ${vs})`, "In the backtest"], cmpRows)}` : "";
  return `${title}
    ${oddsBandChart(id, bands, "trades")}
    <p class="note">Bar = the share of ${drv ? "all this strategy's" : "these"} trades in each odds band; dot = their CLV against ${vs} (trades whose close is final), bar = its 95% range, line = 0. Live samples are small, so read the ranges: the backtest's odds bands, under Backtest, are where the edge question is answered.</p>
    ${table}
    ${compare}`;
}
function liveRuleNote(cur, x) {
  const rule = state.data.portfolio?.rule || {};
  if (cur.id === "corners" && (x.rule?.book || x.key) === "draftkings") return `<b>Live test: fixed ${pct(x.rule?.threshold ?? PAPER_EDGE)} on model (f) against DraftKings.</b> The same model and rule as the Pinnacle track, on DraftKings' price: a $${x.rule?.stake ?? 10} paper trade opens on a team's corner line when the model's chance shows at least ${aPct(x.rule?.threshold ?? PAPER_EDGE)} edge, at most one per team and line, in the Premier League, La Liga, the Bundesliga, Serie A and Ligue 1 (DraftKings lists no team corners in the Championship, League One or League Two). On a whole line (say 5), exactly 5 corners is a push: the stake comes back. Closing line value is against DraftKings' own close; where DraftKings priced only one side at the close there is none, and Pinnacle's close at the same line is the second check. Prices: logged with Pinnacle's, about a day before kickoff and again just before it.`;
  if (cur.id === "corners") return `<b>Live test: fixed ${pct(x.rule?.threshold ?? PAPER_EDGE)} on model (f) against Pinnacle.</b> A $${x.rule?.stake ?? 10} paper trade opens on a team's corner line when the model's chance shows at least ${aPct(x.rule?.threshold ?? PAPER_EDGE)} edge against Pinnacle's price, at most one per team and line, in every competition with team corners (League One and League Two since 24 Oct, where model (f) has never been tested, so treat their picks as untested). On a whole line (say 5), exactly 5 corners is a push: the stake comes back. Prices: Pinnacle's team corners, logged about a day before kickoff and again just before it.`;
  if (x.key === "lean") return `${x.rule?.odds_range ? `<b>Odds range since ${monthDay(x.rule.odds_range_from)}: ${rangeWords(x.rule.odds_range)} only</b> (decimal ${x.rule.odds_range[0].toFixed(2)} to ${x.rule.odds_range[1].toFixed(2)}). ${esc(x.rule.odds_range_note || "")}<br><br>` : ""}<b>Blend Lean (1σ+).</b> A $${x.rule?.stake ?? 10} paper trade opens on the side whose blended chance (model mixed with DraftKings' price) beats the price's break-even by at least one σ, Strong at 2σ, one per match, in every competition. σ is how far the blend usually strays from the market in that competition${sigmaList(x.rule?.sigma)}. Be warned: most Lean picks come from the part of the blend that sharpens DraftKings' own price (it makes favourites a little likelier and long shots less likely, a pattern fitted on Pinnacle's prices), not from the model, which gets little weight. With that sharpening switched off, only 2 of the 12 matches that reached 1σ from 1 to 10 Oct would still have qualified. Lean hasn't proven itself, and neither has the baseball app's Lean tier it comes from. Expect few picks, mostly outside the Premier League, and judge it on closing line value rather than a few dozen results.`;
  if (cur.id === "moneyline" && fixedRule()) return `<b>Your live test: fixed ${pct(rule.threshold ?? PAPER_EDGE)} on the model alone.</b> A $${rule.stake ?? 10} paper trade opens on every match where the model's own chance (not blended with the market) shows at least ${aPct(rule.threshold ?? PAPER_EDGE)} edge against DraftKings' quoted price, in every competition. The Matches tab flags exactly these.`;
  if (cur.id === "moneyline") return rule.threshold == null ? "No new paper trades: no minimum edge has beaten the market in past bets." : `A $${rule.stake ?? 10} paper trade opens whenever a pick reaches a ${pct(rule.threshold)} edge, the same minimum the Matches tab flags with.`;
  return esc(cur.note || "");
}
function liveStrategyHtml(cur, x, lg, strats = []) {
  const lean = x.key === "lean";
  const leanLike = (x.variantOf || x.key) === "lean";
  const drv = lean ? state.pfDriver : "";
  const set = strategyLiveSet(cur, x, lg, drv);
  const trades = set.trades || [];
  const m = tradeStats(trades);
  const dkCorners = cur.id === "corners" && (x.variantOf || x.key) === "draftkings";
  const book = cur.id === "corners" ? BOOK_POSS[dkCorners ? "draftkings" : "pinnacle"] : trades.length && trades.every(isPlayer) ? `${playerBook()}'s` : "DraftKings'";
  const banner = x.st?.error ? `<div class="banner">This filtered view could not be built this time (${esc(x.st.error)}); the full track's record is unaffected.</div>`
    : set.error ? `<div class="banner">${esc(set.error)}</div>` : set.note && cur.status === "live" ? `<p class="note">${esc(set.note)}</p>` : "";
  // A variant shows its own note, then its parent's rule.
  const ruleParent = x.variantOf ? pfParent(strats, x) : x;
  const vNote = x.variantOf ? x.st?.note || x.st?.filter?.note : "";
  const rules = `<div class="explain live-test rule-note">${vNote ? `<b>${esc(x.label)}.</b> ${esc(vNote)}<br><br>` : ""}${liveRuleNote(cur, ruleParent)}</div><p class="note">${cur.id === "moneyline" ? `${oddsAge() || "Odds: DraftKings."} ` : ""}Paper trades: no money is staked. Void = stake back.</p>`;
  const drvPanel = lean ? `<details class="fold drv-fold"${drv ? " open" : ""}><summary>What carried each pick${drv ? ` <span class="muted">(showing ${esc(DRIVER_NAME[drv].toLowerCase())})</span>` : ""}</summary>${leanDriverPanel(x.st, lg)}</details>` : "";
  // The page heading already names the strategy; only a chosen competition needs saying.
  const title = lg ? `<div class="section-title nocase">${esc(leagueName(lg))} only</div>` : "";
  if (!trades.length) {
    const empty = drv ? `No ${DRIVER_NAME[drv].toLowerCase()} Lean trades yet.`
      : lean ? `No Blend Lean trades yet. One opens when a side's blended chance beats DraftKings' break-even by 1σ.${x.rule?.source === "none" && x.rule?.note ? ` ${esc(x.rule.note)}` : ""}`
      : cur.id === "corners" ? `No corner trades ${dkCorners ? "at DraftKings " : ""}yet. One opens when a team's corner line shows a 12% edge against ${book} price.`
      : cur.status === "testing" ? "No trades yet. They start once this strategy passes its tests."
      : cur.status === "retired" ? "No live trades: this strategy was retired before any opened. Its testing history is under Backtest."
      : "No paper trades yet.";
    return `${title}${banner}${drvPanel}<div class="empty">${empty}</div>${rules}`;
  }
  const avg = leanLike && m.avgZ != null ? sigmaText(m.avgZ) : signedPct(m.avgEdge);
  // Unsettled trades split by kickoff: upcoming and in play stay "Open"; a finished match
  // waits for its result in its own group until the ledger settles it.
  const open = trades.filter((t) => t.status === "open" && tradePhase(t) !== "awaiting");
  const awaiting = trades.filter((t) => tradePhase(t) === "awaiting");
  const openRisk = open.reduce((a, t) => a + (t.stake || 0), 0);
  const tiles = `
    <div class="tiles pf-tiles">
      <div class="tile"><div class="label">Profit, settled</div><div class="value ${m.n ? signCls(m.profit) : ""}">${m.n ? usd(m.profit, 2) : "–"}</div><div class="sub">${m.n ? `on $${m.staked.toFixed(0)} staked · ${signedPct(m.roi)}` : "nothing settled yet"}</div></div>
      <div class="tile"><div class="label">Record</div><div class="value">${m.won}–${m.lost}</div><div class="sub">${m.void} void${m.clv != null ? ` · CLV ${signedPct(m.clv)}` : ""}</div></div>
      <div class="tile"><div class="label">Open</div><div class="value">${m.open}</div><div class="sub">$${m.atRisk.toFixed(0)} at risk${awaiting.length ? ` · ${awaiting.length} finished, awaiting result (not in profit yet)` : ""}</div></div>
      <div class="tile"><div class="label">Avg edge</div><div class="value">${avg}</div><div class="sub">when placed${leanLike ? " (σ above break-even)" : ""}</div></div>
    </div>`;
  // Closes taken over an hour before kickoff (throttled scheduled builds): approximate CLV.
  const early = trades.filter((t) => !isPlayer(t) && !isCorner(t) && (t.status === "won" || t.status === "lost") && tradeClv(t) != null && closeApprox(t)).length;
  const earlyText = early ? ` ${early} close${early > 1 ? "s were" : " was"} taken more than an hour before kickoff, so ${early > 1 ? "their" : "its"} CLV is approximate.` : "";
  const caveat = m.n < 200 || early ? `<p class="note">${m.n < 200 ? `${m.n ? `Only ${m.n} settled so far` : "Nothing settled yet"}; until a few hundred, results are mostly luck. Closing line value (CLV, against ${book} close) is the faster sign of an edge.` : ""}${earlyText}</p>` : "";
  // paper.py's 95% range on ROI, shown only when its summary covers exactly these trades
  // (a competition plus a driver has no summary of its own).
  const s = set.summary || {};
  const ci = s.roi_ci95 && s.settled === m.n && Math.abs((s.profit ?? 0) - m.profit) < 0.005
    ? `<p class="note">95% range on ROI: ${signedPct(s.roi_ci95[0])} to ${signedPct(s.roi_ci95[1])} (resampling match weeks). Won ${pct(s.win_rate, 1)} vs ${pct(s.breakeven, 1)} needed to break even.</p>` : "";
  const curve = settledCurve(trades);
  // A parent with variants, or a variant: every one of them on one chart, parent solid.
  const parent = pfParent(strats, x);
  const family = [parent, ...pfVariants(strats, parent)];
  // A filter's parent is "All"; with a shadow variant (more trades) the parent is the live rule.
  const parentName = family.slice(1).some((v) => v.st?.adds === "shadow") ? "Live rule" : "All";
  const overlay = family.length > 1
    ? family.map((s, k) => ({
      name: k ? variantShort(s, parent) : parentName,
      label: k ? "" : s.label,
      curve: settledCurve(s === x ? trades : strategyLiveSet(cur, s, lg).trades || []),
    }))
    : null;
  const definedAt = family.map((s) => s.st?.defined_at).find(Boolean) || null;
  // The origin is the first trade opened: the parent's (every version starts there) or this set's.
  // Both lines start together: at the backcast's lean_started (Blend Lean's first trade,
  // owner 24 Oct) or else the first trade any version opened.
  const famStart = () => family.map((s) => firstOpened(strategyLiveSet(cur, s, lg).trades || [])).filter(Boolean).sort()[0];
  const bcStart = parent.st?.backcast?.lean_started ? String(parent.st.backcast.lean_started).slice(0, 10) : null;
  const start = overlay ? [bcStart, famStart()].filter(Boolean).sort()[0] : firstOpened(trades, curve);
  const markLabel = family.slice(1).some((v) => v.st?.adds === "shadow") ? "rule set (judged from here)" : "filter set (judged from here)";
  const chart = overlay && overlay.some((o) => o.curve.length) ? overlayCurveChart(overlay, definedAt, start, markLabel)
    : curve.length ? curveChart(curve, start) : `<p class="muted">The chart starts when the first trade settles.</p>`;
  const judged = x.variantOf ? sinceDefinedHtml(cur, x, parent, lg) : "";
  // Blend Lean's line is the new rule backcast from its first trade (owner, 24 Oct).
  const bc = x.st?.backcast;
  const backcast = bc?.applied ? `<p class="note backcast-note"><b>Backcast before ${esc(dayName(String(bc.until).slice(0, 10)))}:</b> this line counts the ${bc.trades} Lean trade${bc.trades === 1 ? "" : "s"} from before the rule that ${bc.trades === 1 ? "was" : "were"} inside the range (${bc.settled} settled), descriptive only since the range was chosen after seeing them; from then on it is the trades the rule opened. The old rule's line (all odds) is its own ↳ folder under Moneyline.</p>` : "";
  const done = trades.filter((t) => t.status !== "open");
  const shown = done.slice(0, state.pfShown);
  const doneProfit = done.reduce((a, t) => a + (t.profit || 0), 0);
  return `
    ${title}
    ${banner}
    ${tiles}
    ${caveat}
    ${ci}
    ${backcast}
    ${judged}
    ${drvPanel}
    <div class="section-title">Profit over time</div>
    <div class="card pf-curve">${chart}</div>
    ${sizingHtml(`kz-${x.key}`, x.st?.sizing, lg ? "every competition" : "")}
    ${liveOddsBandsHtml(cur, x, lg, drv)}
    <div class="section-title">Open · ${open.length}${open.length ? ` · $${openRisk.toFixed(0)} at risk` : ""}</div>
    ${open.length ? `<div class="card list">${open.map(pfTradeRow).join("")}</div>` : `<div class="card"><span class="muted">${awaiting.length ? "No upcoming or in-play trades." : "No open trades."}</span></div>`}
    ${awaiting.length ? `<div class="section-title">Awaiting result · ${awaiting.length}</div><p class="note await-note">These matches have finished. ${AWAIT_NOTE} Their close and CLV are final.</p><div class="card list">${awaiting.map(pfTradeRow).join("")}</div>` : ""}
    ${done.length ? `<div class="section-title">Settled · ${done.length} · <span class="${signCls(doneProfit)}">${usd(doneProfit, 2)}</span></div><div class="card list">${shown.map(pfTradeRow).join("")}</div>${done.length > shown.length ? `<button class="more" id="pf-more">Show more (${done.length - shown.length} left)</button>` : ""}` : ""}
    ${impliedVsRealizedHtml(trades)}
    ${pfBreakdowns(set.breakdowns || {})}
    ${rules}`;
}
// Portfolio as drill-down lists (owner, 24 Oct): the tab opens on one row per portfolio, a
// portfolio opens on one row per strategy (shadow filters filed under their parent), and
// a strategy opens on its Live paper / Backtest / Model views. state.pfLevel = "home",
// "portfolio" or "strategy"; the Portfolio tab button, tapped again, goes back to the top.
// Anytime goalscorer is hidden while nobody is working on it (owner, 24 Oct).
const PF_HIDDEN = new Set(["goalscorer"]);
const CHEV = `<span class="chev" aria-hidden="true">›</span>`;
// Comeback bets keeps its one-row list (the lead's brief), like the multi-strategy portfolios.
const pfMulti = (cur) => cur.id === CB_ID || pfStrategies(cur).length > 1;
// The level on screen: a hidden portfolio, or a strategy level for a portfolio with one
// strategy's folder, falls back sensibly.
function pfLevelOn() {
  const lv = state.pfLevel || "home";
  if (lv === "home") return "home";
  const cur = pfCurrent();
  if (!cur || PF_HIDDEN.has(cur.id)) return "home";
  return lv === "portfolio" && !pfMulti(cur) ? "strategy" : lv;
}
// "Portfolio › Moneyline ›": each step goes back up a level.
function pfCrumbs(cur, toPortfolio) {
  const steps = [`<button class="crumb" data-pfnav="home">${toPortfolio ? "Portfolio" : "‹ Portfolio"}</button>`];
  if (toPortfolio) steps.push(`<button class="crumb" data-pfnav="portfolio">${esc(cur.name)}</button>`);
  return `<nav class="crumbs" aria-label="Back to">${toPortfolio ? '<span class="crumb-back" aria-hidden="true">‹</span>' : ""}${steps.join('<span class="crumb-sep" aria-hidden="true">›</span>')}</nav>`;
}
const pfPill = (cur) => `<span class="pill st-${esc(cur.status)}">${esc(cur.status === "closed" && cbData()?.verdict === "fail" ? "Closed: no edge" : PF_STATUS[cur.status] || cur.status)}</span>`;
function pfHomeHtml() {
  const all = portfolios();
  const shown = all.filter((p) => p.status !== "retired" && !PF_HIDDEN.has(p.id));
  const retired = all.filter((p) => p.status === "retired");
  const rows = shown.map((p) => {
    const st = pfStrategies(p);
    const names = st.filter((x) => !archivedStrat(x)).map((x) => x.label);
    const old = st.filter(archivedStrat).length;
    const extra = old ? `${old} archived` : "";
    const m = tradeStats(p.live?.trades || []);
    const cbHead = p.id === CB_ID ? cbHeadline() : null;
    const right = cbHead ? cbHead.right : m.n
      ? `<b class="${signCls(m.profit)}">${usd(m.profit, 2)}</b><small>${m.n} settled${m.open ? ` · ${m.open} open` : ""}</small>`
      : `<b class="muted">–</b><small>${m.open ? `${m.open} open` : "no trades"}</small>`;
    return `
      <button class="row-btn folder" data-pfid="${esc(p.id)}">
        <span class="who"><b>${esc(p.name)}</b> ${pfPill(p)}<span class="meta">${cbHead ? cbHead.meta : `${names.length > 1 ? `${names.length} strategies: ` : ""}${esc(names.join(", "))}${extra ? ` · ${extra}` : ""}`}</span></span>
        <span class="vals">${right}</span>${CHEV}
      </button>`;
  }).join("");
  return `
    <div class="section-title">Portfolios</div>
    <div class="card list folders">${rows}</div>
    <p class="note">Each portfolio holds that market's paper strategies. Profit is all its strategies together, in every competition, $10 a trade. Paper trades: no money is staked.${shown.some((p) => p.status === "closed") ? " A closed portfolio is research that finished without ever trading: its figure is the test's return per $1, not live profit." : ""}</p>
    ${retired.length ? `<div class="pf-retired">Retired: ${retired.map((p) => `<button class="linkish" data-pfid="${esc(p.id)}">${esc(p.name)}</button>`).join(", ")}</div>` : ""}`;
}
// One row per strategy (owner, 25 Oct: each its own, none filed under another): its live
// record, and its backtest headline underneath. A shadow test (corners' "below 60%") says so;
// an archived old rule (Lean's all odds) has the replay at every price as its backtest.
function stratMeta(cur, x, m) {
  if (x.st?.adds === "shadow") return `Old rule, until ${x.st?.defined_at ? monthDay(x.st.defined_at) : "the change"} · tracked with no bets`;
  if (archivedTrack(x)) return `Archived ${monthDay(PF_ARCHIVED[cur.id]?.since)} · still logs every ${ruleText(x) ? `${ruleText(x).replace("+", "")}-edge ` : ""}pick`;
  const f = x.st?.filter;
  if (x.variantOf && f && isNum(f.max)) return `Only the ${ruleText(x) ? `${ruleText(x).replace("+", "")}-edge ` : ""}picks model (f) gives under a ${pct(f.max)} chance to win${x.st?.defined_at ? ` · judged from ${monthDay(x.st.defined_at)}` : ""}`;
  if (x.variantOf) return `Shadow test · places no bets${x.st?.defined_at ? ` · judged from ${monthDay(x.st.defined_at)}` : ""}`;
  const rule = ruleText(x);
  return `${cur.status === "testing" && !m.open && !m.n ? "In testing" : `Live${m.since ? ` since ${monthDay(m.since)}` : ""}`}${rule ? ` · ${rule}` : ""}`;
}
function strategyFolders(cur, strats, lg) {
  const rows = strats.map((x) => {
    const m = tradeStats(strategyLiveSet(cur, x, lg).trades || []);
    const right = x.st?.error ? `<b class="muted">–</b><small>unavailable</small>` : m.n
      ? `<b class="${signCls(m.profit)}">${usd(m.profit, 2)}</b><small>${m.won}–${m.lost} · ${signedPct(m.roi)}</small>`
      : `<b class="muted">–</b><small>${m.open ? `${m.open} open` : "no trades"}</small>`;
    // Shadow tests have no backtest of their own; an archived rule has its replay.
    const h = x.variantOf && !archivedStrat(x) ? null : backtestHeadline(cur, x, lg);
    const bt = x.variantOf && !archivedStrat(x) ? "" : `<span class="bt">Backtest: ${h ? `<b class="${h.cls}">${h.v}</b> · ${esc(h.sub)}` : "none"}</span>`;
    return `
      <button class="row-btn folder strat${archivedStrat(x) ? " archived" : ""}" data-pfstrat="${esc(x.key)}">
        <span class="who"><b>${esc(x.label)}</b><span class="meta">${stratMeta(cur, x, m)}</span>${bt}</span>
        <span class="vals">${right}</span>${CHEV}
      </button>`;
  }).join("");
  return `<div class="card list folders">${rows}</div>`;
}
// The folder note on an "under N% to win" line, from its filter and defined_at.
function cutNote(x) {
  if (!x) return "";
  const cut = pct(x.st.filter.max), edge = ruleText(x).replace("+", "");
  return ` "Under ${cut} to win" lines are cut from the archived ${edge ? `${edge} ` : ""}tracks: the same paper trades, keeping only the bets model (f) gives under a ${cut} chance to win (a push, the stake back, is left out of that chance).${x.st?.defined_at ? ` Judged from ${monthDay(x.st.defined_at)} on closing line value against Pinnacle.` : ""}`;
}
function pfFolderHtml(cur, strats) {
  if (cur.id === CB_ID) return cbFolderHtml(cur);
  const lg = leagueOn();
  return `
    ${pfCrumbs(cur, false)}
    <h2 class="pf-title">${esc(cur.name)} ${pfPill(cur)}</h2>
    <div class="pf-head"><span>${esc(cur.note || "")}</span></div>
    ${leagueFilter()}
    <div class="section-title">Strategies</div>
    ${strategyFolders(cur, strats.filter((x) => !archivedStrat(x)), lg)}
    <p class="note">Open a strategy for its live paper trades, its backtest and its model.${cutNote(strats.find((x) => x.variantOf && !archivedStrat(x) && isNum(x.st?.filter?.max)))}</p>
    ${strats.some(archivedStrat) ? `<div class="section-title">Archive</div>${strategyFolders(cur, strats.filter(archivedStrat), lg)}<p class="note">${strats.some((x) => x.st?.adds === "shadow") ? "Old rules, still tracked on the picks they would have made so the current rule can be compared with them. They place no bets." : "Kept out of the main list: these tracks still log every pick at their rule, and the lines above are cut from them."}</p>` : ""}`;
}
function pfStrategyHtml(cur, allStrats) {
  if (cur.id === CB_ID) return cbStrategyHtml(cur);
  const multi = allStrats.length > 1;
  // Moneyline and Team corners have a third view: each strategy's model and its calibration.
  const hasModel = cur.id === "moneyline" || cur.id === "corners";
  const views = [["live", "Live paper"], ["backtest", "Backtest"], ...(hasModel ? [["model", "Model"]] : [])];
  const view = views.some(([k]) => k === state.pfView) ? state.pfView : "live";
  const toggle = `
    <div class="segmented small-seg" role="group" aria-label="Which view">
      ${views.map(([k, l]) => `<button data-pf="${k}" class="${view === k ? "on" : ""}" aria-pressed="${view === k}">${l}</button>`).join("")}
    </div>`;
  const x = pfSelected(allStrats, cur);
  // Variants have no backtest or model of their own: those views show the parent's.
  const parent = pfParent(allStrats, x);
  const research = view === "backtest" && cur.backtest?.kind === "research";
  const byComp = cur.id === "moneyline" || cur.id === "corners";
  const lg = byComp && !research ? leagueOn() : "";
  const m = tradeStats(strategyLiveSet(cur, x, lg).trades || []);
  const rule = ruleText(x);
  const sub = x.st?.adds === "shadow"
    ? `Archived: the rule until ${x.st?.defined_at ? monthDay(x.st.defined_at) : "the change"}, tracked with no bets beside ${esc(parent.label)}`
    : archivedTrack(x)
    ? `Archived ${monthDay(PF_ARCHIVED[cur.id]?.since)}: still logs every pick; the under-60% line is cut from it`
    : x.variantOf && isNum(x.st?.filter?.max)
    ? `${esc(parent.label)}'s picks that model (f) gives under a ${pct(x.st.filter.max)} chance to win${x.st?.defined_at ? ` · judged from ${monthDay(x.st.defined_at)}` : ""}`
    : x.variantOf
    ? `Shadow test of ${esc(parent.label)}: places no bets${x.st?.defined_at ? ` · judged from ${monthDay(x.st.defined_at)}` : ""}`
    : multi ? `${cur.status === "testing" && !m.open && !m.n ? "In testing" : `Live${m.since ? ` since ${monthDay(m.since)}` : ""}`}${rule ? ` · ${rule}` : ""}` : "";
  const head = multi
    ? `${pfCrumbs(cur, true)}<h2 class="pf-title">${esc(x.label)}</h2>${sub ? `<div class="pf-sub">${sub}</div>` : ""}`
    : `${pfCrumbs(cur, false)}<h2 class="pf-title">${esc(cur.name)} ${pfPill(cur)}</h2><div class="pf-head"><span>${cur.status === "retired" ? `<b>${esc(cur.name)}</b>, read-only history. ` : ""}${esc(cur.note || "")}</span></div>`;
  // Lean's replay is the old rule (every price); the new rule's own figure is its range's bands.
  const leanBt = view === "backtest" && parent.key === "lean" ? btPart(state.data.portfolio?.strategy_backtests?.lean, "lean", lg) : null;
  const range = parent.rule?.odds_range;
  const inRange = leanBt && range ? leanRangeBt(leanBt.part, range) : null;
  const parentNote = x.st?.adds === "shadow" && view === "backtest"
    ? `<p class="note">This replay is the old rule: Blend Lean at every price, the same replay ${esc(parent.label)} is measured against.</p>`
    : x.variantOf && view !== "live" ? `<p class="note">A shadow test has no ${view === "model" ? "model" : "backtest"} of its own: this is ${esc(parent.label)}'s.</p>`
    : x.key === "lean" && inRange?.bets ? `<p class="note lean-range-bt">This replay is the old rule, at every price. Inside the ${rangeWords(range)} range it placed ${inRange.bets.toLocaleString()} bets, ROI ${signedPct(inRange.roi)}: the ${range[0].toFixed(2)}–2.00 and 2.00–${range[1].toFixed(2)} rows under "Where the bets sit on the odds".</p>` : "";
  const top = `${head}${byComp && !research ? leagueFilter() : ""}${toggle}${parentNote}`;
  if (view === "model") return `${top}${modelViewHtml(cur, parent, lg)}`;
  if (view === "backtest") return pfClassic(top);
  return `${top}${liveStrategyHtml(cur, x, lg, allStrats)}`;
}
function viewPortfolio() {
  const level = pfLevelOn();
  if (level === "home") return pfHomeHtml();
  const cur = pfCurrent();
  const allStrats = pfStrategies(cur);
  return level === "portfolio" ? pfFolderHtml(cur, allStrats) : pfStrategyHtml(cur, allStrats);
}

// ---------- Portfolio "Comeback bets": closed research, never traded (lead brief, 26 Oct) ----------
// data.comeback_research (Research lab's lab/comeback_research.json, passed through by
// publish): {verdict, verdict_text, closed_at, cost, runs, preregistered, designs: {C (judged),
// B, pooled (descriptive)}, bets [{date, lg, home, away, fav, ht, odds, implied, won, design}],
// phase1, timelines, notes}. Chances 0–1, ROI per $1, ranges [lo, hi]. Its one strategy has
// two views in place of Live / Backtest / Model: Result and Why.
const CB_ID = "comebacks";
const cbData = () => state.data?.comeback_research || null;
const cbStrat = () => ({ key: "comeback_ht", label: "$1 on the trailing favourite at half-time", st: null, rule: null });
const cbPts = (v) => (isNum(v) ? `${v >= 0 ? "+" : "−"}${Math.abs(v * 100).toFixed(1)}` : "–"); // points of chance
// The like-for-like pair (lead, 26 Oct): the price's chance and the gap are on the bets with
// a fair price (`implied_matches`), so the win rate beside them is implied + gap on those same
// bets, not the rate on every bet. {won, implied, gap, gap_ci95, priced} or null.
function cbPair(d) {
  if (!d || !isNum(d.implied)) return null;
  const gap = isNum(d.gap) ? d.gap : d.win_rate - d.implied;
  return { won: d.implied + gap, implied: d.implied, gap, gap_ci95: d.gap_ci95, priced: d.implied_matches };
}
const cbPricedText = (pr) => (isNum(pr?.priced) ? `, on the ${num(pr.priced)} bets with a fair price` : "");
const cbOdds = (d) => (isNum(d) ? `${american(d)} (${d.toFixed(2)})` : "–");
const cbDate = (d) => new Date(`${d}T12:00:00Z`).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
const cbMonth = (d) => new Date(`${d}T12:00:00Z`).toLocaleDateString(undefined, { month: "short", year: "numeric", timeZone: "UTC" });
// Wilson 95% range for a share won of n.
function wilson(p, n) {
  if (!isNum(p) || !n) return [null, null];
  const z = 1.96, d = 1 + (z * z) / n, c = (p + (z * z) / (2 * n)) / d, h = (z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n))) / d;
  return [Math.max(0, c - h), Math.min(1, c + h)];
}
// The top-level row and the strategy row: the judged design's headline, never live profit.
function cbHeadline() {
  const c = cbData()?.designs?.C;
  if (!c) return { right: `<b class="muted">–</b><small>no result in this update</small>`, meta: "Research, never traded" };
  return {
    right: `<b class="${signCls(c.roi)}">${signedPct(c.roi)}</b><small>${num(c.bets)} bets · per $1</small>`,
    meta: `Research, closed ${cbData().closed_at ? monthDay(`${cbData().closed_at}T12:00:00Z`) : ""} · never traded`,
  };
}
function cbFolderHtml(cur) {
  const h = cbHeadline(), cr = cbData();
  const row = `
    <button class="row-btn folder strat" data-pfstrat="${cbStrat().key}">
      <span class="who"><b>${esc(cbStrat().label)}</b><span class="meta">Bought test, judged on design C · ${cr?.designs?.C ? `${num(cr.designs.C.wins)} of ${num(cr.designs.C.bets)} won` : "no result yet"}</span><span class="bt">Result: ${cr?.verdict === "fail" ? "no edge" : esc(cr?.verdict || "–")}</span></span>
      <span class="vals">${h.right}</span>${CHEV}
    </button>`;
  return `
    ${pfCrumbs(cur, false)}
    <h2 class="pf-title">${esc(cur.name)} ${pfPill(cur)}</h2>
    <div class="pf-head"><span>${esc(cur.note || "")}</span></div>
    <div class="section-title">Strategy</div>
    <div class="card list folders">${row}</div>
    <p class="note">Open it for the result (what the test found, in dollars per $1 bet) and why (the free research that led to it, and what it means).</p>`;
}
function cbStrategyHtml(cur) {
  const views = [["result", "Result"], ["why", "Why"]];
  const view = views.some(([k]) => k === state.pfView) ? state.pfView : "result";
  const toggle = `
    <div class="segmented small-seg" role="group" aria-label="Which view">
      ${views.map(([k, l]) => `<button data-pf="${k}" class="${view === k ? "on" : ""}" aria-pressed="${view === k}">${l}</button>`).join("")}
    </div>`;
  const cr = cbData();
  const head = `${pfCrumbs(cur, true)}<h2 class="pf-title">${esc(cbStrat().label)}</h2><div class="pf-sub">Closed${cr?.closed_at ? ` ${monthDay(`${cr.closed_at}T12:00:00Z`)}` : ""}: no edge found, never traded</div>`;
  if (!cr || !cr.designs?.C) return `${head}${toggle}<div class="empty">The comeback research result isn't in this update. It comes back with the next site update.</div>`;
  return `${head}${toggle}${view === "why" ? cbWhyHtml(cr) : cbResultHtml(cr)}`;
}
// Rows of "how often it won" (dot and Wilson range) against a reference chance (tick: the
// market's price or the score-blind model's), blue where it won at least as often.
// refRead: the reference in the readout, unescaped app text ("the in-play price's").
function cbRowsChart(id, rows, refName, refRead) {
  rows = rows.filter((r) => isNum(r.won) && r.n);
  if (!rows.length) return "";
  const rr = rows.map((r) => ({ ...r, ci: r.ci || wilson(r.won, r.n) }));
  const vals = rr.flatMap((r) => [r.ref, r.ci[0], r.ci[1]]).filter(isNum);
  const lo = Math.max(0, Math.floor((Math.min(...vals) - 0.02) * 10) / 10), hi = Math.min(1, Math.ceil((Math.max(...vals) + 0.02) * 10) / 10);
  const x = (v) => ((v - lo) / (hi - lo || 1)) * 100;
  const step = hi - lo > 0.5 ? 0.2 : 0.1;
  const ticks = [];
  for (let v = Math.ceil(lo / step - 1e-9) * step; v <= hi + 1e-9; v += step) ticks.push(v);
  const body = rr.map((r, i) => {
    const tone = r.won >= r.ref ? "pos" : "neg";
    return `<div class="eb-row rc-row" data-stop="${i}" data-rcrow="${i}" data-rc="${id}">
      <span class="eb-label">${esc(r.label)}<span class="meta">${num(r.n)} ${r.unit || "matches"}</span></span>
      <span class="eb-plot"><span class="eb-range ${tone}" style="left:${x(r.ci[0])}%;width:${Math.max(0.5, x(r.ci[1]) - x(r.ci[0]))}%"></span><span class="eb-dot imp" style="left:${x(r.ref)}%"></span><span class="eb-dot real ${tone}" style="left:${x(r.won)}%"></span></span>
    </div>`;
  }).join("");
  const read = (r) => `<b>${esc(r.label)}</b>, ${num(r.n)} ${r.unit || "matches"}: won ${pct(r.won, 1)} (range ${pct(r.ci[0], 0)} to ${pct(r.ci[1], 0)}) against ${refRead} ${pct(r.ref, 1)}${r.extra ? `; ${r.extra}` : ""}.`;
  scrubCharts[id] = { n: rr.length, rows: true, readoutId: `rc-readout-${id}`, readout: (i) => read(rr[i]) };
  const axis = `<div class="eb-axis"><span></span><span class="eb-plot">${ticks.map((v) => `<span style="left:${x(v)}%">${pct(v)}</span>`).join("")}</span></div>`;
  return `<div class="card eb-card scrub-rows" ${scrubAttrs(id, rr.length, "Point at or drag across the rows, or use the arrow keys")}>
    <div class="eb-legend"><span><i class="eb-key imp"></i>${esc(refName)}</span><span><i class="eb-key real pos"></i>Won (range)</span></div>
    ${body}${axis}
    <div class="eb-readout" id="rc-readout-${id}" aria-live="polite">${read(rr[0])}</div>
  </div>`;
}
const CB_SERIES = [["C", "Design C (judged)"], ["B", "Design B (first look)"], ["pooled", "C and B together"]];
// Profit over time at $1 a bet, one line per design (C solid, B and the two together dashed).
function cbProfitChart(cr) {
  const bets = (cr.bets || []).filter((b) => b.date && isNum(b.odds));
  if (!bets.length) return "";
  const series = CB_SERIES.map(([k, name]) => {
    const mine = bets.filter((b) => k === "pooled" || b.design === k);
    const byDay = new Map();
    let cum = 0;
    for (const b of mine) {
      cum += b.won ? b.odds - 1 : -1;
      const d = byDay.get(b.date) || { date: b.date, n: 0, won: 0 };
      d.n += 1; d.won += b.won ? 1 : 0; d.cum = cum;
      byDay.set(b.date, d);
    }
    return { name, curve: [...byDay.values()] };
  }).filter((s) => s.curve.length);
  const readout = (ser, rows, i) => {
    const d = rows[0][i].date;
    const parts = ser.map((o, k) => `${esc(o.name)} ${usd(rows[k][i].cum, 2)}`);
    return `${rows[0][i].origin ? "Before the first bet" : esc(cbDate(d))} · ${parts.join(" · ")}`;
  };
  return overlayCurveChart(series, null, bets[0].date, "", { id: "cbcv", readout, dateLabel: cbMonth, aria: "Profit over time at $1 a bet, design C judged, B and both together descriptive" });
}
function cbRange(id, rows, label) {
  return rangeChart(id, rows.map((g) => ({
    label: label(g), sub: `${num(g.bets)} bets`, v: g.roi, lo: g.roi_ci95?.[0], hi: g.roi_ci95?.[1],
    readout: `<b>${esc(label(g))}</b>: ${num(g.bets)} bets, ${num(g.wins)} won (${pct(g.win_rate, 1)} vs the price's ${pct(g.implied, 1)}); ROI ${signedPct(g.roi)}${rngText(g.roi_ci95)} per $1.`,
  })), (v) => signedPct(v, 0));
}
function cbResultHtml(cr) {
  const c = cr.designs.C, b = cr.designs.B, all = cr.designs.pooled;
  const cost = cr.cost || {};
  const pair = cbPair(c);
  const tile = (label, value, sub, cls = "") => `<div class="tile"><div class="label">${label}</div><div class="value ${cls}">${value}</div><div class="sub">${sub}</div></div>`;
  const tiles = `<div class="tiles pf-tiles">
    ${tile("Bets (design C)", num(c.bets), `${num(c.wins)} won · $1 each`)}
    ${pair ? tile("Won", pct(pair.won, 1), `vs ${pct(pair.implied, 1)} the price implied${cbPricedText(pair)}: ${cbPts(pair.gap)} points${pair.gap_ci95 ? ` (${cbPts(pair.gap_ci95[0])} to ${cbPts(pair.gap_ci95[1])})` : ""}`, signCls(pair.gap)) : tile("Won", pct(c.win_rate, 1), "no fair price to compare")}
    ${tile("Return per $1", signedPct(c.roi), c.roi_ci95 ? `95% range ${signedPct(c.roi_ci95[0])} to ${signedPct(c.roi_ci95[1])}` : "", signCls(c.roi))}
    ${tile("Cost in credits", num(cost.total), [cost.design_c ? `C ${num(cost.design_c)}` : "", cost.design_b ? `B ${num(cost.design_b)}` : "", cost.probes ? `probes ${num(cost.probes)}` : ""].filter(Boolean).join(" · "))}
  </div>`;
  const others = [b, all].filter(Boolean).map((d) => `<li>${esc(d.label)}: ${num(d.bets)} bets, ${num(d.wins)} won, ${signedPct(d.roi)}${rngText(d.roi_ci95)} per $1 (descriptive).</li>`).join("");
  const bands = cbRowsChart("cbband", (c.by_band || []).map((g) => ({
    label: g.hi >= 1 ? `${pct(g.lo)}+ favourite` : `${pctRange(g.lo, g.hi)} favourite`, n: g.bets, unit: "bets", won: g.win_rate, ref: g.implied,
    extra: `ROI ${signedPct(g.roi)}${rngText(g.roi_ci95)} per $1`,
  })), "In-play price's chance", "the in-play price's");
  const shown = state.cbShown || 50;
  const judged = (cr.bets || []).filter((x) => x.design === "C").slice().reverse();
  const betRows = judged.slice(0, shown).map((x) => `<tr><td>${esc(cbDate(x.date))}</td><td>${esc(leagueShort(x.lg))}</td><td>${esc(x.home)} v ${esc(x.away)}<span class="meta">${esc(x.fav === "home" ? x.home : x.away)} behind ${esc(x.ht || "")} at half-time</span></td><td class="num">${cbOdds(x.odds)}</td><td>${x.won ? '<b class="pos-text">Won</b>' : "Lost"}</td></tr>`).join("");
  return `
    <div class="verdict neg">${esc(cr.verdict_text || "No edge.")}</div>
    <p class="note">No live trades: this idea was tested on bought historical in-play prices and closed without ever opening a paper trade. The numbers below are that test, $1 a bet.</p>
    ${tiles}
    <p class="note">At Betfair's exchange after its 2% commission instead: ${signedPct(c.betfair_roi)}${rngText(c.betfair_roi_ci95)} per $1.${isNum(c.not_priced) && c.not_priced ? ` ${num(c.not_priced)} matches had no fresh in-play price and aren't counted.` : ""}</p>
    <div class="section-title">Profit over time ($1 a bet)</div>
    <div class="card pf-curve">${cbProfitChart(cr)}</div>
    <p class="note">Design C is the judged test, pre-registered before its prices were bought. Design B (the first look) and the two together are shown for description only.</p>
    <ul class="slopes">${others}</ul>
    <div class="section-title">Return by league (design C)</div>
    ${cbRange("cblg", c.by_league || [], (g) => leagueName(g.code))}
    <div class="section-title">Return by season (design C)</div>
    ${cbRange("cbse", c.by_season || [], (g) => g.season)}
    <p class="note cb-mix-note">In these rows and the ones below, the win rate counts every bet, but the price's chance only the bets with a fair price${isNum(c.implied_matches) ? ` (${num(c.implied_matches)} of ${num(c.bets)} overall)` : ""}, so the two can differ slightly from a like-for-like comparison.</p>
    <div class="section-title">Won vs the price, by how big a favourite</div>
    ${bands}
    <p class="note">Rows group the trailing favourites by their chance of winning before kickoff. Blue: they won at least as often as the in-play price implied; red: less. Each row is a few hundred bets at most, so the ranges are wide and one row can land either side by chance; the judged answer is the whole test above.</p>
    <details class="fold mk-fold cb-bets"${state.cbShown ? " open" : ""}><summary>The ${num(judged.length)} judged bets, newest first</summary>
      <div class="card" style="padding:8px 14px"><table class="mkts mk-table cb-table"><thead><tr><th>Date</th><th>Lg</th><th>Match</th><th>Odds</th><th>Result</th></tr></thead><tbody>${betRows}</tbody></table></div>
      ${judged.length > shown ? `<button class="more" data-cbmore="1">Show more (${num(judged.length - shown)} left)</button>` : ""}
    </details>`;
}
function cbWhyHtml(cr) {
  const p1 = cr.phase1 || {}, tl = cr.timelines || {};
  const p1rows = [
    p1.ht_behind && { label: "Behind at half-time", n: p1.ht_behind.matches, won: p1.ht_behind.won, ref: p1.ht_behind.model },
    p1.conceded_first && { label: "Conceded first", n: p1.conceded_first.matches, won: p1.conceded_first.won, ref: p1.conceded_first.model },
  ].filter(Boolean);
  // Per minute band on its benchmarked matches (those with a fair pre-match price), as the totals.
  const minRows = (tl.by_minute || []).map((m) => ({ label: `${(m.label || `${m.lo}–${m.hi}`).replace("-", "–")} min`, n: m.benchmarked ?? m.matches, won: m.won, ref: m.model }));
  const runs = cr.runs || {};
  const runList = Object.entries(runs).map(([k, v]) => `${esc(k.replace(/_/g, " "))} ${esc(Array.isArray(v) ? v.join(", ") : v)}`).join("; ");
  const pre = cr.preregistered || {};
  return `
    <div class="section-title">1. The idea, from free data</div>
    <p class="note">${p1.ht_behind ? `Favourites that were behind at half-time went on to win ${pct(p1.ht_behind.won, 1)} of ${num(p1.ht_behind.matches)} matches, where a fair model that ignores the score expected ${pct(p1.ht_behind.model, 1)}.` : ""}${isNum(p1.tests) ? ` ${p1.tests_above_zero} of ${p1.tests} pre-registered checks came out above zero.` : ""}</p>
    ${cbRowsChart("cbp1", p1rows, "Score-blind model", "the score-blind model's")}
    <div class="section-title">2. When the underdog scores first, by minute</div>
    <p class="note">${isNum(tl.matches) ? `${num(tl.matches)} matches with the favourite known before kickoff (${pct(tl.joined_share, 1)} joined to the results). The underdog scored first in ${pct(tl.trail_first_share, 1)} of them${isNum(tl.trail_first) ? ` (${num(tl.trail_first)})` : ""}; the favourite then won ${pct(tl.won, 1)} against the model's ${pct(tl.model, 1)}${isNum(tl.benchmarked) ? `, on the ${num(tl.benchmarked)} matches with a fair benchmark` : ""}${tl.gap_ci95 ? ` (${cbPts(tl.gap)} points, range ${cbPts(tl.gap_ci95[0])} to ${cbPts(tl.gap_ci95[1])})` : ""}.` : ""} ${esc(tl.note || "")}</p>
    ${cbRowsChart("cbmin", minRows, "Score-blind model", "the score-blind model's")}
    <div class="section-title">3. What it means</div>
    <div class="verdict neg">The in-play price already covers the comeback, and overrates it.</div>
    <p class="note">The free data compared comebacks with a model that ignores the score, so the gap it found is that model's blind spot, not the market's. The bookmakers' in-play price already expects favourites to fight back, and in the judged test it expected too much: trailing favourites won ${pct(cbPair(cr.designs.C)?.won ?? cr.designs.C.win_rate, 1)} of the time against the ${pct(cr.designs.C.implied, 1)} the price implied${cbPricedText(cbPair(cr.designs.C))}. Backing them lost money at DraftKings and at Betfair's exchange.${isNum(tl.priced_test_cost) ? ` A priced test of "the underdog scored first" would cost about ${num(tl.priced_test_cost)} credits and isn't recommended.` : ""}</p>
    <details class="fold mk-fold"><summary>How this was measured</summary>
      ${cr.question ? `<p class="note"><b>Question.</b> ${esc(cr.question)}</p>` : ""}
      ${cr.rule ? `<p class="note"><b>Rule.</b> ${esc(cr.rule)}</p>` : ""}
      ${[p1.note, ...(cr.notes || [])].filter(Boolean).map((n) => `<p class="note">${esc(n)}</p>`).join("")}
      ${pre.c || pre.b ? `<p class="note"><b>Pre-registered</b> before any prices were bought: design C in commit ${esc(pre.c || "–")}, design B in commit ${esc(pre.b || "–")}.</p>` : ""}
      ${runList ? `<p class="note small-note">Runs: ${runList}.</p>` : ""}
    </details>`;
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
const isLean = (t) => t.strategy === "lean" && !isPlayer(t) && !isCorner(t);
const TRADE_FIELDS = [
  ["Match", (t) => `${t.home} v ${t.away}`],
  ["Kickoff", (t) => kickoffText(t.kickoff)],
  ["Pick", (t) => tradeLabel(t)],
  ["Strategy", (t) => (isLean(t) ? "Blend Lean (1σ+)" : "Model 12% (your live test)"), (t) => !isPlayer(t) && !isCorner(t) && t.source === "live" && (leanOn() || !!t.strategy)],
  ["Player", (t) => `${t.player} (${t.team || "?"}${t.position ? `, ${t.position}` : ""})`, "player"],
  ["Result", (t) => (t.actual != null ? `${countWord(t.market, t.actual)}${t.started != null ? (t.started ? ", started" : ", came on") : ""}` : "–"), "player"],
  ["Result", (t) => (t.actual != null ? `${t.team} had ${t.actual} corners${t.push ? ": exactly the line, a push (stake back)" : ""}` : "–"), "corners"],
  ["Model's expected corners", (t) => (t.model_mean != null ? `${t.team} ${Number(t.model_mean).toFixed(1)}` : "–"), "corners"],
  ["Entry odds", (t) => `${american(t.odds)} (${t.odds.toFixed(2)})`],
  ["Odds fetched", (t) => (t.odds_fetched_at ? kickoffText(t.odds_fetched_at) : "–")],
  ["Opened", (t) => `${kickoffText(t.opened_at)} (${hoursText(t.hours_to_kickoff)} before kickoff)`],
  [(t) => (isLean(t) || (isPlayer(t) && t.model_p_base != null && Math.abs(t.model_p_base - t.model_p) > 1e-4) ? "Chance used (blend)" : "Model chance"), (t) => pct(t.model_p, 1)],
  ["Raw model chance", (t) => pct(t.p_model, 1), (t) => isLean(t) && t.p_model != null],
  ["Break-even at entry", (t) => pct(1 / t.odds, 1), isLean],
  ["What carried it", (t) => `${DRIVER_NAME[driverOf(t) || "unknown"]}${t.z_market != null ? ` (${sigmaText(t.z_market)} without the model's term)` : ""}${t.driver_from === "odds_log" ? "; worked out later from the logged price" : ""}`, isLean],
  ["Above break-even", (t) => `${sigmaText(t.z)} (${tierName(t)}${t.sigma != null ? `; σ = ${ptsText(t.sigma)}` : ""})`, (t) => isLean(t) && t.z != null],
  ["Raw model chance", (t) => pct(t.model_p_base, 1), (t) => isPlayer(t) && t.model_p_base != null && Math.abs(t.model_p_base - t.model_p) > 1e-4],
  ["Push chance", (t) => pct(t.p_push, 1), (t) => isCorner(t) && t.p_push > 0],
  ["Without team news", (t) => pct(t.model_p_base, 1), (t) => !isPlayer(t) && !isCorner(t) && !isLean(t)],
  ["Team news applied", (t) => (t.news_applied ? "Yes" : "No"), "match"],
  ["Edge at entry", (t) => signedPct(t.edge)],
  ["Threshold", (t) => (isLean(t) ? `+${Number(t.threshold).toFixed(0)}σ` : pct(t.threshold))],
  ["Rule", (t) => (isCorner(t) ? `Fixed edge, model (f) vs ${BOOK_NAME[cornerBook(t)]} (live test)` : t.rule === "fixed_raw" ? "Fixed edge, model alone (live test)" : t.rule === "lean_1sigma" ? "Blend 1σ+ above the price's break-even" : "Learned minimum, blended chance"), (t) => !isPlayer(t) && !!t.rule],
  ["Stake", (t) => `$${t.stake}`],
  [(t) => (tradePhase(t) === "upcoming" ? "Latest odds" : "Closing odds"), (t) => priceBoth(t.close_odds)],
  [(t) => (tradePhase(t) === "upcoming" ? "Latest price taken" : "Close taken"), (t) => { const m = closeMinutes(t); return m == null ? "–" : `${minutesText(m)} before kickoff${closeApprox(t) ? " (approximate close)" : ""}`; }, (t) => !isPlayer(t)],
  ["Bookmaker", (t) => (isCorner(t) ? BOOK_NAME[cornerBook(t)] : ({ pinnacle: "Pinnacle", draftkings: "DraftKings", fanduel: "FanDuel" })[t.bookmaker] || t.bookmaker || (isPlayer(t) ? "FanDuel" : "DraftKings"))],
  [(t) => `${tradePhase(t) === "upcoming" ? "Latest" : "Closing"} over / under`, (t) => `${american(t.close_prices?.over)} / ${american(t.close_prices?.under)}`, (t) => isCorner(t) && !!t.close_prices],
  ["Book implied chance", (t) => (t.implied != null ? pct(t.implied, 1) : "–")],
  [(t) => (isPlayer(t) ? "Price move to close" : tradePhase(t) === "upcoming" ? "CLV so far" : isCorner(t) ? `CLV vs ${closeWho(t)}` : "CLV vs DraftKings"), (t) => (isDkCorner(t) && t.clv_dk == null && t.close_prices && (t.close_prices.over == null || t.close_prices.under == null) ? "– (one-sided close)" : signedPct(tradeClv(t)))],
  ["Beat the close", (t) => { const b = tradeBeat(t), c = tradeClv(t); return b == null ? (c == null || tradePhase(t) === "upcoming" ? "–" : c > 0 ? "Yes" : "No") : b ? "Yes" : "No"; }, (t) => !isPlayer(t)],
  ["CLV vs Pinnacle", (t) => signedPct(t.clv_pinnacle), "match"],
  ["CLV vs Pinnacle's close", (t) => signedPct(t.clv_pinnacle), isDkCorner],
  ["Pinnacle's close, over / under", (t) => (t.pinnacle_close_prices ? `${american(t.pinnacle_close_prices.over)} / ${american(t.pinnacle_close_prices.under)}` : "not quoted at this line"), isDkCorner],
  ["Status", (t) => (t.status === "open" ? `Open · ${PHASE_LABEL[tradePhase(t)]}` : t.status[0].toUpperCase() + t.status.slice(1))],
  ["Score", (t) => t.score || "–", (t) => !isPlayer(t)],
  ["Profit", (t) => (t.profit == null ? "–" : usd(t.profit, 2))],
  ["Season", (t) => seasonName(t.season)],
  ["Look", (t) => (isCorner(t) ? ({ look: "about a day before kickoff", close: "just before kickoff" })[t.look] || "–" : t.look === "lineup" ? "after lineups" : t.look ? `${t.look} before kickoff` : "–")],
  ["Model", (t) => [t.model_ref?.commit, t.model_ref?.xg_weight != null ? `xG weight ${t.model_ref.xg_weight}` : null, t.model_ref?.matches_fit ? `${t.model_ref.matches_fit} matches` : null].filter(Boolean).join(" · ") || "–"],
  ["Trade id", (t) => t.id],
];

// The trade sheet's state line for a live trade: upcoming, in play, awaiting its result, or settled.
function tradeStateLine(t) {
  if (t.source !== "live") return "";
  const ph = tradePhase(t);
  const m = minsSinceKickoff(t.kickoff);
  let text;
  if (ph === "upcoming") text = m == null ? "" : `Kicks off in ${hoursText(-m / 60)}. Prices are still moving: the odds and CLV below are the latest logged.`;
  else if (ph === "inplay") text = `Kicked off ${hoursText(m / 60)} ago. Prices after kickoff aren't logged, so the close and CLV below are final. It settles after full time.`;
  else if (ph === "awaiting") text = `Kicked off ${hoursText(m / 60)} ago. ${AWAIT_NOTE} The close and CLV below are final.`;
  else text = t.status === "void" ? (t.push ? "A push: stake back." : "Void: stake back.") : `${t.status === "won" ? "Won" : "Lost"}${t.profit != null ? ` ${usd(t.profit, 2)}` : ""}.`;
  const label = ph === "settled" ? "Settled" : PHASE_LABEL[ph];
  return `<p class="trade-state"><b class="phase-tag ${ph}">${label}</b>${text ? ` ${esc(text)}` : ""}</p>`;
}

function tradeHtml(t) {
  const kind = isPlayer(t) ? "player" : isCorner(t) ? "corners" : "match";
  const rows = TRADE_FIELDS.filter(([, , only]) => (typeof only === "function" ? only(t) : !only || only === kind))
    .map(([k, f]) => `<tr><td>${esc(typeof k === "function" ? k(t) : k)}</td><td>${esc(f(t))}</td></tr>`).join("");
  return `
    <div class="detail">
      <p class="muted" style="margin:0;font-size:13px">${t.source === "live" ? "Live paper trade" : "Backtest trade"}${isPlayer(t) || isCorner(t) ? ` · ${esc(t.home)} v ${esc(t.away)}` : ""}</p>
      <h2 id="sheet-title">${tradeTitle(t)}</h2>
      ${tradeStateLine(t)}
      <div class="card" style="padding:8px 14px;margin-top:10px">
        <table class="kv"><tbody>${rows}</tbody></table>
      </div>
      ${t.source === "live" && kellyStakeLine(t) ? `<p class="note kelly-line">${esc(kellyStakeLine(t))}. Shown for comparison only: flat $10 is the record.</p>` : ""}
    </div>`;
}

const VIEWS = { matches: viewMatches, ratings: viewRatings, record: viewRecord, portfolio: viewPortfolio, explore: viewExplore };

function render() {
  $("#view").innerHTML = VIEWS[state.tab]();
  document.querySelectorAll(".tabbar button").forEach((b) => {
    const on = b.dataset.tab === state.tab;
    b.classList.toggle("active", on);
    b.setAttribute("aria-current", on ? "page" : "false");
  });
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
  $("#updated").textContent = `${n > 1 ? `${n} leagues ·` : d.league} ${d.season} · updated ${ago} · App ${APP_VERSION}`;
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
    // The Portfolio tab, tapped again, goes back to its folders.
    if (t.dataset.tab === "portfolio" && state.tab === "portfolio") state.pfLevel = "home";
    setTab(t.dataset.tab);
  } else if (t.dataset.pfnav) {
    state.pfLevel = t.dataset.pfnav; state.pfShown = 15; state.pfMarket = ""; state.pfSeason = "";
    render();
    window.scrollTo(0, 0);
  } else if (t.dataset.edge !== undefined) {
    state.exploreEdge = t.dataset.edge === "reset" ? null : Number(t.dataset.edge);
    render();
  } else if (t.dataset.pf) {
    state.pfView = t.dataset.pf; state.pfShown = 15; state.pfMarket = ""; state.pfSeason = "";
    render();
  } else if (t.dataset.pfdriver !== undefined) {
    state.pfDriver = t.dataset.pfdriver; state.pfShown = 15; state.lbShown = 20;
    render();
  } else if (t.dataset.cbmore !== undefined) {
    state.cbShown = (state.cbShown || 50) + 100;
    render();
  } else if (t.dataset.lbmore !== undefined) {
    state.lbShown += 20;
    render();
  } else if (t.dataset.pfstrat) {
    if (pfCurrent()?.id === "corners") { state.pfStratCorners = t.dataset.pfstrat; store.set("pfStratCorners", state.pfStratCorners); }
    else { state.pfStrat = t.dataset.pfstrat; store.set("pfStrat", state.pfStrat); }
    state.pfLevel = "strategy"; state.pfShown = 15; state.pfMarket = ""; state.pfSeason = "";
    render();
    window.scrollTo(0, 0);
  } else if (t.dataset.recbet) {
    state.recBet = t.dataset.recbet;
    render();

  } else if (t.dataset.gotoPf) {
    state.pfId = t.dataset.gotoPf; state.pfLevel = "portfolio"; state.pfView = "live"; state.pfShown = 15; state.pfMarket = ""; state.pfSeason = "";
    store.set("pfId", state.pfId);
    setTab("portfolio");

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
    state.pfId = t.dataset.pfid; state.pfLevel = "portfolio"; state.pfShown = 15; state.pfMarket = ""; state.pfSeason = "";
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
    checkForUpdate();
    newerAppDeployed().then((newer) => { if (newer) reloadOnce(); });
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

// ---------- app updates ----------
// iOS keeps a home-screen app's page alive, so a new app.js only runs after a reload. sw.js
// skips waiting and claims the page, so a new service worker taking over means a new app:
// reload once. Check for one whenever the app comes back to the front and on ↻ (which also
// compares the deployed sw.js's version with this one's, in case no worker is running).
let swReg = null;
function reloadOnce() {
  try {
    const last = Number(sessionStorage.getItem("appReloadAt") || 0);
    if (Date.now() - last < 15000) return; // never loop
    sessionStorage.setItem("appReloadAt", String(Date.now()));
  } catch { /* storage blocked: the page-level flag below still guards */ }
  if (reloadOnce.done) return;
  reloadOnce.done = true;
  location.reload();
}
function checkForUpdate() { if (swReg) swReg.update().catch(() => {}); }
async function newerAppDeployed() {
  try {
    const res = await fetch("sw.js", { cache: "no-store" });
    const m = (await res.text()).match(/soccer-model-(v\d+)/);
    return Boolean(m && m[1] !== APP_VERSION);
  } catch { return false; }
}
if ("serviceWorker" in navigator) {
  // A change from no controller (the first install) has no older app to replace: no reload.
  let controlled = Boolean(navigator.serviceWorker.controller);
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (controlled) reloadOnce();
    controlled = true;
  });
  window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").then((r) => { swReg = r; }).catch(() => {}));
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") checkForUpdate(); });
}
load();

// Scrubbable charts: a mouse scrubs on hover, a finger or pen while pressed; release keeps
// the spot; arrow keys (and Home/End) step when the chart has focus.
// The readout line and legend inside a chart's wrapper don't move the selection.
const scrubTarget = (ev) => (ev.target.closest?.(".eb-readout, .eb-legend, .curve-legend") ? null : ev.target.closest?.("[data-scrub]"));
document.addEventListener("pointerdown", (ev) => {
  const w = scrubTarget(ev);
  if (!w) return;
  w.dataset.drag = "1";
  scrubTo(w, scrubIndex(w, ev.clientX, ev.clientY));
});
document.addEventListener("pointermove", (ev) => {
  const w = scrubTarget(ev);
  if (!w || (ev.pointerType !== "mouse" && !w.dataset.drag)) return;
  scrubTo(w, scrubIndex(w, ev.clientX, ev.clientY));
});
const endDrag = () => document.querySelectorAll("[data-scrub][data-drag]").forEach((w) => delete w.dataset.drag);
document.addEventListener("pointerup", endDrag);
document.addEventListener("pointercancel", endDrag);
document.addEventListener("keydown", (ev) => {
  const w = document.activeElement?.closest?.("[data-scrub]");
  if (!w) return;
  const sc = scrubCharts[w.dataset.scrub];
  if (!sc) return;
  const n = scrubLen(sc);
  const at = sc.at ?? (sc.rows ? -1 : n - 1);
  const keys = { ArrowLeft: at - 1, ArrowRight: at + 1, Home: 0, End: n - 1 };
  if (sc.rows) Object.assign(keys, { ArrowUp: at - 1, ArrowDown: at + 1 });
  const to = keys[ev.key];
  if (to === undefined) return;
  ev.preventDefault();
  scrubTo(w, to);
});
