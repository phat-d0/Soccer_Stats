// Smoke test for the web app: serve web/ with the fixture data, visit every tab and
// sub-view in Chromium, and fail on console errors, page errors, horizontal scroll or
// template leftovers ("undefined", "NaN", "${").
//
//   node tests/web/smoke.mjs                 # checks only
//   node tests/web/smoke.mjs --shots DIR     # also saves a screenshot of every step
//
// Needs Playwright (global npm install is fine) and Chromium at /opt/pw-browsers/chromium
// (or set CHROMIUM=/path). Exits 0 with "SKIP" when either is missing.
// Rebuild the fixture with: uv run python tests/web/make_fixture.py

import { execSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const CHROMIUM = process.env.CHROMIUM || "/opt/pw-browsers/chromium";
const NOW = new Date("2026-10-06T12:00:00Z"); // the fixture's build time
const args = process.argv.slice(2);
const shotsDir = args.includes("--shots") ? resolve(args[args.indexOf("--shots") + 1]) : null;

function loadPlaywright() {
  const req = createRequire(import.meta.url);
  try { return req("playwright"); } catch { /* try the global install */ }
  try {
    const root = execSync("npm root -g", { encoding: "utf8" }).trim();
    return req(join(root, "playwright"));
  } catch { return null; }
}
const pw = loadPlaywright();
if (!pw || !existsSync(CHROMIUM)) {
  console.log(`SKIP: ${pw ? `no Chromium at ${CHROMIUM}` : "Playwright not installed"}`);
  process.exit(0);
}

// ---------- the site: web/ plus the fixture JSON, in a temp dir ----------
const site = mkdtempSync(join(tmpdir(), "pl-site-"));
cpSync(join(ROOT, "web"), site, { recursive: true });
cpSync(join(ROOT, "tests/fixtures/web"), site, { recursive: true });
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".png": "image/png", ".webmanifest": "application/manifest+json" };
const server = createServer((req, res) => {
  const path = decodeURIComponent(new URL(req.url, "http://x").pathname);
  const file = join(site, path === "/" ? "index.html" : path);
  if (!file.startsWith(site) || !existsSync(file)) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { "content-type": TYPES[extname(file)] || "application/octet-stream" });
  res.end(readFileSync(file));
});
await new Promise((ok) => server.listen(0, "127.0.0.1", ok));
const BASE = `http://127.0.0.1:${server.address().port}/`;

