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
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
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
// (a no-op when no sheet is open, so a step can follow one that already closed it)
const closeSheet = async (p) => { if (await p.locator("#sheet:not([hidden])").count()) await p.click(".sheet-close"); };
// Portfolio folders (owner, 24 Oct): the tab opens on one folder per portfolio, then one per
// strategy. pfUp goes back to the open portfolio's strategy folders, pfHome to the top;
// pfOpenPf / pfOpenStrat open a portfolio or one of its strategies from wherever we are.
const pfUp = async (p) => { if (await p.locator('[data-pfnav="portfolio"]').count()) await p.click('[data-pfnav="portfolio"]'); };
const pfHome = async (p) => { if (await p.locator('[data-pfnav="home"]').count()) await p.click('[data-pfnav="home"]'); };
const pfOpenPf = async (p, id) => { await pfHome(p); await p.click(`button[data-pfid="${id}"]`); };
const pfOpenStrat = async (p, key) => { await pfUp(p); await p.click(`button[data-pfstrat="${key}"]`); };
// Every chart scrubs (owner, 23 Oct): for the chart `sel` (a [data-scrub] wrapper), a mouse
// hover and a finger drag each land on the stop under the pointer, with the readout line
// equal to the chart's own readout for that stop (and only that stop's marks highlighted);
// Home then the right (or down) arrow step; the page still scrolls over the chart.
// Backtest bands (owner via the Research lab, 23 Oct): for strategy `key` under competition
// `lg` ("" = All), the odds-band and claimed-edge charts and the CLV fold are drawn from the
// file's bands, each readout carries that band's own numbers, and every chart scrubs.
async function bandsCheck(p, key, lg) {
  const sfx = `${key}-${lg || "all"}`;
  const bands = await p.evaluate(([key, lg]) => {
    const bt = state.data.portfolio.strategy_backtests[key];
    const part = lg ? bt.leagues[lg] : bt.all;
    return { ob: part.by_odds_band, eb: part.by_edge_band, cb: part.by_clv_band };
  }, [key, lg]);
  const text = await p.textContent("#view");
  for (const w of ["Where the bets sit on the odds", "Claimed edge vs what the price did", "Does beating the close predict profit?"]) if (!text.includes(w)) throw new Error(`${sfx} lacks "${w}"`);
  // One row per odds band: its share, or "no bets".
  const shares = await p.locator(`[data-scrub="ob-${sfx}"] .band-row[data-stop] .band-share`).allInnerTexts();
  if (shares.length !== bands.ob.length) throw new Error(`${sfx}: ${shares.length} odds rows for ${bands.ob.length} bands`);
  const wantShares = await p.evaluate((ob) => ob.map((b) => (b.bets ? pct(b.share, 0) : "no bets")), bands.ob);
  if (shares.map((t) => t.trim()).join("|") !== wantShares.join("|")) throw new Error(`${sfx} shares ${shares} vs ${wantShares}`);
  // Each readout holds its own band's numbers.
  const bad = await p.evaluate(([sfx, b]) => {
    const out = [];
    const ob = scrubCharts[`ob-${sfx}`], ce = scrubCharts[`ce-${sfx}`];
    b.ob.forEach((x, i) => { const r = plain(ob.readout(i)); if (x.bets ? !(r.includes(`${num(x.bets)} bets`) && r.includes(`CLV ${signedPct(x.clv)}`) && r.includes(`ROI ${signedPct(x.roi)}`)) : !r.includes("no bets")) out.push(`odds ${i}: ${r}`); });
    const eb = b.eb.filter((x) => x.bets && x.claimed != null && x.clv != null);
    eb.forEach((x, i) => { const r = plain(ce.readout(i)); if (!(r.includes(`Claimed ${signedPct(x.claimed)}`) && r.includes(`CLV ${signedPct(x.clv)}`) && r.includes(`${num(x.bets)} bets`))) out.push(`edge ${i}: ${r}`); });
    if (ce.xs.length !== eb.length) out.push(`edge stops ${ce.xs.length} vs ${eb.length}`);
    return out;
  }, [sfx, bands]);
  if (bad.length) throw new Error(`${sfx} readouts: ${bad.join(" | ")}`);
  // The verdict follows the data: worse when CLV falls from the first band to the last.
  const eb = bands.eb.filter((x) => x.bets);
  const verdict = await p.locator(".verdict", { hasText: "edge it claimed" }).innerText();
  const fell = eb[eb.length - 1].clv - eb[0].clv;
  if (fell < -0.005 && !verdict.includes("worse") && !verdict.includes("barely")) throw new Error(`${sfx} verdict: ${verdict}`);
  if (fell > 0.005 && !verdict.includes("better") && !verdict.includes("barely")) throw new Error(`${sfx} verdict: ${verdict}`);
  if ((await p.locator(".band-table").count()) < 2) throw new Error(`${sfx}: band tables`);
  await scrubCheck(p, `[data-scrub="ob-${sfx}"]`);
  await scrubCheck(p, `[data-scrub="ce-${sfx}"]`);
  await p.click(".band-fold > summary");
  await scrubCheck(p, `[data-scrub="cb-${sfx}"]`);
}
// Live paper by odds band (owner, 24 Oct): strategy `key` of portfolio `pfId` under
// competition `lg`: one row per band with its share or "no trades", readouts with that band's
// own trades and CLV, the ROI table, and (Moneyline) the backtest beside it.
async function liveBandsCheck(p, pfId, key, lg) {
  if (lg === undefined) lg = await p.evaluate(() => leagueOn()); // the chip that's on
  const id = `lob-${key}-${lg || "all"}`;
  const bands = await p.evaluate(([pfId, key, lg]) => {
    const st = pfById(pfId).live.strategies.find((s) => s.key === key);
    return lg ? st.by_league[lg].by_odds_band : st.by_odds_band;
  }, [pfId, key, lg]);
  const text = await p.textContent("#view");
  for (const w of ["Where the bets sit on the odds", "Live samples are small", "where the edge question is answered"]) if (!text.includes(w)) throw new Error(`${id} lacks "${w}"`);
  const shares = (await p.locator(`[data-scrub="${id}"] .band-row[data-stop] .band-share`).allInnerTexts()).map((t) => t.trim());
  const want = await p.evaluate((b) => b.map((x) => (x.bets ? pct(x.share, 0) : "no trades")), bands);
  if (shares.join("|") !== want.join("|")) throw new Error(`${id} shares ${shares} vs ${want}`);
  const bad = await p.evaluate(([id, b]) => b.map((x, i) => [x, plain(scrubCharts[id].readout(i))]).filter(([x, r]) => (x.bets ? !r.includes(`${num(x.bets)} trades`) || (x.clv != null && !r.includes(`CLV ${signedPct(x.clv)}`)) : !r.includes("no trades"))).map(([, r]) => r), [id, bands]);
  if (bad.length) throw new Error(`${id} readouts: ${bad.join(" | ")}`);
  const beside = text.includes("Beside the backtest");
  if (beside !== (pfId === "moneyline")) throw new Error(`${id}: backtest comparison ${beside ? "shown" : "missing"}`);
  if (beside && !text.includes("In the backtest")) throw new Error(`${id}: comparison table`);
  await scrubCheck(p, `[data-scrub="${id}"]`);
}
// Kelly beside flat $10 (owner, 24 Oct): the chart `id` draws one line per sizing block from
// $1,000, its readout gives each line's bankroll that day, the tiles carry the data's numbers
// (with "would have gone broke" / "bust" where they apply), and it scrubs.
async function sizingCheck(p, id, getSizing) {
  const sz = await p.evaluate(getSizing);
  const text = (await p.textContent("#view")).replace(/\s+/g, " ");
  for (const w of ["Bankroll: flat $10 vs ¼ Kelly", "Flat $10 is the record. Kelly is shown for comparison and only helps once a strategy beats the closing price.", "How Kelly is sized here"]) if (!text.includes(w)) throw new Error(`${id} lacks "${w}"`);
  const lines = ["flat", "kelly", "kelly_raw"].filter((k) => sz[k]?.curve);
  if ((await p.locator(`[data-scrub="${id}"] path.line.ser`).count()) !== lines.length) throw new Error(`${id}: ${lines.length} lines expected`);
  const tiles = (await p.locator(`[data-scrub="${id}"]`).locator("xpath=ancestor::div[contains(@class,'bank-card')]/following-sibling::div[contains(@class,'sizing-tiles')][1]").innerText()).replace(/\s+/g, " ");
  const want = await p.evaluate((sz) => ({ k: drawdownText("kelly", sz.kelly) === "bust" ? "bust" : bankUsd(sz.kelly.bankroll_now, 2), f: bankUsd(sz.flat.bankroll_now, 2), dd: drawdownText("flat", sz.flat) }), sz);
  for (const w of [want.k, `flat $10: ${want.f}`, `flat $10: ${want.dd}`]) if (!tiles.includes(w)) throw new Error(`${id} tiles lack "${w}": ${tiles}`);
  if (sz.flat.bankroll_now <= 0 && !tiles.includes("would have gone broke (fell $")) throw new Error(`${id}: flat went broke but the tiles don't say so`);
  if (/\d{3,}\.\d%/.test(tiles)) throw new Error(`${id}: a drawdown over 100% shown as a percentage: ${tiles}`);
  // Each readout stop names every line's bankroll on that day.
  const bad = await p.evaluate(([id, sz]) => {
    const sc = scrubCharts[id];
    const keys = ["flat", "kelly", "kelly_raw"].filter((k) => sz[k]?.curve);
    const out = [];
    sc.data.forEach((d, i) => {
      if (!i) return;
      const r = plain(sc.readout(i));
      keys.forEach((k) => {
        const c = [...sz[k].curve].reverse().find((x) => x.date <= d);
        const v = c ? c.bankroll : sz[k].bankroll_start;
        if (!r.includes(bankUsd(v, 2))) out.push(`${d} ${k} ${bankUsd(v, 2)} not in "${r}"`);
      });
    });
    return out.slice(0, 3);
  }, [id, sz]);
  if (bad.length) throw new Error(`${id} readouts: ${bad.join(" | ")}`);
  await scrubCheck(p, `[data-scrub="${id}"]`);
}
async function scrubCheck(p, sel) {
  const wrap = p.locator(sel).first();
  await wrap.scrollIntoViewIfNeeded();
  const id = await wrap.getAttribute("data-scrub");
  const info = await p.evaluate((id) => { const sc = scrubCharts[id]; return { n: scrubLen(sc), rows: !!sc.rows }; }, id);
  if (info.n < 2) throw new Error(`${id}: ${info.n} stops`);
  const now = () => p.evaluate((id) => {
    const sc = scrubCharts[id];
    const sel = [...document.querySelectorAll(`[data-scrub="${id}"] [data-stop].sel`)].map((e) => Number(e.dataset.stop));
    return { at: sc.at, text: (document.getElementById(sc.readoutId)?.textContent || "").replace(/\s+/g, " ").trim(), want: sc.at == null ? "" : plain(sc.readout(sc.at)).replace(/\s+/g, " ").trim(), sel };
  }, id);
  const verify = async (what, at) => {
    const s = await now();
    if (s.at == null || (at != null && s.at !== at)) throw new Error(`${id} ${what}: at ${s.at}, wanted ${at}`);
    if (!s.text || s.text !== s.want) throw new Error(`${id} ${what}: readout "${s.text}" vs "${s.want}"`);
    if (s.sel.some((k) => k !== s.at)) throw new Error(`${id} ${what}: other stops highlighted ${s.sel}`);
    return s;
  };
  // Two targets: the first and last stops' own marks when the chart has them (rows, points),
  // else a quarter and three quarters of the way across the plot.
  const marks = wrap.locator("[data-stop]");
  const centre = (b) => ({ x: b.x + b.width / 2, y: b.y + b.height / 2 });
  let a, b, ia = null, ib = null;
  if (await marks.count()) {
    // (a tall row chart: the fifth row at most, so both stay on screen)
    ia = 0; ib = info.rows ? Math.min(info.n - 1, 4) : info.n - 1;
    a = centre(await wrap.locator('[data-stop="0"]').first().boundingBox());
    b = centre(await wrap.locator(`[data-stop="${ib}"]`).first().boundingBox());
  } else {
    const box = await wrap.locator("svg").first().boundingBox();
    a = { x: box.x + box.width * 0.25, y: box.y + box.height / 2 };
    b = { x: box.x + box.width * 0.75, y: box.y + box.height / 2 };
  }
  // Hover (a mouse, no button).
  await p.mouse.move(a.x, a.y);
  const h1 = await verify("hover", ia);
  await p.mouse.move(b.x, b.y, { steps: 4 });
  const h2 = await verify("hover across", ib);
  if (h2.at === h1.at) throw new Error(`${id}: hovering across didn't move the stop`);
  await p.mouse.move(0, 0);
  // A finger drag from the second target back to the first.
  const target = info.rows ? wrap : wrap.locator("svg").first();
  await target.dispatchEvent("pointerdown", { pointerType: "touch", pointerId: 21, clientX: b.x, clientY: b.y, bubbles: true });
  await verify("touch down", ib);
  await target.dispatchEvent("pointermove", { pointerType: "touch", pointerId: 21, clientX: a.x, clientY: a.y, bubbles: true });
  await verify("touch drag", ia ?? h1.at);
  await target.dispatchEvent("pointerup", { pointerType: "touch", pointerId: 21, bubbles: true });
  // Keys.
  await wrap.focus();
  await p.keyboard.press("Home");
  await verify("Home", 0);
  await p.keyboard.press(info.rows ? "ArrowDown" : "ArrowRight");
  await verify("arrow", 1);
  // The page still scrolls with the pointer over the chart.
  const ta = await (info.rows ? wrap : wrap.locator("svg").first()).evaluate((el) => getComputedStyle(el).touchAction);
  if (ta !== "pan-y") throw new Error(`${id}: touch-action ${ta}`);
  await p.mouse.move(a.x, a.y);
  // (a page shorter than the screen can't scroll at all, so there is nothing to check)
  if (await p.evaluate(() => document.documentElement.scrollHeight <= innerHeight + 1)) return;
  const y0 = await p.evaluate(() => window.scrollY);
  const room = await p.evaluate(() => window.scrollY + innerHeight + 50 < document.documentElement.scrollHeight);
  await p.mouse.wheel(0, room ? 200 : -200);
  await p.waitForFunction((y) => window.scrollY !== y, y0, { timeout: 3000 });
}
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
  // League One / League Two corners (24 Oct, Pinnacle only): an E0 corner card relabelled E2
  // gets the untested-model note and the no-DraftKings line, not the weaker-fit one.
  ["corners-e2-note", async (p) => {
    const text = await p.evaluate(() => {
      const fx = state.data.fixtures.find((f) => f.league === "E0" && f.corners?.pinnacle);
      const div = document.createElement("div");
      div.innerHTML = cornersSection({ ...fx, league: "E2" });
      return div.textContent;
    });
    for (const want of ["never been tested in League One", "DraftKings lists no team corners in League One"]) if (!text.includes(want)) throw new Error(`E2 corners lacks "${want}"`);
    if (text.includes("fits less well")) throw new Error("E2 corners should not carry the weaker-fit note");
  }],
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
    for (const want of ["have a Blend Lean pick", "Each Lean pick in range opens one $10 paper trade", "under Portfolio → Moneyline", "Blend Lean rule"]) {
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
    // Lean trades only at −200 to +200 since 24 Oct (owner): Leeds at +650 is outside, so
    // its pick is greyed as "no trade"; the trade the old rule opened stays shown.
    const lean = await card.locator(".badge.lean-out").innerText();
    if (!lean.includes("Lean pick outside its odds range, no trade: Away @ +650") || !lean.includes("−200 to +200 only")) throw new Error(`Lean out-of-range badge: ${lean}`);
    if (await card.locator(".badge.lean").count()) throw new Error("an out-of-range Lean pick has the pick badge");
    const cb = p.locator("button.match", { hasText: "Bournemouth" }).first();
    if (!(await cb.locator(".badge.lean-out").count()) || (await cb.locator(".badge.lean, .badge.paper").count())) throw new Error("Chelsea v Bournemouth's out-of-range pick");
    if (!(await p.textContent("#view")).includes("more picks are outside Lean's odds range, so no trade opens")) throw new Error("Matches note lacks the out-of-range count");
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
  ["match-sheet-lean-out", async (p) => {
    await p.click('button.match[data-fixture="2"]'); await sheet(p);
    const text = await p.textContent("#sheet-body");
    if (!/No Lean trade: the pick is Away at \+330, outside Lean's −200 to \+200 range since \w+ \d+\. The blend gives it [\d.]+% against the [\d.]+% this price needs/.test(text)) throw new Error(`out-of-range sheet text: ${text.replace(/\s+/g, " ").slice(0, 400)}`);
    await closeSheet(p);
  }, true],
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
  ["players-chart", async (p) => { await p.selectOption("#pl-team", ""); await p.click('button[data-plview="chart"]'); await p.waitForSelector("#dev-chart .dev-row"); await p.click("#dev-chart .dev-row >> nth=0"); await scrubCheck(p, "[data-scrub='dev']"); }],
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
  // The Record tab is gone (owner via the lead, 28 Oct): its parts live in Portfolio.
  ["no-record-tab", async (p) => {
    await closeSheet(p);
    if (await p.locator('.tabbar button[data-tab="record"]').count()) throw new Error("the Record tab is still in the tab bar");
    if ((await p.locator(".tabbar button").count()) !== 4) throw new Error("the tab bar should have four tabs");
    await tab("portfolio")(p);
  }],
  // Record → Goals & corners became two Moneyline rows: Goals markets (closed, in the
  // Archive, with the range charts) and Team totals (logging only), testing, in the main list.
  ["portfolio-goals-markets", async (p) => {
    await pfOpenPf(p, "moneyline");
    if (!(await p.locator('.section-title:has-text("Archive") + .card button[data-pfstrat="goals_markets"]').count())) throw new Error("Goals markets isn't in the Archive");
    const row = (await p.locator('button[data-pfstrat="goals_markets"]').innerText()).replace(/\s+/g, " ");
    for (const w of ["Goals markets", "Closed", "No edge", "never traded", "Total goals lose to Pinnacle's price"]) if (!row.includes(w)) throw new Error(`Goals markets row lacks "${w}": ${row}`);
    await p.click('button[data-pfstrat="goals_markets"]');
    const text = await p.textContent("#view");
    for (const w of ["Total goals", "Each team's goals", "No edge", "Team totals (logging only) row", "Corners' research is under Team corners"]) if (!text.includes(w)) throw new Error(`Goals markets page lacks "${w}"`);
    if (text.includes("5 of 50")) throw new Error("the team-total progress belongs on its own row");
    await scrubCheck(p, "[data-scrub='tg']");
  }],
  ["portfolio-goals-markets-details", async (p) => {
    for (const d of await p.locator("details.mk-fold > summary").all()) await d.click();
    if (!(await p.textContent("#view")).includes("Lines better")) throw new Error("the league tables don't open");
  }],
  ["portfolio-team-totals", async (p) => {
    await pfUp(p);
    const main = await p.locator('.card.folders').first().locator('button[data-pfstrat="team_totals_log"]').count();
    if (!main) throw new Error("Team totals isn't in the main list");
    const row = (await p.locator('button[data-pfstrat="team_totals_log"]').innerText()).replace(/\s+/g, " ");
    for (const w of ["Team totals (logging only)", "Testing", "5 of 50", "places no bets"]) if (!row.includes(w)) throw new Error(`Team totals row lacks "${w}": ${row}`);
    await p.click('button[data-pfstrat="team_totals_log"]');
    const text = await p.textContent("#view");
    for (const w of ["5 of 50", "matches ready to score", "Live test against FanDuel", "nothing is bet"]) if (!text.includes(w)) throw new Error(`Team totals page lacks "${w}"`);
  }],
  // The publish field missing (a broken or old build): no goals rows, nothing else breaks.
  ["portfolio-markets-missing", async (p) => {
    await p.route("**/data.json", async (route) => {
      const res = await route.fetch();
      const d = await res.json();
      delete d.markets_research;
      await route.fulfill({ response: res, json: d });
    });
    await p.reload(); await p.waitForSelector("button.match");
    await tab("portfolio")(p); await pfOpenPf(p, "moneyline");
    if (await p.locator('button[data-pfstrat="goals_markets"], button[data-pfstrat="team_totals_log"]').count()) throw new Error("goals rows without markets_research");
  }],
  ["portfolio-markets-restored", async (p) => {
    await p.unroute("**/data.json");
    await p.reload(); await p.waitForSelector("button.match");
    await tab("portfolio")(p); await pfOpenPf(p, "moneyline");
  }],
  // Record → Match bets: the Premier League model replay is in Moneyline's Model view,
  // E0_dk.json's threshold sweep folded under Model 12%'s Premier League backtest.
  ["portfolio-model-replay", async (p) => {
    await pfOpenStrat(p, "lean"); await p.click('button[data-pf="model"]'); await p.click('.chip[data-league="E0"]');
    const text = await p.textContent("#view");
    for (const w of ["The model replayed, Premier League", "Accuracy vs price", "Goals + xG", "Goals only", "Running profit (units)"]) if (!text.includes(w)) throw new Error(`model replay lacks "${w}"`);
    await p.locator("details.mk-fold > summary", { hasText: "Running profit (units)" }).click();
    await scrubCheck(p, "[data-scrub='pc']");
  }],
  ["portfolio-model-replay-sp1", async (p) => {
    await p.click('.chip[data-league="SP1"]');
    const text = await p.textContent("#view");
    if (text.includes("The model replayed, Premier League") || !text.includes("covers the Premier League only")) throw new Error("the replay should be a one-line note outside the Premier League");
    await p.click('.chip[data-league=""]');
    if (!(await p.textContent("#view")).includes("The model replayed, Premier League")) throw new Error("no replay under All");
  }],
  ["portfolio-edge12-e0-sweep", async (p) => {
    await pfOpenStrat(p, "edge12"); await p.click('button[data-pf="backtest"]'); await p.click('.chip[data-league="E0"]');
    await p.locator("details.dk-sweep > summary").click();
    const text = await p.textContent("details.dk-sweep");
    for (const w of ["Model alone", "Blend", "Odds capped at", "Log loss", "Most accurate"]) if (!text.includes(w)) throw new Error(`DraftKings sweep lacks "${w}"`);
    if ((await p.textContent("#view")).includes("Record →")) throw new Error("a note still points at Record");
    await p.click('.chip[data-league=""]'); await p.click('button[data-pf="live"]');
  }],
  ["portfolio-live", async (p) => {
    await closeSheet(p); await tab("portfolio")(p);
    // The top level: one folder per portfolio, Anytime goalscorer hidden (owner, 24 Oct).
    const folders = await p.locator("#view button[data-pfid]:not(.linkish)").evaluateAll((els) => els.map((e) => e.dataset.pfid));
    if (folders.join(",") !== "moneyline,corners,comebacks") throw new Error(`portfolio folders: ${folders.join(",")}`);
    if (!(await p.locator('#view button.linkish[data-pfid="player_shots"]').count())) throw new Error("retired link missing");
    await p.click('button[data-pfid="moneyline"]');
    // Moneyline's folders: one per strategy, the old rule filed under Lean.
    const strats = await p.locator("#view button[data-pfstrat]").evaluateAll((els) => els.map((e) => e.dataset.pfstrat));
    // Each strategy is its own row (owner, 25 Oct); the old rule sits in the Archive list.
    // Each draw no bet track sits right after its parent in the main list (owner, 26 Oct).
    // Model 12% and its draw no bet track are archived (owner, 26 Oct), after the old Lean rule.
    // A failed pre-match study (the strategy search) joins the Archive last (owner, 28 Oct).
    if (strats.join(",") !== "lean,lean_dnb,team_totals_log,lean_all_odds,edge12,edge12_dnb,strategy_search,goals_markets") throw new Error(`strategy folders: ${strats.join(",")}`);
    const archive = await p.locator(".section-title", { hasText: "Archive" }).count();
    if (!archive || !(await p.locator('.section-title:has-text("Archive") + .card button[data-pfstrat="lean_all_odds"]').count())) throw new Error("the old rule isn't in the Archive list");
    // The strategy folders: Lean's row carries its own settled record and its backtest.
    const leanRow = await p.locator('button[data-pfstrat="lean"]').innerText();
    if (!/1σ\+/.test(leanRow) || !/2–1/.test(leanRow) || !leanRow.includes("Backtest:")) throw new Error(`Lean row: ${leanRow}`);
    await p.click('button[data-pfstrat="lean"]');
    const on = await p.locator(".pf-title").innerText();
    if (!on.includes("Blend Lean")) throw new Error(`opened strategy: ${on}`);
    if ((await p.locator(".crumbs").innerText()).replace(/\s+/g, " ") !== "‹ Portfolio › Moneyline") throw new Error("crumbs");
    const text = await p.textContent("#view");
    for (const want of ["Profit, settled", "Record", "Avg edge", "Profit over time", "Open · ", "Settled · ", "Blend Lean (1σ+). A $10", "baseball app's Lean tier", "not from the model", "Wolves v Everton", "until a few hundred, results are mostly luck"]) {
      if (!text.includes(want)) throw new Error(`Lean live lacks "${want}"`);
    }
    if (text.includes("Nott'm Forest v Arsenal")) throw new Error("a 12% trade shows under Blend Lean");
    // Every trade row under Lean is a Lean trade (its id ends "|lean").
    const ids = await p.locator("#view button[data-trade]").evaluateAll((els) => els.map((e) => e.dataset.trade));
    if (!ids.length || ids.some((id) => !id.endsWith("|lean"))) throw new Error(`non-Lean rows under Lean: ${ids.join(", ")}`);
    // Profit over time: before any pointer, the readout is the latest settled day.
    const readout = await p.textContent("#cv-readout");
    // (Lean has its old-rule variant since 24 Oct, so its chart is the overlay.)
    const latest = await p.evaluate(() => plain(scrubCharts.cv.readout(scrubCharts.cv.xs.length - 1)));
    if (readout !== latest || !readout.includes("Live rule")) throw new Error(`curve readout: ${readout}`);
    const avg = await p.locator(".tile", { hasText: "Avg edge" }).locator(".value").innerText();
    if (!avg.endsWith("σ")) throw new Error(`Lean avg edge should be in σ: ${avg}`);
  }],
  // The profit chart scrubs (owner, 23 Oct): point (laptop), press and drag, drag a finger
  // (phone) or use the arrow keys; the readout shows that day's date, total and trades.
  ["portfolio-live-scrub", async (p) => {
    const svg = p.locator("[data-scrub='cv'] svg");
    await svg.scrollIntoViewIfNeeded();
    const box = await svg.boundingBox();
    const y = box.y + box.height / 2;
    const expected = () => p.evaluate(() => ({ at: scrubCharts.cv.at, text: plain(scrubCharts.cv.readout(scrubCharts.cv.at)), origin: scrubCharts.cv.at === 0, n: scrubCharts.cv.xs.length }));
    const check = async (what) => {
      const e = await expected();
      const r = await p.textContent("#cv-readout");
      if (r !== e.text || !r.includes(e.origin ? "first trade opened" : "Live rule")) throw new Error(`${what}: readout "${r}" vs day ${e.at} "${e.text}"`);
      if (await p.locator("[data-scrub='cv'] .cursor").evaluate((el) => el.style.display) === "none") throw new Error(`${what}: no cursor`);
      return e;
    };
    // Pointing (no button) left of the first stop snaps to the $0 origin, dated by the
    // first Lean trade opened (owner, 23 Oct).
    await p.mouse.move(box.x + box.width * 0.5, y);
    await p.mouse.move(box.x + 2, y, { steps: 4 });
    const a = await check("hover left");
    if (a.at !== 0 || !a.origin) throw new Error(`hover left should be the origin, got ${a.at}`);
    const first = await p.evaluate(() => dayName(firstOpened(stratSet(pfById("moneyline").live, pfById("moneyline").live.strategies.find((x) => x.key === "lean")).trades)));
    if (!(await p.textContent("#cv-readout")).startsWith(`${first} · first trade opened`)) throw new Error(`origin readout should start "${first}"`);
    // Press and drag to the right edge: the latest day; release keeps it.
    await p.mouse.down();
    await p.mouse.move(box.x + box.width - 2, y, { steps: 8 });
    await p.mouse.up();
    const b = await check("drag right");
    if (b.at !== b.n - 1) throw new Error(`drag right should reach the last day, got ${b.at}`);
    // A finger drag (touch pointer events) from the right back to the middle.
    const sel = "[data-scrub='cv'] svg";
    await p.locator(sel).dispatchEvent("pointerdown", { pointerType: "touch", pointerId: 7, clientX: box.x + box.width - 2, clientY: y, bubbles: true });
    await p.locator(sel).dispatchEvent("pointermove", { pointerType: "touch", pointerId: 7, clientX: box.x + box.width * 0.5, clientY: y, bubbles: true });
    const c = await check("touch drag");
    await p.locator(sel).dispatchEvent("pointerup", { pointerType: "touch", pointerId: 7, bubbles: true });
    if (c.at === b.at && c.n > 2) throw new Error("touch drag didn't move the cursor");
    // A finger that isn't pressed doesn't scrub.
    await p.locator(sel).dispatchEvent("pointermove", { pointerType: "touch", pointerId: 8, clientX: box.x + 2, clientY: y, bubbles: true });
    if ((await expected()).at !== c.at) throw new Error("an unpressed finger moved the cursor");
    // Keyboard: focus, then the left arrow steps one day back.
    await p.locator("[data-scrub='cv']").focus();
    await p.keyboard.press("ArrowLeft");
    const d = await check("arrow left");
    if (d.at !== Math.max(0, c.at - 1)) throw new Error(`arrow left: ${c.at} → ${d.at}`);
    // Vertical page scroll isn't blocked over the chart.
    if ((await svg.evaluate((el) => getComputedStyle(el).touchAction)) !== "pan-y") throw new Error("the chart must keep touch-action: pan-y");
    const before = await p.evaluate(() => window.scrollY);
    await p.mouse.move(box.x + box.width / 2, y);
    await p.mouse.wheel(0, 400);
    await p.waitForFunction((y0) => window.scrollY !== y0, before, { timeout: 3000 });
  }],
  ["portfolio-live-bands", async (p) => {
    await liveBandsCheck(p, "moneyline", "lean", "");
    await sizingCheck(p, "kz-lean", () => pfById("moneyline").live.strategies.find((s) => s.key === "lean").sizing);
  }],
  // Blend Lean's odds range (owner, 24 Oct): the rule on Lean's row and rule box; the old
  // rule (all odds) as an indented variant that adds the shadow entries, with the overlay.
  ["portfolio-lean-old-rule", async (p) => {
    await pfUp(p);
    const row = (await p.locator('button[data-pfstrat="lean"]').innerText()).replace(/\s+/g, " ");
    if (!row.includes("1σ+ · −200 to +200")) throw new Error(`Lean row: ${row}`);
    const vrow = (await p.locator('button[data-pfstrat="lean_all_odds"]').innerText()).replace(/\s+/g, " ");
    if (!vrow.includes("Blend Lean, all odds (old rule)") || !vrow.includes("Old rule, until") || vrow.includes("↳")) throw new Error(`old-rule row: ${vrow}`);
    // The new rule's backtest is the replay's bands inside its range, pooled; the archived
    // old rule keeps the whole replay (every price). Both read DraftKings' replay when there
    // is one (lead brief, 28 Oct), else Pinnacle's.
    const btWant = await p.evaluate(() => {
      const sb = state.data.portfolio.strategy_backtests;
      const part = (sb.lean_dk?.available ? sb.lean_dk : sb.lean).all;
      const b = part.by_odds_band.filter((x) => x.lo === 1.5 || x.lo === 2);
      const bets = b.reduce((a, x) => a + x.bets, 0);
      return { bets, roi: signedPct(b.reduce((a, x) => a + x.roi * x.bets, 0) / bets), all: signedPct(part.lean.roi) };
    });
    const bt = (await p.locator('button[data-pfstrat="lean"] .bt').innerText()).replace(/\s+/g, " ");
    if (!bt.includes(btWant.roi) || !bt.includes(`${btWant.bets.toLocaleString()} bets, −200 to +200 · at DraftKings`)) throw new Error(`Lean in-range backtest: ${bt} vs ${JSON.stringify(btWant)}`);
    if (!vrow.includes(`Backtest: ${btWant.all}`)) throw new Error(`old-rule backtest: ${vrow}`);
    await pfOpenStrat(p, "lean");
    if (!(await p.textContent("#view")).includes("Odds range since")) throw new Error("Lean rule box lacks the odds range");
    // The new rule's line is backcast from Lean's first trade: both lines start there, the
    // rule date is marked, and Lean's view says the early part is a backcast.
    const bc = await p.evaluate(() => pfById("moneyline").live.strategies.find((s) => s.key === "lean").backcast);
    if (!(await p.textContent("#view")).includes(`Backcast before`)) throw new Error("Lean view lacks the backcast note");
    if ((await p.locator("[data-scrub='cv'] .filter-lab").textContent()) !== "rule set (judged from here)") throw new Error("rule marker");
    const origin = await p.evaluate(() => plain(scrubCharts.cv.readout(0)));
    const want0 = await p.evaluate((d) => dayName(String(d).slice(0, 10)), bc.lean_started);
    if (!origin.startsWith(`${want0} · first trade opened`)) throw new Error(`overlay origin ${origin} vs lean_started ${want0}`);
    if ((await p.locator(".pf-curve .curve-legend .lg-item").allInnerTexts()).join("|").indexOf("Live rule") < 0) throw new Error("overlay legend lacks the live rule");
    await pfOpenStrat(p, "lean_all_odds");
    const ids = await p.locator("#view button[data-trade]").evaluateAll((els) => els.map((e) => e.dataset.trade));
    const want = await p.evaluate(() => pfById("moneyline").live.strategies.find((s) => s.key === "lean_all_odds").trade_ids);
    if (!ids.some((id) => id.endsWith("|shadow")) || ids.some((id) => !want.includes(id))) throw new Error(`old-rule trades: ${ids.join(", ")}`);
    const chip = await p.locator('#view button[data-trade$="|shadow"] .chip-shadow').first().innerText();
    if (chip !== "Shadow: no trade opened (outside the odds range)") throw new Error(`shadow chip: ${chip}`);
    const box = (await p.textContent(".card.judged")).replace(/\s+/g, " ");
    for (const w of ["Before the rule,", "backcast", "Live rule (backcast)", "Cut (outside the range)", "Descriptive only:", "Judged from", "Live rule", "All odds (old rule)", "odds range was picked after seeing the first", "the old code opened in that window", "How this comparison works", "minus all odds (old rule)"]) {
      if (!box.includes(w)) throw new Error(`old-rule box lacks "${w}": ${box}`);
    }
    if (box.includes("60%")) throw new Error("old-rule box talks about the 60% cut");
    const text = await p.textContent("#view");
    if (!text.includes("Blend Lean, all odds (old rule).") || !text.includes("Odds range since")) throw new Error("old-rule rule note");
    await scrubCheck(p, "[data-scrub='cv']");
    await p.locator(".band-share").first().scrollIntoViewIfNeeded();
    await pfOpenStrat(p, "lean");
  }],
  // Draw no bet tracks (owner, 26 Oct; paper_dnb): their own rows after each parent, their
  // own trades from `dnb_trades`, a draw shown as "Draw: stake back", ROI on bets not
  // refunded, the descriptive comparison with the parent, the parent overlaid on the chart.
  ["portfolio-dnb", async (p) => {
    await pfUp(p);
    const row = (await p.locator('button[data-pfstrat="edge12_dnb"]').innerText()).replace(/\s+/g, " ");
    for (const w of ["Model 12%, draw no bet", "Archived Oct 26 · still logs every 12%-edge pick, draw no bet", "Backtest, 24 Oct: no edge;"]) if (!row.includes(w)) throw new Error(`DNB row lacks "${w}": ${row}`);
    const leanRow = (await p.locator('button[data-pfstrat="lean_dnb"]').innerText()).replace(/\s+/g, " ");
    if (!leanRow.includes("Tracked beside the record · places no bets of its own · a draw returns the stake")) throw new Error(`Lean DNB row: ${leanRow}`);
    // Model 12% and its track sit in the Archive (owner, 26 Oct); Lean's track stays in the main list.
    for (const k of ["edge12", "edge12_dnb"]) if (!(await p.locator(`.section-title:has-text("Archive") + .card button[data-pfstrat="${k}"]`).count())) throw new Error(`${k} isn't in the Archive`);
    if (await p.locator('.section-title:has-text("Archive") + .card button[data-pfstrat="lean_dnb"]').count()) throw new Error("Lean's DNB track is in the Archive");
    const e12 = (await p.locator('button[data-pfstrat="edge12"]').innerText()).replace(/\s+/g, " ");
    if (!e12.includes("Archived Oct 26 · still logs every 12%-edge pick") || !e12.includes("Backtest:")) throw new Error(`Model 12% row: ${e12}`);
    const arch = (await p.locator('.section-title:has-text("Archive") + .card + .note').innerText()).replace(/\s+/g, " ");
    if (!arch.includes("Old rules are still tracked") || !arch.includes("Kept out of the main list since Oct 26: Model 12% still opens a paper trade")) throw new Error(`archive note: ${arch}`);
    await pfHome(p);
    const home = (await p.locator('button[data-pfid="moneyline"]').innerText()).replace(/\s+/g, " ");
    if (!home.includes("Blend Lean (new rule) · 1 draw no bet track · 5 archived") || home.includes("Model 12%")) throw new Error(`Moneyline row: ${home}`);
    // The headline counts the active strategies only: Lean's new-rule trades.
    const wantHead = await p.evaluate(() => { const cur = pfById("moneyline"); const m = tradeStats(strategyLiveSet(cur, pfStrategies(cur).find((x) => x.key === "lean"), "").trades); return usd(m.profit, 2); });
    if (!home.includes(wantHead)) throw new Error(`Moneyline headline ${home} vs Lean ${wantHead}`);
    await p.click('button[data-pfid="moneyline"]');
    await p.click('button[data-pfstrat="edge12_dnb"]');
    if ((await p.locator(".pf-title").innerText()) !== "Model 12%, draw no bet") throw new Error("DNB track not opened");
    const ids = await p.locator("#view button[data-trade]").evaluateAll((els) => els.map((e) => e.dataset.trade));
    const want = await p.evaluate(() => pfById("moneyline").live.strategies.find((s) => s.key === "edge12_dnb").trade_ids);
    if (ids.length !== want.length || ids.some((id) => !id.endsWith("|dnb") || !want.includes(id))) throw new Error(`DNB trades: ${ids.join(", ")}`);
    const draw = (await p.locator('button[data-trade="E0|2627|Leeds|Crystal Palace|dnb"]').innerText()).replace(/\s+/g, " ");
    if (!draw.includes("Crystal Palace, draw no bet") || !draw.includes("Draw stake back") || /Lost|−\$10/.test(draw)) throw new Error(`draw row: ${draw}`);
    const text = (await p.textContent("#view")).replace(/\s+/g, " ");
    const m = await p.evaluate(() => tradeStats(strategyLiveSet(pfCurrent(), pfSelected(pfStrategies(pfCurrent())), "").trades));
    if (m.won !== 2 || m.lost !== 1 || m.draws !== 1 || m.open !== 1) throw new Error(`DNB stats: ${JSON.stringify(m)}`);
    for (const w of ["2–1", "1 draw, stake back", "per stake not refunded", "(not refunded)", "ROI counts only the bets that weren't refunded", `Compared with Model 12% (your live test), since`, "descriptive", "CLV per $", "1X2 (the record)", "+1 back", "Descriptive only: the two bet different sides and at different prices", "How this comparison works", "Model 12%, draw no bet. Model 12%'s rule on draw no bet", "side x (1 - 1/draw)", "may carry a bigger margin", "The parent's rule, Model 12% (your live test):", "Draw = stake back"]) {
      if (!text.includes(w)) throw new Error(`DNB view lacks "${w}"`);
    }
    // The track leads with its own note, ahead of the tiles; the parent's rule comes last (lead, 26 Oct).
    const first = (await p.locator("#view .explain").first().innerText()).replace(/\s+/g, " ");
    if (!first.startsWith("Model 12%, draw no bet. Model 12%'s rule on draw no bet")) throw new Error(`first box: ${first}`);
    if (text.indexOf("Model 12%'s rule on draw no bet") > text.indexOf("Profit, settled")) throw new Error("the DNB note comes after the tiles");
    if (text.indexOf("Owner's live test") >= 0 && text.indexOf("Owner's live test") < text.indexOf("Profit, settled")) throw new Error("the parent's note leads the DNB page");
    // The comparison's CLV per $ is paper_dnb's, for both lines.
    const per = await p.evaluate(() => { const c = pfById("moneyline").live.strategies.find((s) => s.key === "edge12_dnb").compare.clv_per_dollar.clv_dk; return [signedPct(c.track.mean), signedPct(c.parent.mean)]; });
    const box = (await p.textContent(".dnb-compare")).replace(/\s+/g, " ");
    if (!per.every((v) => box.includes(v))) throw new Error(`compare box lacks ${per}: ${box}`);
    // Profit over time: the parent solid (since the track started) and draw no bet dashed.
    const legend = (await p.locator(".pf-curve .curve-legend .lg-item").allInnerTexts()).join("|");
    if (!legend.startsWith("1X2 (the record)") || !legend.includes("Draw no bet")) throw new Error(`DNB legend: ${legend}`);
    if (await p.locator("[data-scrub='cv'] .filter-lab").count()) throw new Error("DNB chart has a filter marker");
    const origin = await p.evaluate(() => plain(scrubCharts.cv.readout(0)));
    const start = await p.evaluate(() => dayName(String(pfById("moneyline").live.strategies.find((s) => s.key === "edge12_dnb").defined_at).slice(0, 10)));
    if (!origin.startsWith(start)) throw new Error(`DNB overlay origin ${origin} vs ${start}`);
    await scrubCheck(p, "[data-scrub='cv']");
    // The parent's own chart leaves the draw no bet line out.
    await pfOpenStrat(p, "edge12");
    if ((await p.locator(".pf-curve .curve-legend").count())) throw new Error("Model 12% chart overlays its DNB track");
    await pfOpenStrat(p, "edge12_dnb");
  }],
  ["portfolio-dnb-trade", async (p) => {
    await p.click('button[data-trade="E0|2627|Leeds|Crystal Palace|dnb"]'); await sheet(p);
    const text = (await p.textContent("#sheet-body")).replace(/\s+/g, " ");
    for (const w of ["A draw: stake back.", "Away, draw no bet", "Model 12%, draw no bet (tracked beside the record)", "Built from DraftKings' 1X2", "side × (1 − 1/draw)", "Model chance, draw taken out", "Draw no bet: fixed edge, model alone", "CLV vs DraftKings (per stake not refunded)", "CLV per $ vs DraftKings", "CLV per $ vs Pinnacle", "Draw: stake back"]) {
      if (!text.includes(w)) throw new Error(`DNB trade sheet lacks "${w}"`);
    }
    if (text.includes("Without team news")) throw new Error("DNB sheet shows the team-news row");
  }, true],
  ["portfolio-dnb-backtest", async (p) => {
    await closeSheet(p); await p.click('button[data-pf="backtest"]');
    const text = (await p.textContent("#view")).replace(/\s+/g, " ");
    if (!text.includes("The draw no bet track has no backtest of its own here: this is Model 12% (your live test)'s, on 1X2. Backtest, 24 Oct: no edge;")) throw new Error("DNB backtest note");
    await p.click('button[data-pf="model"]');
    if (!(await p.textContent("#view")).includes("The draw no bet track has no model of its own here")) throw new Error("DNB model note");
    await p.click('button[data-pf="live"]');
  }],
  // Lean's track has no trades until the blend fits carry sigma_dnb (the overview's source "none").
  ["portfolio-dnb-lean-empty", async (p) => {
    await pfOpenStrat(p, "lean_dnb");
    const text = (await p.textContent("#view")).replace(/\s+/g, " ");
    for (const w of ["No Blend Lean, draw no bet trades yet. The blend's fits don't carry a σ for draw no bet yet", "Blend Lean, draw no bet. Blend Lean's rule on draw no bet", "applies to the DNB price", "The parent's rule, Blend Lean (new rule):"]) {
      if (!text.includes(w)) throw new Error(`Lean DNB view lacks "${w}"`);
    }
    await pfOpenStrat(p, "lean");
  }],
  // Arsenal v Leeds's Strong Lean trade (+650) is outside the odds range, so it sits in the
  // old rule's line, not the live rule's (owner, 24 Oct).
  ["portfolio-live-lean-trade", async (p) => {
    await pfOpenStrat(p, "lean_all_odds");
    if (!(await p.locator(".pf-title").innerText()).includes("all odds (old rule)")) throw new Error("old rule not opened");
    await p.click('button[data-trade="E0|2627|Arsenal|Leeds|lean"]'); await sheet(p);
    const kl = await p.textContent("#sheet-body .kelly-line");
    if (!/^(¼ Kelly stake: \$[\d,.]+|Kelly: no stake \(no edge on the calibrated chance\))\. Shown for comparison only/.test(kl)) throw new Error(`Kelly line: ${kl}`);
    const text = await p.textContent("#sheet-body");
    for (const want of ["Blend Lean (1σ+)", "Chance used (blend)", "Above break-even", "+2.6σ (Strong", "+1σ"]) if (!text.includes(want)) throw new Error(`Lean trade sheet lacks "${want}"`);
  }, true],
  // Trade states (owner, 18 Oct): kicked off under ~2 h 15 min ago = "In play"; a finished
  // match still open waits in its own "Awaiting result" group, between Open and Settled.
  ["portfolio-live-states", async (p) => {
    await closeSheet(p); await pfOpenStrat(p, "lean");
    const text = await p.textContent("#view");
    for (const want of ["Awaiting result · 1", "These matches have finished. Results usually come in within a few hours of full time, and each trade then settles automatically.", "Their close and CLV are final", "finished, awaiting result (not in profit yet)"]) {
      if (!text.includes(want)) throw new Error(`Lean live lacks "${want}"`);
    }
    // Open = upcoming + in play only (Aston Villa v Brentford in play; Arsenal v Leeds, upcoming,
    // is out of range and so in the old rule's line only).
    const openHead = await p.locator(".section-title", { hasText: "Open · " }).innerText();
    if (!/Open · 1 · \$10 at risk/i.test(openHead)) throw new Error(`open header: ${openHead}`);
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
  // The backtest at DraftKings' own prices (lead brief, 28 Oct; strategy_backtests.lean_dk,
  // made up in the fixture until Moneyline's file lands): first when present, a switch to
  // Pinnacle's replay, a coverage line for each and the same rule at both books. It ends on
  // Pinnacle's, which the next steps check in full.
  ["portfolio-lean-backtest-dk", async (p) => {
    await p.click('.chip[data-league=""]'); await p.click('button[data-pfdriver=""]'); await p.click('button[data-pf="backtest"]');
    const on = await p.locator(".bt-src button.on").innerText();
    if (on !== "At DraftKings' prices") throw new Error(`backtest source on: ${on}`);
    const text = (await p.textContent("#view")).replace(/\s+/g, " ");
    for (const w of ["At DraftKings' prices: 2020/21 to 2024/25, 1 league.", "At Pinnacle's prices: 2019/20", "The same rule at both books", "In the replay at DraftKings' prices Lean", "Lean replayed on DraftKings' historical prices"]) {
      if (!text.includes(w)) throw new Error(`DraftKings backtest lacks "${w}"`);
    }
    // The comparison: the same rule's ROI at both books, from each file.
    const want = await p.evaluate(() => { const sb = state.data.portfolio.strategy_backtests; return [signedPct(sb.lean_dk.all.lean.roi), signedPct(sb.lean.all.lean.roi)]; });
    const table = (await p.locator(".drv-table").first().innerText()).replace(/\s+/g, " ").trim();
    if (!table.startsWith("DraftKings Pinnacle") || !want.every((w) => table.includes(w))) throw new Error(`both-books table: ${table} vs ${want}`);
    // La Liga is in Pinnacle's replay only: no switch, and the note says so.
    await p.click('.chip[data-league="SP1"]');
    if (await p.locator(".bt-src").count()) throw new Error("a switch for a league only one book covers");
    if (!(await p.textContent("#view")).includes("La Liga is only in the replay at Pinnacle's prices.")) throw new Error("one-book note");
    await p.click('.chip[data-league=""]');
    await p.click('button[data-btsrc="pin"]');
    if ((await p.locator(".bt-src button.on").innerText()) !== "At Pinnacle's prices") throw new Error("switch to Pinnacle");
  }],
  // Blend Lean's backtest: the replay at Pinnacle (data-log's real lean_backtest.json).
  ["portfolio-lean-backtest", async (p) => {
    await p.click('.chip[data-league=""]'); await p.click('button[data-pfdriver=""]'); await p.click('button[data-pf="backtest"]');
    const text = await p.textContent("#view");
    for (const want of ["This replay is the old rule, at every price. Inside the −200 to +200 range it placed", "In the replay at Pinnacle's prices Lean", "Return by league", "Lean and Strong", "Market-driven and model-driven", "Bets, newest first", "units"]) {
      if (!text.includes(want)) throw new Error(`Lean backtest lacks "${want}"`);
    }
    await p.click('.rc-row[data-rc="lbroi"][data-rcrow="0"]');
    if (!(await p.textContent("#rc-readout-lbroi")).includes("bets")) throw new Error("ROI readout");
    await scrubCheck(p, "[data-scrub='lbroi']");
    await bandsCheck(p, "lean", "");
    await sizingCheck(p, "bz-lean-all", () => state.data.portfolio.strategy_backtests.lean.all.sizing);
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
  // The strategy rows' backtest headlines follow the competition chip (owner, 21 Oct), and
  // both strategies have a per-league backtest (Model 12%'s edge12_backtest.json).
  ["portfolio-backtest-headlines", async (p) => {
    await pfUp(p);
    const row = async (k) => (await p.locator(`button[data-pfstrat="${k}"] .bt`).innerText()).replace(/\s+/g, " ");
    // DraftKings' replay (All and the Premier League in the fixture) heads the row where it
    // covers the competition, Pinnacle's elsewhere (lead brief, 28 Oct).
    const want = {
      "": ["bets, −200 to +200 · at DraftKings", "bets · at DraftKings"],
      E0: ["Premier League at DraftKings", "Premier League at DraftKings"],
      SP1: ["La Liga at Pinnacle", "La Liga at Pinnacle"],
      E2: ["League One at Pinnacle", "League One at Pinnacle"],
      E3: ["League Two at Pinnacle", "League Two at Pinnacle"],
      UCL: ["0 bets, −200 to +200 · Champions League at DraftKings' close", "Champions League at DraftKings' close"],
    };
    const seen = {};
    for (const [lg, [lean, e12]] of Object.entries(want)) {
      await p.click(`.chip[data-league="${lg}"]`);
      seen[lg] = await row("lean");
      if (!seen[lg].includes(lean)) throw new Error(`Lean row under "${lg}": ${seen[lg]}`);
      const r = await row("edge12");
      if (!r.includes(e12)) throw new Error(`12% row under "${lg}": ${r}`);
    }
    if (new Set(["", "E0", "SP1"].map((k) => seen[k])).size !== 3) throw new Error(`Lean headline doesn't follow the chip: ${JSON.stringify(seen)}`);
  }],
  // The Champions League: Lean has no bets (the file's note says why), coverage by book.
  ["portfolio-backtest-ucl-lean", async (p) => {
    await pfOpenStrat(p, "lean");
    const text = await p.textContent("#view");
    if (!text.includes("300") || (await p.locator("#view .tiles").count())) throw new Error("UCL Lean should show the file's note, not tiles");
    if (text.includes("Where the bets sit on the odds") || (await p.locator("#view .band-row").count())) throw new Error("UCL Lean has no bets: no band charts");
    if (text.includes("Bankroll: flat $10 vs ¼ Kelly")) throw new Error("UCL Lean has no bets: no Kelly chart");
    await p.click("details.mk-fold:has(summary:text('Seasons and fits')) > summary");
    const cov = await p.locator("details.mk-fold[open] table").innerText();
    for (const w of ["DK", "PIN", "Bet on"]) if (!cov.includes(w)) throw new Error(`UCL coverage lacks ${w}`);
  }],
  ["portfolio-backtest-ucl-edge12", async (p) => {
    await pfOpenStrat(p, "edge12");
    const text = await p.textContent("#view");
    if (!text.includes("mostly measures the gap between the two books") || !(await p.locator("#view .tiles").count())) throw new Error("UCL Model 12% backtest");
    if (!text.includes("Pinnacle's close at the same moment")) throw new Error("UCL bands' close note");
    await bandsCheck(p, "edge12", "UCL");
    await sizingCheck(p, "bz-edge12-UCL", () => state.data.portfolio.strategy_backtests.edge12.leagues.UCL.sizing);
    await p.click("details.mk-fold:has(summary:text('Seasons and fits')) > summary");
  }],
  ["portfolio-backtest-e2-edge12", async (p) => {
    await p.click('.chip[data-league="E2"]');
    const text = await p.textContent("#view");
    if (!text.includes("League One") || !(await p.locator("#view .bet-row").count())) throw new Error("League One Model 12% backtest");
  }],
  ["portfolio-backtest-e3-lean", async (p) => {
    await p.click('.chip[data-league="E3"]'); await pfOpenStrat(p, "lean");
    if (!(await p.locator("#view .tiles").count())) throw new Error("League Two Lean backtest tiles");
    await bandsCheck(p, "lean", "E3");
    await sizingCheck(p, "bz-lean-E3", () => state.data.portfolio.strategy_backtests.lean.leagues.E3.sizing);
  }],
  ["portfolio-backtest-e3-edge12", async (p) => { await pfOpenStrat(p, "edge12"); await bandsCheck(p, "edge12", "E3"); }],
  // Model 12% under All: the per-league replay with its range plots and readout.
  ["portfolio-backtest-edge12-all", async (p) => {
    await p.click('.chip[data-league=""]'); await pfOpenStrat(p, "edge12");
    const text = await p.textContent("#view");
    for (const w of ["In the replay at Pinnacle's prices Model 12%", "Return by league", "Bets, newest first", "Champions League is priced at DraftKings' close"]) {
      if (!text.includes(w)) throw new Error(`12% All backtest lacks "${w}"`);
    }
    await p.click('.rc-row[data-rc="edge12roi"][data-rcrow="0"]');
    if (!(await p.textContent("#rc-readout-edge12roi")).includes("bets")) throw new Error("12% ROI readout");
    await bandsCheck(p, "edge12", "");
    // Pooled Model 12%: flat $10 falls far below $0 and Kelly on the raw model is bust.
    await sizingCheck(p, "bz-edge12-all", () => state.data.portfolio.strategy_backtests.edge12.all.sizing);
    const t = (await p.textContent("#view")).replace(/\s+/g, " ");
    if (!t.includes("would have gone broke (fell $") || !/Kelly on the raw model \(over-confident\): bankroll bust/.test(t)) throw new Error("12% pooled broke / bust wording");
  }],
  // Under the Premier League: the DraftKings backtest, then the Pinnacle replay, both labelled.
  ["portfolio-backtest-edge12-e0", async (p) => {
    await p.click('.chip[data-league="E0"]');
    const text = await p.textContent("#view");
    for (const w of ["DraftKings backtest, Premier League", "Replays at DraftKings' and Pinnacle's prices, Premier League"]) {
      if (!text.includes(w)) throw new Error(`12% E0 backtest lacks "${w}"`);
    }
    await bandsCheck(p, "edge12", "E0");
    await sizingCheck(p, "bz-edge12-E0", () => state.data.portfolio.strategy_backtests.edge12.leagues.E0.sizing);
  }],
  ["portfolio-backtest-e0-lean", async (p) => { await pfOpenStrat(p, "lean"); await bandsCheck(p, "lean", "E0"); }],
  // Model view (owner, 22 Oct): each strategy's equation, fitted numbers and calibration.
  ["portfolio-model-lean", async (p) => {
    await p.click('button[data-pf="model"]'); await p.click('.chip[data-league=""]'); await pfOpenStrat(p, "lean");
    const text = await p.textContent("#view");
    for (const w of ["score_k", "Blend Lean (blend of price and model)", "Match model (Dixon-Coles)", "Fitted per competition", "The blend's shape, Premier League", "Lined up with results: every outcome", "Lined up with results: live paper trades"]) {
      if (!text.includes(w)) throw new Error(`Lean model view lacks "${w}"`);
    }
    if ((await p.locator("#view .model-table").count()) < 2) throw new Error("per-league tables under All");
    // Live bins merge only under 3 trades; a bin under 10 is greyed and says "few trades".
    const few = await p.evaluate(() => (pfById("moneyline").live.strategies.find((x) => x.key === "lean").calibration.bins || []).filter((b) => b.few).length);
    const shown = await p.locator('[data-scrub="live-lean-all"] .rc-row.few').count();
    if (shown !== few) throw new Error(`few bins: ${shown} greyed for ${few} in the data`);
    if (few && !(await p.locator('[data-scrub="live-lean-all"] .rc-row.few').first().innerText()).includes("few trades")) throw new Error("few bin label");
    // The blend's shape scrubs in 1-point steps: halfway along the drawn range is 49%.
    await scrubCheck(p, "[data-scrub='shape-E0']");
    await p.keyboard.press("Home");
    for (let i = 0; i < 46; i++) await p.keyboard.press("ArrowRight");
    const sh = await p.textContent("#rc-readout-shape-E0");
    if (!/^Market 50%: blend \d+\.\d% with the model agreeing, \d+\.\d% with the model 10 points higher, \d+\.\d% with it 10 points lower\.$/.test(sh)) throw new Error(`shape readout: ${sh}`);
    // Every outcome: each stop is a bin with all three series in one readout.
    await scrubCheck(p, "[data-scrub='rel-lean-all']");
    const rel = await p.textContent("#rc-readout-rel-lean-all");
    for (const w of ["Model", "Blend", "Price", "outcomes", "said", "happened"]) if (!rel.includes(w)) throw new Error(`reliability readout lacks "${w}": ${rel}`);
    const lit = await p.locator("[data-scrub='rel-lean-all'] .mk.sel").count();
    if (lit < 2) throw new Error(`only ${lit} series highlighted in the bin`);
    if (!text.includes("slope")) throw new Error("slope in words");
  }],
  ["portfolio-model-lean-e0", async (p) => {
    await p.click('.chip[data-league="E0"]');
    const text = await p.textContent("#view");
    for (const w of ["Fitted for Premier League", "Price weight b", "Home advantage", "Lined up with results: its own bets (backtest)"]) {
      if (!text.includes(w)) throw new Error(`Lean E0 model view lacks "${w}"`);
    }
    await p.click('.rc-row[data-rc="bets-lean-E0"][data-rcrow="0"]');
    if (!(await p.textContent("#rc-readout-bets-lean-E0")).includes("bets")) throw new Error("bets readout");
    await scrubCheck(p, "[data-scrub='bets-lean-E0']");
  }],
  ["portfolio-model-lean-e3", async (p) => {
    await p.click('.chip[data-league="E3"]');
    const text = await p.textContent("#view");
    if (!text.includes("Fitted for League Two") || !text.includes("Price weight b")) throw new Error("League Two blend numbers");
    if (!text.includes("No Match model (Dixon-Coles) fit for League Two")) throw new Error("League Two missing match fit note");
  }],
  ["portfolio-model-lean-ucl", async (p) => {
    await p.click('.chip[data-league="UCL"]');
    const text = await p.textContent("#view");
    if (!text.includes("No Blend Lean bets in this backtest for Champions League") || !text.includes("DraftKings' close")) throw new Error("UCL Lean model view");
  }],
  ["portfolio-model-edge12-ucl", async (p) => {
    await pfOpenStrat(p, "edge12");
    const text = await p.textContent("#view");
    if (!text.includes("Fitted for Champions League") || !text.includes("Ridge") || text.includes("The blend's shape")) throw new Error("UCL 12% model view");
  }],
  ["portfolio-model-edge12-e0", async (p) => {
    await p.click('.chip[data-league="E0"]');
    const text = await p.textContent("#view");
    for (const w of ["Match model (Dixon-Coles)", "Home advantage", "Lined up with results: its own bets (backtest)"]) {
      if (!text.includes(w)) throw new Error(`12% E0 model view lacks "${w}"`);
    }
    if (text.includes("Blend Lean (blend of price and model)")) throw new Error("12% shows the blend's equation");
  }],
  ["portfolio-live-edge12", async (p) => {
    await p.click('button[data-pf="live"]');
    await p.click('.chip[data-league=""]'); await pfOpenStrat(p, "edge12");
    const text = await p.textContent("#view");
    if (!text.includes("Your live test: fixed 12%") || text.includes("Wolves v Everton")) throw new Error("12% strategy view");
    const ids = await p.locator("#view button[data-trade]").evaluateAll((els) => els.map((e) => e.dataset.trade));
    if (!ids.length || ids.some((id) => id.endsWith("|lean"))) throw new Error(`a Lean trade shows under 12%: ${ids.join(", ")}`);
    const on = await p.locator(".pf-title").innerText() + " " + await p.locator(".pf-sub").innerText();
    if (!on.includes("Model 12%") || !on.includes("12%+")) throw new Error(`12% heading: ${on}`);
    const avg = await p.locator(".tile", { hasText: "Avg edge" }).locator(".value").innerText();
    if (!avg.endsWith("%")) throw new Error(`12% avg edge should be a %: ${avg}`);
    if (!(await p.locator(".chart.curve").count())) throw new Error("no profit chart for the 12% test");
    if (!text.includes("95% range on ROI")) throw new Error("12% live lacks paper.py's ROI range");
  }],
  ["portfolio-live-edge12-bands", async (p) => {
    await liveBandsCheck(p, "moneyline", "edge12", "");
    await sizingCheck(p, "kz-edge12", () => pfById("moneyline").live.strategies.find((s) => s.key === "edge12").sizing);
    if (!(await p.textContent("#view")).includes("Kelly on the raw model (over-confident):")) throw new Error("Model 12% lacks the raw-model Kelly line");
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
  // (the ledger grows, so page through "Show more" until its row is listed)
  ["portfolio-live-close-sheet", async (p) => {
    const row = p.locator('button[data-trade="E0|2627|Spurs|Brentford"]');
    for (let i = 0; i < 20 && !(await row.count()) && (await p.locator("#pf-more").count()); i++) await p.click("#pf-more");
    await row.click(); await sheet(p);
  }, true],
  ["portfolio-live-trade", async (p) => { await closeSheet(p); await p.click("button[data-trade] >> nth=0"); await sheet(p); }, true],
  // The DraftKings backtest's own trades: Model 12% under the Premier League.
  ["portfolio-backtest", async (p) => { await closeSheet(p); await p.click('button[data-pf="backtest"]'); await p.click('.chip[data-league="E0"]'); await pfOpenStrat(p, "edge12"); await p.click("details.fold > summary"); }],
  ["portfolio-backtest-trade", async (p) => { await p.click("button[data-trade] >> nth=0"); await sheet(p); }, true],
  // Anytime goalscorer is hidden while paused (owner, 24 Oct): no folder for it at the top.
  ["portfolio-top", async (p) => {
    // Tapping the Portfolio tab again goes back to the top level.
    await closeSheet(p); await tab("portfolio")(p);
    if (await p.locator(".crumbs").count()) throw new Error("the Portfolio tab didn't go back to the top");
    if (await p.locator('#view button[data-pfid="goalscorer"]').count()) throw new Error("goalscorer folder should be hidden");
    const text = await p.textContent("#view");
    for (const w of ["Moneyline", "Team corners", "2 strategies:", "Retired:", "Each portfolio holds"]) if (!text.includes(w)) throw new Error(`portfolio top lacks "${w}"`);
  }],
  // A retired portfolio, reached from the "Retired" link: its history, read-only. It has one
  // strategy, so it opens straight on its views.
  ["portfolio-retired-live", async (p) => {
    await p.click('button.linkish[data-pfid="player_shots"]'); await p.click('button[data-pf="live"]');
    if ((await p.locator(".crumbs").innerText()).trim() !== "‹ Portfolio") throw new Error("retired crumbs");
  }],
  // Record → Player shots moved here: below the bet-by-bet backtest, the three strategies
  // compared (switch), their threshold sweep and predicted vs actual.
  ["portfolio-retired-backtest", async (p) => {
    await p.click('button[data-pf="backtest"]');
    const text = await p.textContent("#view");
    for (const w of ["Running profit ($)", "The strategies compared", "Return per bet by edge threshold", "Predicted vs actual win rate", "Its bets are the ones above"]) if (!text.includes(w)) throw new Error(`player shots backtest lacks "${w}"`);
  }],
  ["portfolio-retired-backtest-blend_3h", async (p) => {
    await p.click('button[data-recstrat="blend_3h"]');
    if ((await p.locator(".tiles.player-strat-tiles .tile").count()) !== 4) throw new Error("no tiles for the other strategy");
  }],
  ["portfolio-retired-backtest-raw_3h", async (p) => { await p.click('button[data-recstrat="raw_3h"]'); await p.click('button[data-recstrat="blend_lineup"]'); }],
  ["portfolio-retired-trade", async (p) => { await p.click("button[data-trade] >> nth=0"); await sheet(p); }, true],
  // In-play research (owner, 28 Oct; Comeback bets until then): every study a row with its
  // verdict pill, headline and cost; the top-level row counts them (never live profit).
  ["portfolio-comebacks", async (p) => {
    await closeSheet(p); await pfHome(p);
    const row = (await p.locator('button[data-pfid="comebacks"]').innerText()).replace(/\s+/g, " ");
    for (const w of ["In-play research", "Running", "4 studies", "2 failed · 1 passed · 1 running · never traded"]) if (!row.includes(w)) throw new Error(`research row lacks "${w}": ${row}`);
    if (await p.locator('button.linkish[data-pfid="comebacks"]').count()) throw new Error("research shouldn't sit behind the retired link");
    await p.click('button[data-pfid="comebacks"]');
    const list = await p.locator("#view button[data-pfstrat]").evaluateAll((els) => els.map((e) => e.dataset.pfstrat));
    if (list.join(",") !== "comeback_ht,cashout_ht,heavy_fav_ht,fixture_pass") throw new Error(`research studies: ${list}`);
    if (await p.locator("#view .chips").count()) throw new Error("research needs no competition chips");
    const rowText = async (k) => (await p.locator(`button[data-pfstrat="${k}"]`).innerText()).replace(/\s+/g, " ");
    const cb = await rowText("comeback_ht");
    for (const w of ["$1 on the trailing favourite at half-time", "Failed", "−25.1%", "732 bets", "8,011 credits", "never traded", "Judged on design C"]) if (!cb.includes(w)) throw new Error(`comeback row lacks "${w}": ${cb}`);
    const hv = await rowText("heavy_fav_ht");
    for (const w of ["Running", "Pre-registered Oct 4", "planned 410 bets", "up to 4,300 credits"]) if (!hv.includes(w)) throw new Error(`running row lacks "${w}": ${hv}`);
    const co = await rowText("cashout_ht");
    for (const w of ["Cash out a heavy underdog once it reaches 50%", "Failed", "150,910 credits", "growth a bet vs holding", "570 cash-outs at 50%", "vs hold"]) if (!co.includes(w)) throw new Error(`cash-out row lacks "${w}": ${co}`);
    const ps = await rowText("fixture_pass");
    for (const w of ["Passed", "640 credits", "per $1"]) if (!ps.includes(w)) throw new Error(`passed row lacks "${w}": ${ps}`);
  }],
  ["portfolio-comebacks-result", async (p) => {
    await p.click('button[data-pfstrat="comeback_ht"]');
    const views = await p.locator("#view .segmented button").allInnerTexts();
    if (views.join(",") !== "Result,Why") throw new Error(`comebacks views: ${views}`);
    const text = (await p.textContent("#view")).replace(/\s+/g, " ");
    const vt = await p.evaluate(() => studies().find((x) => x.id === "comeback_ht").verdict_text);
    for (const w of [vt, "No live trades", "732", "123 won", "17.0%", "vs 21.3% the price implied, on the 700 bets with a fair price: −4.3 points (−6.9 to −1.6)", "the win rate counts every bet, but the price's chance only the bets with a fair price (700 of 732 overall)", "−25.1%", "95% range −38.3% to −10.2%", "8,011", "Betfair's exchange after its 2% commission instead: −18.1%", "Design B: top five", "+7.3%", "−18.2%", "Return by league (design C)", "Return by season (design C)", "Won vs the price, by how big a favourite"]) {
      if (!text.includes(w)) throw new Error(`comeback result lacks "${w}"`);
    }
    // The profit chart: three lines (C judged solid, B and both together dashed), a readout
    // naming each, ending on the judged total.
    if ((await p.locator("[data-scrub='cbcv'] path.line.ser").count()) !== 3) throw new Error("comeback profit lines");
    const end = await p.evaluate(() => {
      const c = studies().find((x) => x.id === "comeback_ht").bets.filter((b) => b.design === "C");
      return usd(c.reduce((a, b) => a + (b.won ? b.odds - 1 : -1), 0), 2);
    });
    const ro = await p.textContent("#cbcv-readout");
    if (!ro.includes(`Design C (judged) ${end}`) || !ro.includes("Design B (first look)")) throw new Error(`comeback readout: ${ro} (want C ${end})`);
    await scrubCheck(p, "[data-scrub='cbcv']");
    await p.click('.rc-row[data-rc="cblg"][data-rcrow="0"]');
    if (!(await p.textContent("#rc-readout-cblg")).includes("bets")) throw new Error("league readout");
    await scrubCheck(p, "[data-scrub='cbband']");
  }],
  ["portfolio-comebacks-bets", async (p) => {
    await p.click("details.cb-bets > summary");
    const n = await p.locator(".cb-table tbody tr").count();
    if (n !== 50) throw new Error(`comeback bet list shows ${n}, want 50`);
    const first = await p.locator(".cb-table tbody tr").first().innerText();
    if (!/[+−]\d+ \(\d+\.\d\d\)/.test(first) || !/behind .* at half-time/.test(first)) throw new Error(`bet row: ${first}`);
    await p.click("button[data-cbmore]");
    if ((await p.locator(".cb-table tbody tr").count()) !== 150 || !(await p.locator("details.cb-bets[open]").count())) throw new Error("show more");
  }],
  ["portfolio-comebacks-why", async (p) => {
    await p.click('button[data-pf="why"]');
    const text = (await p.textContent("#view")).replace(/\s+/g, " ");
    for (const w of ["The idea, from free data", "19.7% of 2,166 matches", "expected 14.0%", "5 of 18", "When the underdog scores first, by minute", "15,838 matches", "on the 3,789 matches with a fair benchmark", "75+ min", "The in-play price already covers the comeback, and overrates it.", "17.0% of the time against the 21.3% the price implied, on the 700 bets with a fair price", "18,820 credits", "How this was measured"]) {
      if (!text.includes(w)) throw new Error(`comeback why lacks "${w}"`);
    }
    await p.click("details.mk-fold:has(summary:text('How this was measured')) > summary");
    const how = await p.locator("details.mk-fold[open]").last().innerText();
    for (const w of ["dca429e", "e4b3f8b", "40176031079"]) if (!how.includes(w)) throw new Error(`how-measured lacks ${w}`);
    // (centred first: scrolled to the top edge, its first row would sit under the sticky bar)
    await p.locator("[data-scrub='cbmin']").evaluate((e) => e.scrollIntoView({ block: "center" }));
    await scrubCheck(p, "[data-scrub='cbmin']");
    if ((await p.locator("[data-scrub='cbp1'] .rc-row").count()) !== 2) throw new Error("phase 1 rows");
  }],
  // A study still running (pre-registered, prices being bought) shows its plan.
  ["portfolio-study-running", async (p) => {
    await pfUp(p); await p.click('button[data-pfstrat="heavy_fav_ht"]');
    await p.click('button[data-pf="result"]');
    if (!(await p.locator(".pf-title .pill.v-run").count())) throw new Error("running pill");
    const text = (await p.textContent("#view")).replace(/\s+/g, " ");
    for (const w of ["Running: pre-registered Oct 4", "Pre-registered", "Planned sample", "410", "bets from 430 matches", "4,300", "the most it may spend", "Design H: eight leagues", "design H in commit a291256", "Nothing to judge yet"]) if (!text.includes(w)) throw new Error(`running study lacks "${w}"`);
    await p.click('button[data-pf="why"]');
    if (!(await p.textContent("#view")).includes("How this was measured")) throw new Error("running study why");
    await p.click('button[data-pf="result"]');
  }],
  // A study that passed, with cash-out returns (profit per bet, not odds − 1) and a generic Why.
  ["portfolio-study-pass", async (p) => {
    await pfUp(p); await p.click('button[data-pfstrat="fixture_pass"]');
    await p.click('button[data-pf="result"]');
    const text = (await p.textContent("#view")).replace(/\s+/g, " ");
    for (const w of ["Yes on this sample", "No live trades yet: this idea passed", "Bets (design A)", "Cost in credits", "A 600 · probes 40", "Return by league (design A)", "Won vs the price, by pre-match chance", "Pre-match price's chance", "Design A is the judged test"]) if (!text.includes(w)) throw new Error(`passed study lacks "${w}"`);
    if (text.includes("Betfair")) throw new Error("no Betfair line without Betfair numbers");
    if ((await p.locator("[data-scrub='cbcv'] path.line.ser").count()) !== 1) throw new Error("one profit line");
    const end = await p.evaluate(() => usd(studies().find((x) => x.id === "fixture_pass").bets.reduce((a, b) => a + b.profit, 0), 2));
    if (!(await p.textContent("#cbcv-readout")).includes(end)) throw new Error(`cash-out curve should end at ${end}`);
    await p.locator("[data-scrub='cbcv']").evaluate((e) => e.scrollIntoView({ block: "center" }));
    await scrubCheck(p, "[data-scrub='cbcv']");
    await p.click("details.cb-bets > summary");
    if (!/Cashed out [+−]\$\d/.test(await p.locator(".cb-table tbody tr").first().innerText())) throw new Error("cash-out bet row");
    await p.click('button[data-pf="why"]');
    const why = (await p.textContent("#view")).replace(/\s+/g, " ");
    for (const w of ["1. The idea", "Holding to full time", "2. What it means", "How this was measured"]) if (!why.includes(w)) throw new Error(`passed study why lacks "${w}"`);
    await p.locator("[data-scrub='why0']").evaluate((e) => e.scrollIntoView({ block: "center" }));
    await scrubCheck(p, "[data-scrub='why0']");
    await p.click('button[data-pf="result"]');
  }],
  // The cash-out test (lab/cashout_test.py's output in the app file; made up here): decisions,
  // growth vs holding at the judged stake, hold vs cash-out per $1, the two bankrolls, the grid
  // by threshold and margin, the in-play price's calibration, league and season rows.
  ["portfolio-study-cashout", async (p) => {
    await pfUp(p); await p.click('button[data-pfstrat="cashout_ht"]');
    await p.click('button[data-pf="result"]');
    const text = (await p.textContent("#view")).replace(/\s+/g, " ");
    for (const w of ["Decisions", "570", "cash-outs at 50%", "Growth vs holding, 2% stake", "+0.020%", "Hold, per $1", "Cash out at 50%, per $1", "The judged test: cash out at 50% with a 5% cash-out margin, staking 2%", "on 570 decisions", "The sample.", "Hold vs cash out ($1 a bet)", "less than a billionth of the start", "About these curves", "By threshold and margin", "judged", "Is the in-play price right?", "By league", "By season", "Other samples (descriptive)", "Underdogs at 3.0 to 4.0", "With B and C put back (already read, half-time only)", "The frozen sample at half-time only", "2025/26 only", "Partial cash-out to the Kelly stake"]) if (!text.includes(w)) throw new Error(`cash-out study lacks "${w}"`);
    if ((await p.locator(".co-grid tbody tr").count()) !== 9) throw new Error("the 3 x 3 grid");
    if ((await p.locator(".co-grid tr.co-primary").count()) !== 1) throw new Error("one judged row");
    // The chart is the running profit at $1 a bet (curves.flat), ending on its last point.
    if ((await p.locator("[data-scrub='cbcv'] path.line.ser").count()) !== 2) throw new Error("hold and cash-out lines");
    const end = await p.evaluate(() => { const f = studies().find((x) => x.id === "cashout_ht").curves.flat.points; return usd(f[f.length - 1].hold, 2); });
    if (!(await p.textContent("#cbcv-readout")).includes(`Hold to the end ${end}`)) throw new Error(`flat curve should end at ${end}`);
    for (const id of ["cbcv", "cbcal"]) {
      await p.locator(`[data-scrub='${id}']`).evaluate((e) => e.scrollIntoView({ block: "center" }));
      await scrubCheck(p, `[data-scrub='${id}']`);
    }
    await p.click("details.cb-bets > summary");
    if (!/hold [+−]\$[\d.]+ · cash [+−]\$/.test(await p.locator("details.cb-bets .cb-table tbody tr").first().innerText())) throw new Error("cash-out bet row");
    await p.click('button[data-pf="why"]');
    const why = (await p.textContent("#view")).replace(/\s+/g, " ");
    for (const w of ["Why holding lost so much", "Is the in-play price right?", "What the fee does", "How this was measured"]) if (!why.includes(w)) throw new Error(`cash-out why lacks "${w}"`);
    await p.locator("[data-scrub='why0']").evaluate((e) => e.scrollIntoView({ block: "center" }));
    await scrubCheck(p, "[data-scrub='why0']");
    if (!(await p.textContent("#rc-readout-why0")).includes("holding returned")) throw new Error("why row's hold");
    await p.click('button[data-pf="result"]');
  }],
  // A failed pre-match study sits in Moneyline's Archive with its pill and result: the real
  // strategy search (Research, #131), shown through its own search view (lead brief, 28 Oct).
  ["portfolio-study-moneyline", async (p) => {
    await pfOpenPf(p, "moneyline");
    if (!(await p.locator('.section-title:has-text("Archive") + .card button[data-pfstrat="strategy_search"]').count())) throw new Error("the failed study isn't in the Archive");
    const row = (await p.locator('button[data-pfstrat="strategy_search"]').innerText()).replace(/\s+/g, " ");
    for (const w of ["Strategy search", "Failed", "never traded", "free data, no credits", "None found", "of 11,857 rules", "luck check p 0.94"]) if (!row.includes(w)) throw new Error(`study row lacks "${w}": ${row}`);
    if (!(await p.textContent("#view")).includes("Failed studies stay here with their result")) throw new Error("archive note for studies");
    await p.click('button[data-pfstrat="strategy_search"]');
    await p.click('button[data-pf="result"]');
    if ((await p.locator(".crumbs").innerText()).replace(/\s+/g, " ") !== "‹ Portfolio › Moneyline") throw new Error("study crumbs");
    const text = (await p.textContent("#view")).replace(/\s+/g, " ");
    for (const w of ["No strategy found", "Rules tried", "11,857", "Best CLV", "+1.3%", "luck check p 0.94", "Beat the close", "about 50% by chance", "The best rule, against luck", "a search with no skill finds one as good as this 94% of the time", "So no rule passed", "How many rules beat the close", "Profit over time: the best rule by CLV", "Every market, every side", "Best rules by closing line value", "Best rules by return (ROI)"]) if (!text.includes(w)) throw new Error(`strategy search lacks "${w}"`);
    if (await p.locator("#view .chips").count()) throw new Error("a study page has no competition chips");
    if ((await p.locator("[data-scrub='cbcv'] path.line.ser").count()) !== 1) throw new Error("one curve for the best rule");
    for (const id of ["cbcv", "cbshare", "cbmk"]) {
      await p.locator(`[data-scrub='${id}']`).evaluate((e) => e.scrollIntoView({ block: "center" }));
      await scrubCheck(p, `[data-scrub='${id}']`);
    }
    await p.click("details.cb-bets > summary");
    // (some of its bets have no closing price, so only the market and side are certain)
    if (!/Asian handicap, /.test(await p.locator("details.cb-bets .cb-table tbody tr").first().innerText())) throw new Error("search bet row");
    await p.click('button[data-pf="why"]');
    if (!(await p.textContent("#view")).includes("How this was measured")) throw new Error("search why");
  }],
  // Leaving Comeback bets: another portfolio's views come back on Live paper.
  ["portfolio-comebacks-leave", async (p) => {
    await pfOpenPf(p, "moneyline"); await p.click('button[data-pfstrat="edge12"]');
    if (!(await p.locator('button[data-pf="live"].on').count())) throw new Error("Moneyline should fall back to Live paper after a study");
  }],
  // Team corners (in testing): live paper with Pinnacle CLV and a push, a league chip, the
  // trade sheet, then the research backtest (no money tiles) with a range-plot readout.
  ["portfolio-corners", async (p) => {
    await closeSheet(p); await pfOpenPf(p, "corners");
    const list = await p.textContent("#view");
    for (const want of ["Model (f) 12% vs Pinnacle", "Model (f) 12% vs DraftKings", "Model (f) vs Pinnacle: 12% edge, under 60% to win", "Only the 12%-edge picks model (f) gives under a 60% chance to win", "Archived Oct 25 · still logs every 12%-edge pick", "research only", "Judged from Sep 20 on closing line value"]) if (!list.includes(want)) throw new Error(`Team corners list lacks "${want}"`);
    // The owner archived the two plain 12% tracks (25 Oct): the under-60% lines lead, the
    // tracks they are cut from sit in the Archive list.
    const order = await p.locator("#view button[data-pfstrat]").evaluateAll((els) => els.map((e) => e.dataset.pfstrat));
    if (order.join(",") !== "pinnacle_below60,draftkings_below60,pinnacle,draftkings") throw new Error(`corner rows: ${order.join(",")}`);
    if (!(await p.locator('.section-title:has-text("Archive") + .card button[data-pfstrat="pinnacle"]').count())) throw new Error("the Pinnacle track isn't archived");
    await p.click('button[data-pfstrat="pinnacle"]'); await p.click('button[data-pf="live"]');
    const text = await p.textContent("#view");
    for (const want of ["over 4.5 corners", "Push", "Pinnacle's close", "Live test: fixed 12% on model (f)", "Model (f) 12% vs Pinnacle", "Profit over time"]) {
      if (!text.includes(want)) throw new Error(`Team corners lacks "${want}"`);
    }
    if (await p.locator('#view button[data-trade$="|dk"]').count()) throw new Error("a DraftKings trade shows under the Pinnacle track");
  }],
  ["portfolio-corners-bands", async (p) => {
    await liveBandsCheck(p, "corners", "pinnacle");
    await sizingCheck(p, "kz-pinnacle", () => pfById("corners").live.strategies.find((s) => s.key === "pinnacle").sizing);
  }],
  // The DraftKings track: its own trades, CLV vs DraftKings' close with Pinnacle's as a
  // second line, a one-sided close without DraftKings CLV; Moneyline's selection untouched.
  ["portfolio-corners-dk", async (p) => {
    const before = await p.evaluate(() => localStorage.getItem("pfStrat"));
    await pfOpenStrat(p, "draftkings");
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
  ["portfolio-corners-pin-again", async (p) => { await closeSheet(p); await pfOpenStrat(p, "pinnacle"); }],
  // The under-60% line (owner, 23 Oct; renamed 25 Oct): its own row, and
  // both curves on one chart (parent solid, variant dashed) with a legend, the "filter set"
  // marker and a readout giving both totals and their difference on the same day.
  ["portfolio-corners-overlay", async (p) => {
    await pfUp(p);
    const row = (await p.locator('button[data-pfstrat="pinnacle_below60"]').innerText()).replace(/\s+/g, " ");
    if (!row.includes("Model (f) vs Pinnacle: 12% edge, under 60% to win") || row.includes("↳") || !/[+−-]\$\d/.test(row) || !/\d+–\d+ · [+−]\d/.test(row)) throw new Error(`variant row: ${row}`);
    await pfOpenStrat(p, "pinnacle");
    if (await p.locator(".pf-curve .curve-legend .lg-item").count() !== 2) throw new Error("overlay legend");
    if (await p.locator("[data-scrub='cv'] path.line.ser").count() !== 2 || await p.locator("[data-scrub='cv'] path.line.ser.dash").count() !== 1) throw new Error("overlay curves");
    const mark = await p.locator("[data-scrub='cv'] .filter-lab").textContent();
    if (mark !== "filter set (judged from here)") throw new Error(`filter marker: ${mark}`);
    if (await p.locator("[data-scrub='cv'] text.end.ser").count() !== 2) throw new Error("overlay end labels");
    // The origin is a stop: a pointer left of it selects it, dated by the first trade opened.
    const svg = await p.locator("[data-scrub='cv'] svg").boundingBox();
    await p.locator("[data-scrub='cv'] svg").dispatchEvent("pointerdown", { pointerType: "touch", pointerId: 9, clientX: svg.x + 1, clientY: svg.y + svg.height / 2, bubbles: true });
    await p.locator("[data-scrub='cv'] svg").dispatchEvent("pointerup", { pointerType: "touch", pointerId: 9, bubbles: true });
    const o = (await p.textContent("#cv-readout")).replace(/\s+/g, " ");
    const first = await p.evaluate(() => dayName(firstOpened(stratSet(pfById("corners").live, pfById("corners").live.strategies.find((x) => x.key === "pinnacle")).trades)));
    if (o !== `${first} · first trade opened · All $0.00 · Under 60% to win $0.00`) throw new Error(`overlay origin readout: ${o}`);
    await p.locator("[data-scrub='cv']").focus(); await p.keyboard.press("Home"); await p.keyboard.press("ArrowRight");
    const r = (await p.textContent("#cv-readout")).replace(/\s+/g, " ");
    const want = await p.evaluate(() => plain(scrubCharts.cv.readout(1)));
    if (r !== want || !/ · All [+−]?\$[\d.]+ · Under 60% to win [+−]?\$[\d.]+ \(difference [+−]?\$[\d.]+\)$/.test(r)) throw new Error(`overlay readout: ${r}`);
    await p.keyboard.press("End");
  }],
  // The variant selected: its own trades and tiles, and the "Judged from" box.
  ["portfolio-corners-variant", async (p) => {
    await pfOpenStrat(p, "pinnacle_below60");
    const ids = await p.locator("#view button[data-trade]").evaluateAll((els) => els.map((e) => e.dataset.trade));
    const mine = await p.evaluate(() => pfById("corners").live.strategies.find((s) => s.key === "pinnacle_below60").trade_ids);
    if (!ids.length || ids.some((id) => !mine.includes(id))) throw new Error(`variant lists: ${ids.join(", ")}`);
    const box = (await p.textContent(".card.judged")).replace(/\s+/g, " ");
    for (const want of ["Judged from", "Under 60% to win", "All", "The judged measure, CLV against Pinnacle's close", "a difference of", "95% range", "Not enough trades yet", "was picked after seeing the first", "only trades opened from"]) {
      if (!box.includes(want)) throw new Error(`Judged-from box lacks "${want}": ${box}`);
    }
    if (await p.locator("[data-scrub='cv'] path.line.ser").count() !== 2) throw new Error("variant view lacks the overlay");
    // The whole-period part (owner, 23 Oct): above the judged part, the same columns from
    // each version's full summary, and a descriptive CLV difference.
    if (!box.includes("Whole period, from") || box.indexOf("Whole period") > box.indexOf("Judged from")) throw new Error(`whole period first: ${box}`);
    if (!box.includes("Descriptive only:") || !box.includes("can't prove the filter") || !/minus all: [+−][\d.]+% \(/.test(box)) throw new Error(`whole-period notes: ${box}`);
    const rows = await p.locator(".whole-tab tbody tr").evaluateAll((trs) => trs.map((tr) => [...tr.children].map((td) => td.textContent.trim())));
    const want = await p.evaluate(() => {
      const ss = pfById("corners").live.strategies;
      const row = (label, s) => [label, String(s.trades), String(s.settled), usd(s.profit, 2), signedPct(s.roi), signedPct(s.clv_pinnacle)];
      return [row("Under 60% to win", ss.find((x) => x.key === "pinnacle_below60").summary), row("All", ss.find((x) => x.key === "pinnacle").summary)];
    });
    if (JSON.stringify(rows) !== JSON.stringify(want)) throw new Error(`whole-period rows ${JSON.stringify(rows)} vs ${JSON.stringify(want)}`);
    // Its CLV difference's two means are the table's CLV column.
    const m = box.match(/minus all: ([+−][\d.]+%) \(([+−][\d.]+%) vs ([+−][\d.]+%);/);
    if (!m || m[2] !== want[0][5] || m[3] !== want[1][5]) throw new Error(`whole-period CLV means ${m && m.slice(1)} vs ${want[0][5]}, ${want[1][5]}`);
  }],
  // The DraftKings track's variant: judged on Pinnacle's close too, DraftKings' own close
  // shown as descriptive only.
  ["portfolio-corners-dk-variant", async (p) => {
    await pfOpenStrat(p, "draftkings_below60");
    const box = (await p.textContent(".card.judged")).replace(/\s+/g, " ");
    const judged = box.indexOf("The judged measure, CLV against Pinnacle's close"), desc = box.indexOf("Descriptive only, CLV against DraftKings' close");
    if (judged < 0 || desc < judged) throw new Error(`DraftKings variant box: ${box}`);
    if (!/kept of \d+ trades? with a close, from \d+ match/.test(box)) throw new Error(`DraftKings variant counts: ${box}`);
  }],
  ["portfolio-corners-pin-back", async (p) => { await pfOpenStrat(p, "pinnacle"); }],
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
    // An archived corner track (owner, 25 Oct) has its own subtitle, and its backtest view
    // carries none of Lean's old-rule wording.
    await closeSheet(p); await pfOpenStrat(p, "pinnacle");
    const sub = await p.locator(".pf-sub").innerText();
    if (sub !== "Archived Oct 25: still logs every pick; the under-60% line is cut from it") throw new Error(`archived track subtitle: ${sub}`);
    await p.click('button[data-pf="backtest"]');
    if (/Blend Lean|old rule/.test(await p.textContent("#view"))) throw new Error("archived corner track's backtest talks about Lean's old rule");
    await p.click('.rc-row[data-rc="pfco"][data-rcrow="0"]');
    const text = await p.textContent("#view");
    for (const want of ["No money tiles", "finalist", "February"]) if (!text.includes(want)) throw new Error(`research backtest lacks "${want}"`);
    if (await p.locator(".tiles").count()) throw new Error("research backtest shows money tiles");
    await p.click("details.mk-fold > summary");
  }],
  ["portfolio-corners-model", async (p) => {
    await p.click('button[data-pf="model"]'); await p.click('.chip[data-league=""]'); await pfOpenStrat(p, "pinnacle");
    const text = await p.textContent("#view");
    for (const w of ["Team corners model (f)", "Lined up with results: every logged line", "Pinnacle's fair price", "Lined up with results: live paper trades"]) {
      if (!text.includes(w)) throw new Error(`corners model view lacks "${w}"`);
    }
    for (const w of ["Model (f)'s shape, Premier League", "Over 4.5", "Model (f)"]) if (!text.includes(w)) throw new Error(`corners model view lacks "${w}"`);
    if (!(await p.locator("#view .rel-chart").count())) throw new Error("corners diagonal reliability chart");
    await scrubCheck(p, "[data-scrub='qrel-pinnacle']");
    if (!(await p.textContent("#rc-readout-qrel-pinnacle")).includes("lines")) throw new Error("quote readout");
    await scrubCheck(p, "[data-scrub='cshape-E0']");
    // The shape chart scrubs too: pointing near the left edge reads about 2 expected corners.
    const shape = p.locator("[data-scrub='cshape-E0'] svg");
    await shape.scrollIntoViewIfNeeded();
    const sb = await shape.boundingBox();
    await p.mouse.move(sb.x + sb.width * 0.15, sb.y + sb.height / 2, { steps: 3 });
    const sr = await p.textContent("#rc-readout-cshape-E0");
    if (!/^Expected [0-9.]+ corners: over 3\.5 \d+%, over 4\.5 \d+%, over 5\.5 \d+%\.$/.test(sr)) throw new Error(`corner shape readout: ${sr}`);
  }],
  ["portfolio-corners-model-dk", async (p) => {
    await pfOpenStrat(p, "draftkings"); await p.click('.chip[data-league="E0"]');
    const text = await p.textContent("#view");
    for (const w of ["DraftKings' fair price", "Fitted for Premier League", "Spread (NB2 α)"]) {
      if (!text.includes(w)) throw new Error(`DraftKings corners model view lacks "${w}"`);
    }
    await pfOpenStrat(p, "pinnacle"); await p.click('button[data-pf="live"]');
  }],
  ["portfolio-moneyline", async (p) => { await pfOpenPf(p, "moneyline"); }],
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
// App updates (lead, 23 Oct): with the service worker allowed, the first install doesn't
// reload; a new sw.js taking over reloads the page exactly once; ↻ reloads when the deployed
// sw.js names another version.
async function updateCheck() {
  const swFile = join(site, "sw.js");
  const original = readFileSync(swFile, "utf8");
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: "allow" });
  const page = await ctx.newPage();
  let loads = 0;
  page.on("load", () => { loads += 1; });
  page.on("pageerror", (e) => failures.push(`update/page error: ${e.message}`));
  try {
    await page.goto(BASE);
    await page.waitForSelector("button.match");
    await page.waitForFunction(() => navigator.serviceWorker.controller !== null, null, { timeout: 15000 });
    await page.waitForTimeout(800);
    if (loads !== 1) failures.push(`update/first install: ${loads} loads (the first install must not reload)`);
    // Deploy a "new" app: sw.js with another cache name, then the app's own update check.
    writeFileSync(swFile, original.replace(/soccer-model-v\d+/, "soccer-model-v999"));
    const reloaded = page.waitForEvent("load", { timeout: 15000 });
    await page.evaluate(() => checkForUpdate());
    await reloaded;
    await page.waitForSelector("button.match");
    await page.waitForTimeout(1500);
    if (loads !== 2) failures.push(`update/new worker: ${loads} loads (expected exactly one reload)`);
    // ↻ with the deployed sw.js on another version than this app.js: one reload.
    await page.evaluate(() => sessionStorage.removeItem("appReloadAt"));
    const again = page.waitForEvent("load", { timeout: 15000 });
    await page.click("#refresh");
    await again;
    await page.waitForSelector("button.match");
    await page.waitForTimeout(1000);
    if (loads !== 3) failures.push(`update/refresh: ${loads} loads (expected one more reload)`);
  } catch (err) {
    failures.push(`update: ${String(err.message || err).split("\n")[0]}`);
  } finally {
    writeFileSync(swFile, original);
    await ctx.close();
  }
}
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
    // The header names the running app version, the same as sw.js's cache name.
    const swVersion = readFileSync(join(site, "sw.js"), "utf8").match(/soccer-model-(v\d+)/)[1];
    const sub = await page.textContent("#updated");
    if (!sub.endsWith(`App ${swVersion}`)) failures.push(`${mode}/load: header "${sub}" should end "App ${swVersion}"`);
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
  await updateCheck();
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