// ---------- steps: each leaves the app on one view ----------
const tab = (name) => async (p) => { await p.click(`.tabbar button[data-tab="${name}"]`); };
const click = (sel) => async (p) => { await p.click(sel); };
const sheet = async (p) => { await p.waitForSelector("#sheet:not([hidden])"); };
const closeSheet = async (p) => { await p.click(".sheet-close"); };
const STEPS = [
  ["matches", tab("matches")],
  ["match-sheet", async (p) => { await p.click('button.match[data-fixture="0"]'); await sheet(p); }, true],
  // ESPN lineups: confirmed XI with subs folded; an ESPN injury FPL already lists is dropped.
  ["match-sheet-lineups", async (p) => {
    await p.click("details.xi-subs > summary");
    const text = await p.textContent("#sheet-body");
    for (const want of ["Confirmed XI", "Substitutes", "Source: ESPN", "Jaka Bijol"]) {
      if (!text.includes(want)) throw new Error(`Lineups lack "${want}"`);
    }
    const saka = await p.locator("#sheet-body .absence", { hasText: "Bukayo Saka" }).count();
    if (saka !== 1) throw new Error(`FPL's absence listed ${saka} times (ESPN repeat?)`);
    await p.locator(".xi-grid").scrollIntoViewIfNeeded();
  }, true],
  // Goals over/under: total goals (model only: DraftKings sends no 2.5 total on our feed),
  // each team's goals with FanDuel's prices.
  ["match-sheet-goals", async (p) => {
    await p.click("details.goals-fold > summary");
    const text = await p.textContent("#sheet-body");
    for (const want of ["Total goals", "Each team's goals", "FD over", "doesn't beat the market"]) {
      if (!text.includes(want)) throw new Error(`Goals section lacks "${want}"`);
    }
    if (text.includes("DK over")) throw new Error("Goals section still shows a DraftKings 2.5 column");
    await p.locator("details.goals-fold").scrollIntoViewIfNeeded();
  }, true],
  // Team corners: model (f) beside Pinnacle; the first card's pick already has an open trade.
  ["match-sheet-corners", async (p) => {
    const text = await p.textContent("#sheet-body");
    for (const want of ["Corners", "model expects", "PIN", "Paper trade open", "push", "pulled toward the league average"]) {
      if (!text.includes(want)) throw new Error(`Corners section lacks "${want}"`);
    }
    await p.locator("table.corners-t").first().scrollIntoViewIfNeeded();
    // Pinnacle's open trade (home 4.5) marks Pinnacle's row only, never DraftKings' row at
    // the same line; the away 3.5 DraftKings line is one-sided ("–" for the under).
    const home = p.locator("#sheet-body table.corners-t").first();
    if (!(await home.locator('tr.pick[data-book="pinnacle"][data-line="4.5"]').count())) throw new Error("Pinnacle's open trade isn't marked on its line");
    if (await home.locator('tr.pick[data-book="draftkings"][data-line="4.5"]').count()) throw new Error("Pinnacle's trade marks DraftKings' line");
    if (!(await p.locator('#sheet-body .badge.paper[data-book="pinnacle"]').count()) || (await p.locator('#sheet-body .badge.paper[data-book="draftkings"]').count())) throw new Error("open-trade badges on the wrong book");
    const one = await p.locator('#sheet-body table.corners-t >> nth=1').locator('tr[data-book="draftkings"][data-line="3.5"]').innerText();
    if (!one.includes("DK") || !one.includes("–")) throw new Error(`one-sided DraftKings line: ${one}`);
    if (!text.includes("DK = DraftKings") || !text.includes("prices only one side")) throw new Error("corners note lacks the DraftKings key");
  }, true],
  // The other direction: a DraftKings trade (Ipswich v Fulham, home 4.5) marks DraftKings'
  // row and badge only, never Pinnacle's line at the same team line.
  ["match-sheet-corners-dk", async (p) => {
    await closeSheet(p); await p.locator("button.match", { hasText: "Ipswich" }).first().click(); await sheet(p);
    const home = p.locator("#sheet-body table.corners-t").first();
    if (!(await home.locator('tr.pick[data-book="draftkings"][data-line="4.5"]').count())) throw new Error("DraftKings' open trade isn't marked on its line");
    if (await home.locator('tr.pick[data-book="pinnacle"][data-line="4.5"]').count()) throw new Error("DraftKings' trade marks Pinnacle's line");
    const badges = await p.locator("#sheet-body .badge.paper").allInnerTexts();
    if (badges.length !== 1 || !badges[0].includes("(DraftKings)")) throw new Error(`open-trade badges: ${badges.join(" | ")}`);
    await home.scrollIntoViewIfNeeded();
  }, true],
  // The recommended minimum edge, then exploring another one (folded under "Explore other edges").
  ["match-sheet-no-lineups", async (p) => {
    await closeSheet(p);
    const badges = await p.locator("button.match .badge.lineup").count();
    if (badges !== 1) throw new Error(`expected 1 "Lineups confirmed" badge, got ${badges}`);
    await p.click('button.match[data-fixture="1"]'); await sheet(p);
    const text = await p.textContent("#sheet-body");
    if (!text.includes("Not out yet") || text.includes("Confirmed XI")) throw new Error("unconfirmed lineup shown wrongly");
  }, true],
  // The competition filter (shown because the fixture has a second league, SP1).
  ["matches-league-sp1", async (p) => { await closeSheet(p); await p.click('.chip[data-league="SP1"]'); }],
  // A match with no odds: model-only goals, no bookmaker columns.
  ["match-sheet-sp1-goals", async (p) => {
    await p.click("button.match >> nth=0"); await sheet(p); await p.click("details.goals-fold > summary");
    const text = await p.textContent("#sheet-body");
    if (!text.includes("model only") || text.includes("FD over") || text.includes("DK over")) throw new Error("SP1 goals should be model only");
    if (!text.includes("No Pinnacle or DraftKings price for this match yet") || !text.includes("fits less well in La Liga")) throw new Error("SP1 corners should be model only, with the weaker-fit note");
  }, true],
  // The Champions League (owner, 13 Oct): goals-only model chances ("exp. goals"), and an
  // odds-only card for a club no football-data league covers.
  ["matches-league-ucl", async (p) => {
    await closeSheet(p); await p.click('.chip[data-league="UCL"]');
    const cards = await p.locator("button.match").allInnerTexts();
    if (cards.length !== 2) throw new Error(`expected 2 Champions League cards, got ${cards.length}`);
    if (!cards[0].includes("exp. goals") || cards[0].includes("xG")) throw new Error(`UCL model card: ${cards[0]}`);
    if (!cards[1].includes("No model chance") || cards[1].includes("exp. goals")) throw new Error(`UCL odds-only card: ${cards[1]}`);
  }],
  ["match-sheet-ucl", async (p) => {
    await p.click("button.match >> nth=0"); await sheet(p);
    const text = await p.textContent("#sheet-body");
    if (!text.includes("Arsenal v FC Porto") || !text.includes("Scorelines")) throw new Error("UCL model sheet");
  }, true],
  ["match-sheet-ucl-odds-only", async (p) => {
    await closeSheet(p); await p.click("button.match >> nth=1"); await sheet(p);
    const text = await p.textContent("#sheet-body");
    for (const want of ["FK Qarabag v Chelsea", "No model chance", "No paper trade opens", "Champions League"]) {
      if (!text.includes(want)) throw new Error(`UCL odds-only sheet lacks "${want}"`);
    }
    if (text.includes("Scorelines")) throw new Error("UCL odds-only sheet shows model sections");
  }, true],
  ["matches-league-all", async (p) => { await closeSheet(p); await p.click('.chip[data-league=""]'); }],
  // With Blend Lean on (portfolio.strategy_order has "lean"), Matches shows Lean only (owner,
  // 11 Oct): Lean picks and Lean paper trades; the 12% test's picks and trades are under
  // Portfolio only.
  ["matches-lean-only", async (p) => {
    const text = await p.textContent("#view");
    for (const want of ["have a Blend Lean pick", "Each Lean pick opens one $10 paper trade", "under Portfolio → Moneyline", "Blend Lean rule"]) {
      if (!text.includes(want)) throw new Error(`Matches lacks "${want}"`);
    }
    for (const bad of ["Fixed 12% (your live test)", "a 12% pick", "12% would pick now", "Model 12% (second)", "First strategy"]) {
      if (text.includes(bad)) throw new Error(`Matches still shows "${bad}"`);
    }
    if (await p.locator("button.match .badge.second").count()) throw new Error("a 12% pick badge shows on Matches");
    const open = await p.locator("button.match .badge.paper").allInnerTexts();
    if (open.some((t) => t.includes("(12%)"))) throw new Error(`a 12% paper trade shows on Matches: ${open.join(" | ")}`);
    const d = await (await p.request.get(new URL("data.json", p.url()).href)).json();
    const ml = d.portfolio.portfolios.find((x) => x.id === "moneyline");
    const cards = new Set(d.fixtures.map((f) => `${f.home}|${f.away}`));
    const lean = ml.live.trades.filter((t) => t.strategy === "lean" && t.status === "open" && cards.has(`${t.home}|${t.away}`));
    if (!lean.length) throw new Error("the fixture has no open Lean trade on a listed match");
    const withLean = await p.evaluate(() => [...document.querySelectorAll("button.match")].filter((b) => [...b.querySelectorAll(".badge.paper")].some((x) => x.innerText.includes("(Lean)"))).map((b) => b.querySelector(".teams").innerText.replace(/\s+/g, " ")));
    for (const t of lean) if (!withLean.some((f) => f.includes(t.home) && f.includes(t.away))) throw new Error(`Lean trade ${t.id} isn't shown on Matches`);
  }],
  ["match-sheet-lean-only", async (p) => {
    await p.click("button.match:has(.badge.lean) >> nth=0"); await sheet(p);
    const text = await p.textContent("#sheet-body");
    if (text.includes("Model 12%") || text.includes("your live test")) throw new Error("the match sheet still shows the 12% test");
    if (!text.includes("the chance Blend Lean trades on") || !text.includes("Blend Lean")) throw new Error("the match sheet doesn't explain Blend Lean");
  }, true],
  // Without Blend Lean (older data): the owner's fixed live test flags exactly the matches
  // paper trades take (raw model, 12% against DraftKings' quoted price).
  ["matches-live-test", async (p) => {
    await closeSheet(p);
    await p.route("**/data.json", async (route) => {
      const res = await route.fetch();
      const d = await res.json();
      delete d.portfolio.strategy_order;
      await route.fulfill({ response: res, json: d });
    });
    await p.reload(); await p.waitForSelector("button.match");
    const text = await p.textContent("#view");
    for (const want of ["Owner's live test", "Fixed 12% (your live test)", "have a live-test pick", "the model alone shows"]) {
      if (!text.includes(want)) throw new Error(`Matches lacks "${want}"`);
    }
    if (await p.locator("button.match .badge.lean").count()) throw new Error("Lean badges without Blend Lean");
    const flagged = await p.evaluate(() => [...document.querySelectorAll("button.match")].filter((b) => b.querySelector(".badge:not(.paper):not(.lineup):not(.lean)")).map((b) => b.querySelector(".teams").innerText.replace(/\s+/g, " ")));
    const d = await (await p.request.get(new URL("data.json", p.url()).href)).json();
    const ml = d.portfolio.portfolios.find((x) => x.id === "moneyline");
    // Open fixed-rule trades on matches with a card in this build (the real ledger also has
    // trades on matches outside the synthetic fixture list).
    const cards = new Set(d.fixtures.map((f) => `${f.home}|${f.away}`));
    const taken = ml.live.trades.filter((t) => t.rule === "fixed_raw" && (t.strategy || "edge12") === "edge12" && t.status === "open" && cards.has(`${t.home}|${t.away}`));
    if (!taken.length) throw new Error("the fixture has no open fixed-rule trade on a listed match");
    for (const t of taken) {
      if (!flagged.some((f) => f.includes(t.home) && f.includes(t.away))) throw new Error(`paper trade ${t.id} isn't flagged on Matches`);
    }
    if (flagged.length < taken.length) throw new Error(`${flagged.length} flagged, ${taken.length} fixed-rule paper trades`);
    await p.click("details.edge-learned > summary");
    if (!(await p.textContent("details.edge-learned")).includes("Backtests found no edge level that made money")) throw new Error("learned result not shown");
  }],
  ["match-sheet-live-test", async (p) => {
    await p.click("button.match:has(.badge:not(.paper):not(.lineup)) >> nth=0"); await sheet(p);
    const text = await p.textContent("#sheet-body");
    if (!(text.includes("Live-test pick") || text.includes("Paper trade already open")) || !text.includes("model alone")) throw new Error("match sheet doesn't explain the live-test pick");
  }, true],
  // The learned rule (p_source "blend"): today's behaviour comes back.
  ["matches-learned-rule", async (p) => {
    await closeSheet(p);
    await p.unroute("**/data.json");
    await p.route("**/data.json", async (route) => {
      const res = await route.fetch();
      const d = await res.json();
      delete d.portfolio.strategy_order;
      d.portfolio.rule = { ...d.portfolio.rule, rule: "learned", p_source: "blend", threshold: null, threshold_source: "history" };
      for (const c of Object.keys(d.portfolio.rules)) d.portfolio.rules[c] = { threshold: null, source: "history", note: "No edge level beat the market.", rule: "learned", p_source: "blend" };
      await route.fulfill({ response: res, json: d });
    });
    await p.reload(); await p.waitForSelector("button.match");
    const text = await p.textContent("#view");
    if (text.includes("Fixed 12%") || text.includes("Live-test pick") || text.includes("12% pick")) throw new Error("learned rule still shows the live test");
    if (!text.includes("Nothing is flagged")) throw new Error("learned rule should flag nothing");
  }],
  // Explore other edges: on Matches only without Blend Lean (display only).
  ["matches-edge-2", async (p) => { await p.click("details.edge-explore > summary"); await p.click('button[data-edge="0.02"]'); }],
  ["matches-edge-reset", click('button[data-edge="reset"]')],
  // Paper trades skip low_data matches (paper.py); under the live test the app does too.
  ["matches-live-test-low-data", async (p) => {
    await p.unroute("**/data.json");
    await p.route("**/data.json", async (route) => {
      const res = await route.fetch();
      const d = await res.json();
      for (const f of d.fixtures) f.low_data = true;
      await route.fulfill({ response: res, json: d });
    });
    await p.reload(); await p.waitForSelector("button.match");
    const picks = await p.evaluate(() => document.querySelectorAll("button.match .badge:not(.paper):not(.lineup)").length);
    if (picks) throw new Error(`${picks} Lean picks on low-data matches, which paper trades skip`);
    if (!/\b0 of \d+ matches have a Blend Lean pick/.test(await p.textContent("#view"))) throw new Error("Lean count should be 0 on low-data matches");
  }],
  ["matches-restored", async (p) => {
    await p.unroute("**/data.json");
    await p.reload(); await p.waitForSelector("button.match");
  }],
  // Arsenal v Leeds holds a 12% trade on the draw and a Lean trade on its Strong away pick;
  // Matches shows only the Lean one. Newcastle v Tottenham has a Lean pick (home) and only
  // a 12% trade open: keyed on that trade, its Lean badge would read "Would pick now".
  ["matches-lean", async (p) => {
    await p.click('.chip[data-league=""]');
    const card = p.locator("button.match", { hasText: "Leeds" }).first();
    const lean = await card.locator(".badge.lean").innerText();
    if (!/Strong · 2\.6σ: Away/.test(lean) || lean.includes("Would pick") || !lean.includes("Market-driven")) throw new Error(`Lean badge: ${lean}`);
    const open = await card.locator(".badge.paper").allInnerTexts();
    if (!open.some((t) => t.includes("(Lean): Away")) || open.some((t) => t.includes("(12%)"))) throw new Error(`open badges: ${open.join(" | ")}`);
    if (await card.locator(".badge.second").count()) throw new Error("Arsenal v Leeds shows a 12% badge");
    const nt = p.locator("button.match", { hasText: "Tottenham" }).first();
    const ntLean = await nt.locator(".badge.lean").innerText();
    if (!ntLean.includes("Lean · 1.1σ: Home") || ntLean.includes("Would pick") || !ntLean.includes("Model-driven")) throw new Error(`Newcastle Lean badge: ${ntLean}`);
    if (await nt.locator(".badge.second, .badge.paper").count()) throw new Error("Newcastle shows a 12% badge or a trade it doesn't have under Lean");
    const text = await p.textContent("#view");
    for (const want of ["Blend Lean pick", "Blend Lean: 1σ", "σ by competition"]) if (!text.includes(want)) throw new Error(`Matches lacks "${want}"`);
  }],
  ["match-sheet-lean", async (p) => {
    await p.locator("button.match", { hasText: "Leeds" }).first().click(); await sheet(p);
    const text = await p.textContent("#sheet-body");
    for (const want of ["Blend Lean: paper trade already open", "Away", "+2.6σ", "Premier League σ = 1.5 points", "not on the model", "market-driven", "mkt +2.2σ"]) {
      if (!text.includes(want)) throw new Error(`Lean match sheet lacks "${want}"`);
    }
    if (text.includes("Model 12%")) throw new Error("the Lean match sheet still shows the 12% test");
    if (!(await p.locator("table.mkts.with-z th", { hasText: "σ" }).count())) throw new Error("no σ column");
  }, true],
  ["match-sheet-lean-new", async (p) => {
    await closeSheet(p); await p.locator("button.match", { hasText: "Tottenham" }).first().click(); await sheet(p);
    const text = await p.textContent("#sheet-body");
    if (!text.includes("Blend Lean pick (Lean, model-driven): Home") || !text.includes("A $10 paper trade opens") || !text.includes("so the model pushed it over 1σ")) throw new Error("Lean pick without a trade");
  }, true],
  ["match-sheet-lean-none", async (p) => {
    await closeSheet(p); await p.locator("button.match", { hasText: "Liverpool" }).first().click(); await sheet(p);
    const text = await p.textContent("#sheet-body");
    if (!text.includes("Blend Lean: no pick.") || !text.includes("a Lean pick needs +1σ")) throw new Error("no-pick Lean text");
  }, true],
  ["match-sheet-lean-sp1", async (p) => {
    await closeSheet(p); await p.click('.chip[data-league="SP1"]');
    const text = await p.textContent("#view");
    if (!text.includes("La Liga σ = 1.9 points")) throw new Error("SP1 sigma not in the edge panel");
    await p.click('.chip[data-league=""]');
  }],
  ["teams", tab("ratings")],
  ["players-stats", async (p) => { await p.click('button[data-tv="players"]'); await p.waitForSelector("#pl-list .bet-row"); }],
  ["players-stats-club", async (p) => { await p.selectOption("#pl-team", { index: 1 }); }],
  ["players-chart", async (p) => { await p.selectOption("#pl-team", ""); await p.click('button[data-plview="chart"]'); await p.waitForSelector("#dev-chart .dev-row"); await p.click("#dev-chart .dev-row >> nth=0"); }],
  ["players-chart-gxg", async (p) => { await p.selectOption("#pl-metric", "gxg"); }],
  ["player-sheet", async (p) => { await p.click('button[data-plview="list"]'); await p.click("#pl-list button[data-player] >> nth=0"); await sheet(p); }, true],
  ["players-model", async (p) => { await closeSheet(p); await p.click('button[data-plmode="model"]'); await p.waitForSelector("#pl-list .bet-row"); }],
  ["players-model-sheet", async (p) => { await p.click("#pl-list button[data-player] >> nth=0"); await sheet(p); }, true],
  // Teams tab per league (round 9): La Liga with Understat season stats, the Championship
  // goals-only with none. Ends on a Premier League player sheet for the next step.
  ["teams-sp1", async (p) => { await closeSheet(p); await p.click('button[data-tv="teams"]'); await p.click('.chip[data-league="SP1"]'); }],
  ["teams-e1", click('.chip[data-league="E1"]')],
  // The first load of La Liga's stats is held back 600 ms and the view re-renders meanwhile:
  // the request in flight must be reused (the fetch counter above fails on a second one).
  ["players-stats-sp1", async (p) => {
    await p.route("**/players_stats_SP1.json", async (route) => { await new Promise((r) => setTimeout(r, 600)); await route.continue(); });
    await p.click('.chip[data-league="SP1"]'); await p.click('button[data-tv="players"]'); await p.click('button[data-plmode="stats"]');
    await p.click('button[data-plmode="stats"]'); // re-render while the file is still loading
    await p.waitForSelector("#pl-list .bet-row");
  }],
  ["players-chart-sp1", async (p) => { await p.click('button[data-plview="chart"]'); await p.waitForSelector("#dev-chart .dev-row"); await p.click("#dev-chart .dev-row >> nth=0"); }],
  ["players-stats-e1", click('.chip[data-league="E1"]')],
  ["players-model-sp1", async (p) => { await p.click('.chip[data-league="SP1"]'); await p.click('button[data-plmode="model"]'); }],
  ["players-e0-sheet", async (p) => { await p.click('.chip[data-league="E0"]'); await p.click('button[data-plmode="stats"]'); await p.click('button[data-plview="list"]'); await p.click("#pl-list button[data-player] >> nth=0"); await sheet(p); }, true],
  ["record-match", async (p) => { await closeSheet(p); await tab("record")(p); }],
  ["record-match-bucket", click('.eb-row[data-ebrow="0"]')],
  // Blend trades through Blend Lean now: its tiles are Lean's Premier League backtest.
  ["record-match-blend-lean", async (p) => {
    await p.click('button[data-recdk="blend"]');
    const tiles = await p.locator(".tiles.lean-dk .tile").count();
    if (tiles !== 4) throw new Error(`Blend shows ${tiles} Lean tiles, not 4`);
    const text = await p.textContent("#view");
    for (const want of ["Blend Lean (1σ+), the rule the blend trades on now", "Premier League, Pinnacle's early prices 2019/20–2024/25", "from 351 bets at 1σ+", "−4.2%", "needed to break even", "vs Pinnacle's close", "No bets at a 12% edge"]) {
      if (!text.includes(want)) throw new Error(`Blend tab lacks "${want}"`);
    }
  }],
  ["record-match-raw", async (p) => {
    await p.click('button[data-recdk="raw"]');
    if (await p.locator(".tiles.lean-dk").count()) throw new Error("Lean tiles on Model alone");
  }],
  ["record-match-sp1", click('.chip[data-league="SP1"]')],
  ["record-match-all", click('.chip[data-league=""]')],
  ["record-player", click('button[data-recbet="player"]')],
  ["record-player-blend_3h", click('button[data-recstrat="blend_3h"]')],
  ["record-player-raw_3h", click('button[data-recstrat="raw_3h"]')],
  // Record → Goals & corners: three research cards, details folded, a range-plot readout.
  ["record-markets", async (p) => {
    await p.click('button[data-recbet="markets"]');
    const text = await p.textContent("#view");
    for (const want of ["Total goals", "Each team's goals", "Corners", "5 of 50", "No edge"]) {
      if (!text.includes(want)) throw new Error(`Goals & corners lacks "${want}"`);
    }
  }],
  ["record-markets-details", async (p) => {
    for (const d of await p.locator("details.mk-fold > summary").all()) await d.click();
    await p.click('.rc-row[data-rc="co"][data-rcrow="4"]');
    const readout = await p.textContent("#rc-readout-co");
    if (!readout.includes("Ligue 1") || !readout.includes("2025/26")) throw new Error(`corner readout: ${readout}`);
    const text = await p.textContent("#view");
    for (const run of ["37863800450", "38004172501", "38022942019"]) if (!text.includes(run)) throw new Error(`run ${run} not shown`);
  }],
  // The publish field missing (a broken or old build): a plain note, nothing else breaks.
  ["record-markets-missing", async (p) => {
    await p.route("**/data.json", async (route) => {
      const res = await route.fetch();
      const d = await res.json();
      delete d.markets_research;
      await route.fulfill({ response: res, json: d });
    });
    await p.reload(); await p.waitForSelector("button.match");
    await tab("record")(p); await p.click('button[data-recbet="markets"]');
    const text = await p.textContent("#view");
    if (!text.includes("aren't in this update")) throw new Error("no plain note when markets_research is missing");
  }],
  ["record-player-restored", async (p) => {
    await p.unroute("**/data.json");
    await p.reload(); await p.waitForSelector("button.match");
    await tab("record")(p); await p.click('button[data-recbet="player"]');
  }],
  ["record-player-trade", async (p) => { await p.click('button[data-recstrat="blend_lineup"]'); await p.click("button[data-trade] >> nth=0"); await sheet(p); }, true],
  ["portfolio-live", async (p) => {
    await closeSheet(p); await tab("portfolio")(p);
    const on = await p.locator('button[data-pfstrat].on').innerText();
    if (!on.includes("Blend Lean")) throw new Error(`default strategy: ${on}`);
    const text = await p.textContent("#view");
    for (const want of ["Paper strategies", "Model 12% (your live test)", "Profit, settled", "Record", "Avg edge", "Profit over time", "Open · ", "Settled · ", "Blend Lean (1σ+). A $10", "baseball app's Lean tier", "not from the model", "Wolves v Everton", "until a few hundred, results are mostly luck"]) {
      if (!text.includes(want)) throw new Error(`Lean live lacks "${want}"`);
    }
    if (text.includes("Nott'm Forest v Arsenal")) throw new Error("a 12% trade shows under Blend Lean");
    // Every trade row under Lean is a Lean trade (its id ends "|lean").
    const ids = await p.locator("#view button[data-trade]").evaluateAll((els) => els.map((e) => e.dataset.trade));
    if (!ids.length || ids.some((id) => !id.endsWith("|lean"))) throw new Error(`non-Lean rows under Lean: ${ids.join(", ")}`);
    // The strategy list: Lean's row carries its own settled record, 12% its own.
    const leanRow = await p.locator('button[data-pfstrat="lean"]').innerText();
    if (!/1σ\+/.test(leanRow) || !/2–1/.test(leanRow)) throw new Error(`Lean row: ${leanRow}`);
    // Profit over time: three settled days for Lean; tapping the first fills the readout.
    await p.locator(".chart.curve [data-cv='0']").dispatchEvent("click");
    const readout = await p.textContent("#cv-readout");
    if (!readout.includes("running total")) throw new Error(`curve readout: ${readout}`);
    const avg = await p.locator(".tile", { hasText: "Avg edge" }).locator(".value").innerText();
    if (!avg.endsWith("σ")) throw new Error(`Lean avg edge should be in σ: ${avg}`);
  }],
  ["portfolio-live-lean-trade", async (p) => {
    await p.click('button[data-trade="E0|2627|Arsenal|Leeds|lean"]'); await sheet(p);
    const text = await p.textContent("#sheet-body");
    for (const want of ["Blend Lean (1σ+)", "Chance used (blend)", "Above break-even", "+2.6σ (Strong", "+1σ"]) if (!text.includes(want)) throw new Error(`Lean trade sheet lacks "${want}"`);
  }, true],
  // Trade states (owner, 18 Oct): kicked off under ~2 h 15 min ago = "In play"; a finished
  // match still open waits in its own "Awaiting result" group, between Open and Settled.
  ["portfolio-live-states", async (p) => {
    await closeSheet(p);
    const text = await p.textContent("#view");
    for (const want of ["Awaiting result · 1", "These matches have finished. Results usually come in within a few hours of full time, and each trade then settles automatically.", "Their close and CLV are final", "finished, awaiting result (not in profit yet)"]) {
      if (!text.includes(want)) throw new Error(`Lean live lacks "${want}"`);
    }
    // Open = upcoming + in play only (Arsenal v Leeds upcoming, Aston Villa v Brentford in play).
    const openHead = await p.locator(".section-title", { hasText: "Open · " }).innerText();
    if (!/Open · 2 · \$20 at risk/i.test(openHead)) throw new Error(`open header: ${openHead}`);
    const inplay = await p.locator('button[data-trade="E0|2627|Aston Villa|Brentford|lean"]').innerText();
    if (!inplay.includes("In play") || !inplay.includes("Close") || inplay.includes("now ")) throw new Error(`in-play row: ${inplay}`);
    const waiting = await p.locator('button[data-trade="SP1|2627|Sevilla|Valencia|lean"]').innerText();
    if (!waiting.includes("Finished · awaiting result") || !waiting.includes("CLV")) throw new Error(`awaiting row: ${waiting}`);
    // The awaiting trade sits after the Awaiting result title and before Settled.
    const order = await p.evaluate(() => {
      const els = [...document.querySelectorAll("#view .section-title, #view button[data-trade]")];
      return els.map((e) => (e.dataset.trade ? `T:${e.dataset.trade}` : `H:${e.textContent.split(" · ")[0]}`));
    });
    const iA = order.indexOf("H:Awaiting result"), iS = order.indexOf("H:Settled"), iW = order.indexOf("T:SP1|2627|Sevilla|Valencia|lean");
    if (!(iA >= 0 && iA < iW && (iS < 0 || iW < iS))) throw new Error(`group order: ${order.join(", ")}`);
  }],
  ["portfolio-live-awaiting-sheet", async (p) => {
    await p.click('button[data-trade="SP1|2627|Sevilla|Valencia|lean"]'); await sheet(p);
    const text = await p.textContent("#sheet-body");
    for (const want of ["Finished · awaiting result", "Kicked off 5.0 h ago", "settles automatically", "Closing odds", "Close taken", "CLV vs DraftKings", "Open · Finished · awaiting result"]) {
      if (!text.includes(want)) throw new Error(`awaiting trade sheet lacks "${want}"`);
    }
    if (text.includes("Latest odds") || text.includes("CLV so far")) throw new Error("awaiting trade sheet still reads like an upcoming trade");
  }, true],
  ["portfolio-live-inplay-sheet", async (p) => {
    await closeSheet(p); await p.click('button[data-trade="E0|2627|Aston Villa|Brentford|lean"]'); await sheet(p);
    const text = await p.textContent("#sheet-body");
    for (const want of ["In play", "Kicked off 50 min ago", "Prices after kickoff aren't logged", "Closing odds"]) {
      if (!text.includes(want)) throw new Error(`in-play trade sheet lacks "${want}"`);
    }
  }, true],
  ["portfolio-live-lean-sp1", async (p) => { await closeSheet(p); await p.click('.chip[data-league="SP1"]'); if (!(await p.textContent("#view")).includes("Betis v Celta")) throw new Error("SP1 Lean trade"); }],
  // Blend Lean live split by driver: the side-by-side table, a chip on each Lean trade and
  // a filter (Wolves v Everton is model-driven, Spurs v Brentford market-driven).
  ["portfolio-live-lean-drivers", async (p) => {
    await p.click('.chip[data-league=""]');
    const text = await p.textContent("#view");
    for (const want of ["What carried each pick", "Market-driven", "Model-driven"]) if (!text.includes(want)) throw new Error(`Lean live lacks "${want}"`);
    await p.click("details.drv-fold > summary");
    const chip = await p.locator('button[data-trade="E0|2627|Wolves|Everton|lean"] .drv').innerText();
    if (chip !== "Model") throw new Error(`Wolves chip: ${chip}`);
    await p.click('button[data-pfdriver="model"]');
    const only = await p.textContent("#view");
    if (!only.includes("Wolves v Everton") || only.includes("Spurs v Brentford")) throw new Error("model-driven filter");
  }],
  ["portfolio-live-lean-market", click('button[data-pfdriver="market"]')],
  // With a competition chosen the driver filter shows counts only (by_driver isn't kept
  // per league): no profit or ROI for the group, and never "nothing settled yet".
  ["portfolio-live-lean-sp1-model", async (p) => {
    await p.click('.chip[data-league="SP1"]'); await p.click('button[data-pfdriver="model"]');
    const text = await p.textContent("#view");
    // The tiles count straight from the trades, so a competition plus a driver still has a
    // record: Betis v Celta won.
    if (!text.includes("Betis v Celta") || text.includes("nothing settled yet")) throw new Error("SP1 model-driven trades");
    const rec = await p.locator(".tile", { hasText: "Record" }).locator(".value").innerText();
    if (rec !== "1–0") throw new Error(`SP1 model-driven record: ${rec}`);
    // No summary covers a competition plus a driver, so no ROI range may show.
    if (text.includes("95% range on ROI")) throw new Error("SP1 model-driven shows a ROI range it has no summary for");
  }],
  // Blend Lean's backtest: the replay at Pinnacle (data-log's real lean_backtest.json).
  ["portfolio-lean-backtest", async (p) => {
    await p.click('.chip[data-league=""]'); await p.click('button[data-pfdriver=""]'); await p.click('button[data-pf="backtest"]');
    const text = await p.textContent("#view");
    for (const want of ["In the replay at Pinnacle's prices Lean", "Return by league", "Lean and Strong", "Market-driven and model-driven", "Bets, newest first", "units"]) {
      if (!text.includes(want)) throw new Error(`Lean backtest lacks "${want}"`);
    }
    await p.click('.rc-row[data-rc="lbroi"][data-rcrow="0"]');
    if (!(await p.textContent("#rc-readout-lbroi")).includes("bets")) throw new Error("ROI readout");
  }],
  ["portfolio-lean-backtest-model", async (p) => {
    await p.click('button[data-pfdriver="model"]');
    const chips = await p.locator(".bet-row .drv").allInnerTexts();
    if (!chips.length || chips.some((c) => c !== "Model")) throw new Error(`model filter chips: ${chips.join(",")}`);
    if (await p.locator("button[data-lbmore]").count()) await p.click("button[data-lbmore]");
  }],
  ["portfolio-lean-backtest-sp1", async (p) => {
    await p.click('button[data-pfdriver=""]'); await p.click('.chip[data-league="SP1"]');
    await p.click("details.mk-fold:has(summary:text('Seasons and fits')) > summary");
    if (!(await p.textContent("#view")).includes("matches to fit on")) throw new Error("SP1 fits");
  }],
  // The strategy rows' backtest headlines follow the competition chip (owner, 21 Oct).
  ["portfolio-backtest-headlines", async (p) => {
    const row = async (k) => (await p.locator(`button[data-pfstrat="${k}"] .vals`).innerText()).replace(/\s+/g, " ");
    const seen = {};
    for (const lg of ["", "E0", "SP1"]) {
      await p.click(`.chip[data-league="${lg}"]`);
      seen[lg] = await row("lean");
      const e12 = await row("edge12");
      if (lg === "SP1" ? !e12.includes("no backtest for this competition yet") : !e12.includes("Premier League · DraftKings")) throw new Error(`12% row under "${lg}": ${e12}`);
    }
    if (new Set(Object.values(seen)).size !== 3) throw new Error(`Lean headline doesn't follow the chip: ${JSON.stringify(seen)}`);
    if (!seen.SP1.includes("La Liga at Pinnacle") || !seen[""].includes("bets · at Pinnacle")) throw new Error(`Lean subs: ${JSON.stringify(seen)}`);
    // The Champions League isn't in the Lean backtest.
    await p.click('.chip[data-league="UCL"]');
    if (!(await row("lean")).includes("no backtest for this competition")) throw new Error(`UCL Lean row: ${await row("lean")}`);
    // Model 12% under La Liga: a plain note, never the Premier League's numbers.
    await p.click('.chip[data-league="SP1"]'); await p.click('button[data-pfstrat="edge12"]');
    const text = await p.textContent("#view");
    if (!text.includes("No Model 12% backtest for La Liga yet") || (await p.locator("#view .tiles").count())) throw new Error("12% SP1 backtest body");
    await p.click('button[data-pfstrat="lean"]');
  }],
  ["portfolio-live-edge12", async (p) => {
    await p.click('button[data-pf="live"]');
    await p.click('.chip[data-league=""]'); await p.click('button[data-pfstrat="edge12"]');
    const text = await p.textContent("#view");
    if (!text.includes("Your live test: fixed 12%") || text.includes("Wolves v Everton")) throw new Error("12% strategy view");
    const ids = await p.locator("#view button[data-trade]").evaluateAll((els) => els.map((e) => e.dataset.trade));
    if (!ids.length || ids.some((id) => id.endsWith("|lean"))) throw new Error(`a Lean trade shows under 12%: ${ids.join(", ")}`);
    const on = await p.locator('button[data-pfstrat].on').innerText();
    if (!on.includes("Model 12%") || !on.includes("12%+")) throw new Error(`12% row: ${on}`);
    const avg = await p.locator(".tile", { hasText: "Avg edge" }).locator(".value").innerText();
    if (!avg.endsWith("%")) throw new Error(`12% avg edge should be a %: ${avg}`);
    if (!(await p.locator(".chart.curve").count())) throw new Error("no profit chart for the 12% test");
    if (!text.includes("95% range on ROI")) throw new Error("12% live lacks paper.py's ROI range");
  }],
  ["portfolio-live-edge12-states", async (p) => {
    const text = await p.textContent("#view");
    if (!text.includes("Awaiting result · 1")) throw new Error("12% awaiting group");
    const waiting = await p.locator('button[data-trade="E0|2627|Bournemouth|Wolves"]').innerText();
    if (!waiting.includes("Finished · awaiting result")) throw new Error(`12% awaiting row: ${waiting}`);
    const inplay = await p.locator('button[data-trade="E0|2627|Brighton|Everton"]').innerText();
    if (!inplay.includes("In play") || !inplay.includes("No close logged")) throw new Error(`12% in-play row: ${inplay}`);
  }],
  ["portfolio-live-sp1", click('.chip[data-league="SP1"]')],
  ["portfolio-backtest-sp1", click('button[data-pf="backtest"]')],
  ["portfolio-live-all", async (p) => { await p.click('button[data-pf="live"]'); await p.click('.chip[data-league=""]'); }],
  // A settled live match trade whose close was taken early (approximate CLV; fixture data).
  ["portfolio-live-close-sheet", async (p) => { await p.click('button[data-trade="E0|2627|Spurs|Brentford"]'); await sheet(p); }, true],
  ["portfolio-live-trade", async (p) => { await closeSheet(p); await p.click("button[data-trade] >> nth=0"); await sheet(p); }, true],
  ["portfolio-backtest", async (p) => { await closeSheet(p); await p.click('button[data-pf="backtest"]'); await p.click("details.fold > summary"); }],
  ["portfolio-backtest-trade", async (p) => { await p.click("button[data-trade] >> nth=0"); await sheet(p); }, true],
  // An empty portfolio (in testing): its plain-English state, live and backtest.
  ["portfolio-goalscorer", async (p) => { await closeSheet(p); await p.click('button[data-pfid="goalscorer"]'); }],
  ["portfolio-goalscorer-live", click('button[data-pf="live"]')],
  // A retired portfolio, reached from the "Retired" link: its history, read-only.
  ["portfolio-retired-live", click('button.linkish[data-pfid="player_shots"]')],
  ["portfolio-retired-backtest", click('button[data-pf="backtest"]')],
  ["portfolio-retired-trade", async (p) => { await p.click("button[data-trade] >> nth=0"); await sheet(p); }, true],
  // Team corners (in testing): live paper with Pinnacle CLV and a push, a league chip, the
  // trade sheet, then the research backtest (no money tiles) with a range-plot readout.
  ["portfolio-corners", async (p) => {
    await closeSheet(p); await p.click('button[data-pfid="corners"]'); await p.click('button[data-pf="live"]');
    const text = await p.textContent("#view");
    for (const want of ["over 4.5 corners", "Push", "Pinnacle's close", "Live test: fixed 12% on model (f)", "Model (f) 12% vs Pinnacle", "Model (f) 12% vs DraftKings", "Profit over time"]) {
      if (!text.includes(want)) throw new Error(`Team corners lacks "${want}"`);
    }
    if (await p.locator('#view button[data-trade$="|dk"]').count()) throw new Error("a DraftKings trade shows under the Pinnacle track");
  }],
  // The DraftKings track: its own trades, CLV vs DraftKings' close with Pinnacle's as a
  // second line, a one-sided close without DraftKings CLV; Moneyline's selection untouched.
  ["portfolio-corners-dk", async (p) => {
    const before = await p.evaluate(() => localStorage.getItem("pfStrat"));
    await p.click('button[data-pfstrat="draftkings"]');
    const ids = await p.locator("#view button[data-trade]").evaluateAll((els) => els.map((e) => e.dataset.trade));
    if (ids.length !== 3 || ids.some((id) => !id.endsWith("|dk"))) throw new Error(`DraftKings track lists: ${ids.join(", ")}`);
    const text = await p.textContent("#view");
    for (const want of ["on model (f) against DraftKings", "vs DraftKings' close", "vs Pinnacle's close", "one-sided close", "Brighton over 5.5 corners"]) {
      if (!text.includes(want)) throw new Error(`DraftKings corners lacks "${want}"`);
    }
    if ((await p.evaluate(() => localStorage.getItem("pfStrat"))) !== before) throw new Error("the corner switch changed Moneyline's strategy");
  }],
  ["portfolio-corners-dk-trade", async (p) => {
    await p.click('button[data-trade="E0|2627|Aston Villa|Fulham|corners|away|4.5|dk"]'); await sheet(p);
    const text = await p.textContent("#sheet-body");
    for (const want of ["DraftKings", "Fixed edge, model (f) vs DraftKings", "– (one-sided close)", "CLV vs Pinnacle's close", "Pinnacle's close, over / under"]) {
      if (!text.includes(want)) throw new Error(`DraftKings corner trade sheet lacks "${want}"`);
    }
  }, true],
  ["portfolio-corners-pin-again", async (p) => { await closeSheet(p); await p.click('button[data-pfstrat="pinnacle"]'); }],
  ["portfolio-corners-awaiting", async (p) => {
    const text = await p.textContent("#view");
    if (!text.includes("Awaiting result · 1")) throw new Error("corners awaiting group");
    const row = await p.locator('button[data-trade="E0|2627|Crystal Palace|Brentford|corners|away|4.5"]').innerText();
    if (!row.includes("Finished · awaiting result") || !row.includes("Pinnacle's close")) throw new Error(`corner awaiting row: ${row}`);
  }],
  ["portfolio-corners-sp1", click('.chip[data-league="SP1"]')],
  ["portfolio-corners-push", async (p) => {
    await p.click('.chip[data-league=""]');
    await p.click('button[data-trade="E0|2627|Everton|Fulham|corners|away|5"]'); await sheet(p);
    const text = await p.textContent("#sheet-body");
    if (!text.includes("a push (stake back)") || !text.includes("CLV vs Pinnacle's close")) throw new Error("push trade sheet");
  }, true],
  ["portfolio-corners-backtest", async (p) => {
    await closeSheet(p); await p.click('button[data-pf="backtest"]');
    await p.click('.rc-row[data-rc="pfco"][data-rcrow="0"]');
    const text = await p.textContent("#view");
    for (const want of ["No money tiles", "finalist", "February"]) if (!text.includes(want)) throw new Error(`research backtest lacks "${want}"`);
    if (await p.locator(".tiles").count()) throw new Error("research backtest shows money tiles");
    await p.click("details.mk-fold > summary");
  }],
  ["record-markets-corners-link", async (p) => {
    await tab("record")(p); await p.click('button[data-recbet="markets"]');
    await p.click('button[data-goto-pf="corners"]');
    if (!(await p.textContent("#view")).includes("Team corners")) throw new Error("corners link didn't open the portfolio");
  }],
  ["portfolio-moneyline", async (p) => { await p.click('button[data-pfid="moneyline"]'); }],
  // Matches after kickoff (the clock moved past Arsenal v Leeds, 10 Oct 11:30 UTC): the
  // card says "In play", then "Finished"; its open trades read "In play" / "Awaiting
  // result" with the close, and no new pick shows.
  ["matches-in-play", async (p) => {
    await p.clock.setFixedTime(new Date("2026-10-10T12:10:00Z")); await tab("matches")(p);
    const card = p.locator("button.match", { hasText: "Leeds" }).filter({ hasText: "Arsenal" }).first();
    const text = await card.innerText();
    if (!text.includes("In play") || !text.includes("In play (Lean): Away")) throw new Error(`in-play card: ${text}`);
    if (await card.locator(".badge.lean").count()) throw new Error("a Lean pick shows on a match that has kicked off");
  }],
  ["matches-awaiting", async (p) => {
    await p.clock.setFixedTime(new Date("2026-10-10T15:00:00Z")); await tab("portfolio")(p); await tab("matches")(p);
    const card = p.locator("button.match", { hasText: "Leeds" }).filter({ hasText: "Arsenal" }).first();
    const text = await card.innerText();
    if (!text.includes("Finished") || !text.includes("Awaiting result (Lean): Away") || text.includes("Paper trade open")) throw new Error(`finished card: ${text}`);
  }],
  ["match-sheet-awaiting", async (p) => {
    await p.locator("button.match", { hasText: "Leeds" }).filter({ hasText: "Arsenal" }).first().click(); await sheet(p);
    const text = await p.textContent("#sheet-body");
    for (const want of ["Blend Lean: match finished, paper trade awaiting result", "settles automatically", "Awaiting result (Pinnacle): Arsenal over 4.5"]) {
      if (!text.includes(want)) throw new Error(`finished match sheet lacks "${want}"`);
    }
    if (text.includes("A $10 paper trade opens") || text.includes("nothing new opens here")) throw new Error("finished match sheet reads like an upcoming one");
  }, true],
  ["matches-reset-clock", async (p) => { await closeSheet(p); await p.clock.setFixedTime(NOW); await tab("portfolio")(p); await tab("matches")(p); }],
  ["explore", tab("explore")],
];
const MODES = [
  ["phone-light", { viewport: { width: 390, height: 844 }, colorScheme: "light", isMobile: true, hasTouch: true, deviceScaleFactor: 2 }],
  ["phone-dark", { viewport: { width: 390, height: 844 }, colorScheme: "dark", isMobile: true, hasTouch: true, deviceScaleFactor: 2 }],
  ["laptop", { viewport: { width: 1280, height: 900 }, colorScheme: "light" }],
];

const SHEET_SHOT_CSS = ".sheet{position:absolute;bottom:auto;min-height:100%} .sheet-panel{position:relative;max-height:none;margin-top:56px} body{overflow:visible!important}";
const failures = [];
const browser = await pw.chromium.launch({ executablePath: CHROMIUM });
try {
  if (shotsDir) mkdirSync(shotsDir, { recursive: true });
  for (const [mode, opts] of MODES) {
    const ctx = await browser.newContext({ ...opts, serviceWorkers: "block" });
    const page = await ctx.newPage();
    await page.clock.setFixedTime(NOW);
    let step = "load";
    page.on("console", (m) => { if (m.type() === "error") failures.push(`${mode}/${step}: console: ${m.text()}`); });
    page.on("pageerror", (e) => failures.push(`${mode}/${step}: page error: ${e.message}`));
    // Each league's season stats file is fetched at most once (loadPlayerStats shares the
    // request in flight across re-renders).
    const statsFetches = new Map();
    page.on("request", (r) => {
      const name = new URL(r.url()).pathname.split("/").pop();
      if (/^players_stats.*\.json$/.test(name)) {
        statsFetches.set(name, (statsFetches.get(name) || 0) + 1);
        if (statsFetches.get(name) > 1) failures.push(`${mode}/${step}: ${name} fetched ${statsFetches.get(name)} times`);
      }
    });
    await page.goto(BASE);
    await page.waitForSelector("button.match");
    for (const [name, run, isSheet] of STEPS) {
      step = name;
      try {
        await run(page);
        await page.waitForTimeout(150); // let async loads and the sheet animation settle
        const bad = await page.evaluate((inSheet) => {
          const out = [];
          const root = document.documentElement;
          if (root.scrollWidth > root.clientWidth + 1) out.push(`horizontal scroll (${root.scrollWidth}px > ${root.clientWidth}px)`);
          const el = inSheet ? document.querySelector("#sheet-body") : document.querySelector("#view");
          const text = el.innerText;
          for (const s of ["undefined", "NaN", "${", "[object", "Infinity"]) if (text.includes(s)) out.push(`text contains "${s}"`);
          if (inSheet) {
            const panel = document.querySelector(".sheet-panel");
            if (panel.scrollWidth > panel.clientWidth + 1) out.push("sheet scrolls sideways");
          }
          return out;
        }, !!isSheet);
        bad.forEach((b) => failures.push(`${mode}/${name}: ${b}`));
        if (shotsDir) {
          // Sheets scroll inside a fixed panel: unfold it for the screenshot, then restore.
          if (isSheet) await page.addStyleTag({ content: SHEET_SHOT_CSS });
          await page.screenshot({ path: join(shotsDir, `${mode}-${name}.png`), fullPage: true, animations: "disabled" });
          if (isSheet) await page.evaluate(() => document.querySelector("style:last-of-type")?.remove());
        }
      } catch (err) {
        failures.push(`${mode}/${name}: ${String(err.message || err).split("\n")[0]}`);
      }
    }
    await ctx.close();
  }
} finally {
  await browser.close();
  server.close();
  rmSync(site, { recursive: true, force: true });
}

if (failures.length) {
  console.error(`FAIL (${failures.length})\n${failures.map((f) => `  ${f}`).join("\n")}`);
  process.exit(1);
}
console.log(`OK: ${STEPS.length} views x ${MODES.length} modes, no console errors or layout overflow${shotsDir ? `; screenshots in ${shotsDir}` : ""}`);
