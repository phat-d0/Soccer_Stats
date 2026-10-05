// "Ask" tab: chat with Claude about the model's and the bookmakers' numbers.
//
// Runs entirely in the browser. The user's own Anthropic API key is kept in this
// device's localStorage and sent only to api.anthropic.com. Claude gets the current
// fixtures, ratings and track record in its system prompt, plus three tools that
// compute from the same data the app shows (window.PL, set up by app.js).
import Anthropic from "./vendor/anthropic-sdk.mjs";

const PL = window.PL;
const MODELS = {
  "claude-opus-5-5": { label: "Claude Opus 5.5", in: 4, out: 20, cacheRead: 0.2, cacheWrite: 5 },
  "claude-sonnet-5-5": { label: "Claude Sonnet 5.5 (cheaper)", in: 2, out: 10, cacheRead: 0.2, cacheWrite: 2.5 },
};
const DEFAULT_MODEL = "claude-opus-5-5";
const MAX_TOOL_ROUNDS = 6;

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage full or blocked */ } },
  del(k) { try { localStorage.removeItem(k); } catch { /* blocked */ } },
};

const chat = {
  key: store.get("anthropicKey", ""),
  model: MODELS[store.get("chatModel", DEFAULT_MODEL)] ? store.get("chatModel", DEFAULT_MODEL) : DEFAULT_MODEL,
  conv: store.get("chatConv", null), // {dataAt, system, api: [...messages], display: [...]}
  busy: false,
  live: "", // streaming text of the answer in progress
  status: "",
  abort: null,
  draft: "",
  showSettings: false,
};

const esc = PL.esc;
const pct = (x) => (x == null ? null : Math.round(x * 1000) / 10); // 0.6123 -> 61.2

// ---------- data handed to Claude ----------

function fixtureFacts(fx) {
  const imp = PL.impliedFor(fx);
  const pick = PL.bestPick(fx, 0);
  return {
    kickoff: fx.kickoff,
    match: `${fx.home} v ${fx.away}`,
    model_pct: { home: pct(fx.p.home), draw: pct(fx.p.draw), away: pct(fx.p.away), over25: pct(fx.p.over25), under25: pct(fx.p.under25), btts: pct(fx.p.btts) },
    bookmaker_pct: imp.home != null ? { home: pct(imp.home), draw: pct(imp.draw), away: pct(imp.away), over25: pct(imp.over25), under25: pct(imp.under25) } : "odds not posted yet",
    odds: imp.home != null ? fx.odds : undefined,
    expected_goals: fx.xg.map((v) => Math.round(v * 100) / 100),
    best_edge: pick ? { market: pick.market, odds: pick.odds, edge_pct: pct(pick.edge) } : null,
    few_matches_warning: fx.low_data || undefined,
  };
}

function buildSystem(d) {
  const ratings = d.ratings.map((t, i) => ({
    rank: i + 1,
    team: t.team,
    scores_per_game: Math.round(t.goals_for * 100) / 100,
    concedes_per_game: Math.round(t.goals_against * 100) / 100,
    season_xg_for: t.xg_for != null ? Math.round(t.xg_for * 100) / 100 : undefined,
    season_xg_against: t.xg_against != null ? Math.round(t.xg_against * 100) / 100 : undefined,
  }));
  const rec = d.record.model;
  const s3 = rec ? PL.summarize(rec.bets, 0.03) : null;
  const facts = {
    data_updated: d.generated_at,
    season: d.season,
    last_result_in_data: d.last_result,
    xg_weight: d.xg_weight,
    upcoming_fixtures: d.fixtures.map(fixtureFacts),
    team_ratings: ratings,
    track_record_summary: rec && {
      since: d.record_start,
      matches: rec.matches,
      model_log_loss: rec.log_loss,
      bookmaker_closing_log_loss: rec.market_log_loss,
      at_3pct_edge: { bets: s3.n, profit_units: Math.round(s3.profit * 10) / 10, roi_pct: pct(s3.roi), avg_clv_pct: pct(s3.clv), beat_close_pct: pct(s3.beat) },
    },
  };
  return `You are the assistant inside "PL Model", a personal iPhone app that compares a Premier League prediction model with bookmaker odds. The user asks about the model's numbers, the bookmakers' numbers and where they disagree.

How the numbers work:
- The model is a Dixon-Coles Poisson model. It rates each team's attack and defence from past results, with recent matches weighted more, and blends expected goals (xG weight ${d.xg_weight}) with real goals. It adds home advantage and predicts each side's expected goals, then the chance of every scoreline, then market probabilities.
- "Bookmaker %" is Pinnacle's odds (or the market average when Pinnacle's isn't posted) converted to probabilities with the bookmaker's margin removed (Shin's method), so home/draw/away sum to 100%.
- Edge = model probability x decimal odds - 1. The app flags a value bet when edge clears the user's threshold (default 3%).
- The model knows nothing about injuries, line-ups, suspensions, motivation or news. Bookmakers do, and Pinnacle's closing line is very hard to beat; a big disagreement is often the model missing information rather than the bookmaker being wrong.
- The track record replays the model week by week using only data available before each match. Closing line value (CLV) is the most reliable sign of real edge; profit over a few hundred bets is noisy.

How to answer:
- Latency-sensitive; begin your visible answer immediately.
- The user reads on a phone: keep answers short and conversational, a few sentences or a short bullet list. No tables or headings. Round percentages to whole numbers unless the user asks for more.
- Use the data below and your tools; never invent numbers. If something isn't in the data (injuries, news, other leagues), say so plainly.
- Be honest about uncertainty. Don't promise winnings or tell the user to bet; you can explain what the numbers imply. If they ask about staking, mention fractional Kelly and bankroll limits.

Current data (JSON):
${JSON.stringify(facts)}`;
}

const TOOLS = [
  {
    name: "predict_matchup",
    description: "Model prediction for any two Premier League teams, first team at home: expected goals, home/draw/away, over/under 2.5, both teams to score, and the most likely scorelines. If the pairing is an upcoming fixture, also returns the bookmaker's probabilities, odds and edges.",
    input_schema: {
      type: "object",
      properties: {
        home: { type: "string", description: "Home team, spelled as in team_ratings" },
        away: { type: "string", description: "Away team, spelled as in team_ratings" },
      },
      required: ["home", "away"],
      additionalProperties: false,
    },
    strict: true,
    eager_input_streaming: true,
  },
  {
    name: "track_record",
    description: "How the model's replayed bets would have done at a given minimum edge (e.g. 0.05 = 5%): bets, profit, ROI, closing line value, how often it beat the closing odds. Also compares the goals-only model when available.",
    input_schema: {
      type: "object",
      properties: { min_edge: { type: "number", description: "Minimum edge as a fraction, 0 to 0.3" } },
      required: ["min_edge"],
      additionalProperties: false,
    },
    strict: true,
    eager_input_streaming: true,
  },
  {
    name: "past_bets",
    description: "The model's replayed bets, newest first, optionally only matches involving one team, with the result of each bet.",
    input_schema: {
      type: "object",
      properties: {
        team: { type: ["string", "null"], description: "Team name, or null for all teams" },
        min_edge: { type: "number", description: "Minimum edge as a fraction, e.g. 0.03" },
        limit: { type: "integer", description: "How many bets to return, 1 to 40" },
      },
      required: ["team", "min_edge", "limit"],
      additionalProperties: false,
    },
    strict: true,
    eager_input_streaming: true,
  },
];

function findTeam(name) {
  const teams = PL.state.data.teams;
  if (typeof name !== "string") return null;
  const n = name.trim().toLowerCase();
  return teams.find((t) => t.toLowerCase() === n) || null;
}

function runTool(name, input) {
  const d = PL.state.data;
  const num = (v, lo, hi) => typeof v === "number" && Number.isFinite(v) && v >= lo && v <= hi;
  if (name === "predict_matchup") {
    const home = findTeam(input?.home), away = findTeam(input?.away);
    if (!home || !away || home === away) {
      throw new Error(`Unknown or identical teams. Valid names: ${d.teams.join(", ")}`);
    }
    const { m, lam, mu } = PL.scoreMatrix(d.params, home, away);
    const { p, top } = PL.marketsFrom(m);
    const fx = d.fixtures.find((f) => f.home === home && f.away === away);
    return {
      match: `${home} v ${away}`,
      expected_goals: [Math.round(lam * 100) / 100, Math.round(mu * 100) / 100],
      model_pct: { home: pct(p.home), draw: pct(p.draw), away: pct(p.away), over25: pct(p.over25), under25: pct(p.under25), btts: pct(p.btts) },
      likely_scores: top.map(([s, v]) => `${s} (${pct(v)}%)`),
      upcoming_fixture: fx ? fixtureFacts(fx) : "not in the upcoming fixtures list",
    };
  }
  if (name === "track_record") {
    if (!num(input?.min_edge, 0, 0.3)) throw new Error("min_edge must be a number from 0 to 0.3");
    const out = {};
    for (const [label, rec] of Object.entries(d.record)) {
      if (!rec?.bets) continue;
      const s = PL.summarize(rec.bets, input.min_edge);
      out[label === "model" ? "goals_plus_xg" : "goals_only"] = {
        bets: s.n, profit_units: Math.round(s.profit * 10) / 10, roi_pct: pct(s.roi),
        avg_clv_pct: pct(s.clv), beat_close_pct: pct(s.beat),
        log_loss: rec.log_loss, bookmaker_log_loss: rec.market_log_loss,
      };
    }
    return { since: d.record_start, min_edge: input.min_edge, ...out };
  }
  if (name === "past_bets") {
    if (!num(input?.min_edge, 0, 0.3) || !Number.isInteger(input?.limit) || input.limit < 1 || input.limit > 40) {
      throw new Error("min_edge must be 0-0.3 and limit an integer 1-40");
    }
    const team = input.team == null ? null : findTeam(input.team);
    if (input.team != null && !team) throw new Error(`Unknown team. Valid names: ${d.teams.join(", ")}`);
    const bets = PL.summarize(d.record.model.bets, input.min_edge).sel
      .filter((b) => !team || b[1] === team || b[2] === team)
      .slice(-input.limit).reverse()
      .map((b) => ({ date: b[0], match: `${b[1]} v ${b[2]}`, pick: b[3], odds: b[4], edge_pct: pct(b[5]), clv_pct: pct(b[6]), result: b[7] > 0 ? "won" : "lost", profit_units: Math.round(b[7] * 100) / 100 }));
    return { team: team || "all", count: bets.length, bets };
  }
  throw new Error(`Unknown tool ${name}`);
}

// ---------- conversation ----------

function newConversation() {
  const d = PL.state.data;
  chat.conv = { dataAt: d.generated_at, system: buildSystem(d), api: [], display: [] };
  store.set("chatConv", chat.conv);
}

function costOf(usage, model) {
  const r = MODELS[model] || MODELS[DEFAULT_MODEL];
  if (!usage) return 0;
  return (
    (usage.input_tokens || 0) * r.in +
    (usage.output_tokens || 0) * r.out +
    (usage.cache_read_input_tokens || 0) * r.cacheRead +
    (usage.cache_creation_input_tokens || 0) * r.cacheWrite
  ) / 1e6;
}

function explainError(err) {
  if (err instanceof Anthropic.AuthenticationError) return "Your API key was rejected. Check it in Settings.";
  if (err instanceof Anthropic.PermissionDeniedError) return "This API key isn't allowed to use that model.";
  if (err instanceof Anthropic.RateLimitError) return "Rate limited, or the key is out of credit. Wait a moment, or check your balance at console.anthropic.com.";
  if (err instanceof Anthropic.APIConnectionError) return "Couldn't reach Anthropic. Check your connection.";
  if (err instanceof Anthropic.APIUserAbortError) return "Stopped.";
  if (err instanceof Anthropic.APIError) return `Claude API error${err.status ? ` (${err.status})` : ""}: ${err.message}`;
  return `Something went wrong: ${err?.message || err}`;
}

async function ask(question) {
  question = question.trim();
  if (!question || chat.busy) return;
  if (!chat.key) { chat.showSettings = true; rerender(); return; }
  if (!chat.conv) newConversation();

  const conv = chat.conv;
  conv.api.push({ role: "user", content: question });
  conv.display.push({ role: "user", text: question });
  chat.busy = true; chat.live = ""; chat.status = "Thinking…"; chat.draft = "";
  rerender();

  const client = new Anthropic({ apiKey: chat.key, dangerouslyAllowBrowser: true, maxRetries: 2 });
  const model = chat.model;
  let spent = 0;
  let answer = "";
  let note = "";
  try {
    for (let round = 0; round <= MAX_TOOL_ROUNDS; round++) {
      const controller = new AbortController();
      chat.abort = controller;
      const stream = client.beta.messages.stream({
        model,
        max_tokens: 16000,
        betas: ["server-side-fallback-2026-07-01"],
        fallbacks: "default",
        output_config: { effort: "medium" },
        cache_control: { type: "ephemeral" },
        system: conv.system,
        tools: TOOLS,
        messages: conv.api,
      }, { signal: controller.signal });
      stream.on("text", (_delta, snapshot) => {
        chat.live = answer + snapshot; chat.status = ""; updateLive();
      });

      let message;
      try {
        message = await stream.finalMessage();
      } catch (err) {
        // A tool input that couldn't be parsed at all: re-issue the turn once.
        if (err instanceof Anthropic.APIError || round === MAX_TOOL_ROUNDS) throw err;
        continue;
      }
      spent += costOf(message.usage, model);
      const text = message.content.filter((b) => b.type === "text").map((b) => b.text).join("");
      if (text) answer += (answer ? "\n\n" : "") + text;

      if (message.stop_reason === "refusal") { note = "Claude declined to answer that."; break; }
      if (message.stop_reason === "max_tokens") { note = "The answer was cut off."; break; }

      // Append the whole assistant turn (thinking blocks included) unchanged.
      conv.api.push({ role: "assistant", content: message.content });
      const calls = message.content.filter((b) => b.type === "tool_use");
      if (message.stop_reason !== "tool_use" || !calls.length) break;
      if (round === MAX_TOOL_ROUNDS) { note = "Stopped after several lookups."; break; }

      chat.status = "Checking the numbers…"; chat.live = answer; updateLive();
      const results = calls.map((c) => {
        try {
          return { type: "tool_result", tool_use_id: c.id, content: JSON.stringify(runTool(c.name, c.input)) };
        } catch (e) {
          return { type: "tool_result", tool_use_id: c.id, content: String(e.message || e), is_error: true };
        }
      });
      conv.api.push({ role: "user", content: results });
    }
  } catch (err) {
    note = explainError(err);
    // Keep history valid: drop the unanswered question if nothing was appended for it.
    const last = conv.api[conv.api.length - 1];
    if (last && last.role === "user" && typeof last.content === "string") conv.api.pop();
  } finally {
    chat.busy = false; chat.abort = null; chat.live = ""; chat.status = "";
    conv.display.push({ role: "assistant", text: answer, note, cost: spent, model });
    store.set("chatConv", conv);
    rerender();
  }
}

// ---------- rendering ----------

// Tiny markdown: paragraphs, **bold**, *italic*, `code` and "-" / "1." lists. Escapes first.
function md(text) {
  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*(?!\s)(.+?)\*(?!\*)/g, "$1<i>$2</i>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
  const out = [];
  let list = null;
  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    const bullet = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (bullet) {
      if (!list) { list = []; }
      list.push(`<li>${inline(bullet[1])}</li>`);
      continue;
    }
    if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; }
    if (line.trim()) out.push(`<p>${inline(line.replace(/^#+\s*/, ""))}</p>`);
  }
  if (list) out.push(`<ul>${list.join("")}</ul>`);
  return out.join("");
}

const SUGGESTIONS = [
  "Which matches have value this round, and why?",
  "Where does the model disagree most with the bookmakers?",
  "How has the model done against the closing odds?",
  "Explain the model's numbers for the biggest game this round",
];

function settingsHtml() {
  const masked = chat.key ? `${chat.key.slice(0, 10)}…${chat.key.slice(-4)}` : "";
  return `
    <div class="card chat-settings">
      <h3>Claude settings</h3>
      <p class="note" style="margin:0 0 10px">Answers use your own Anthropic API key, billed pay-as-you-go by Anthropic (typically a few cents a question; each answer shows its cost). Create one at <b>console.anthropic.com</b> → API Keys, and set a monthly spend limit there. The key is stored only on this phone and sent only to Anthropic.</p>
      <label class="field"><span>API key</span>
        <input id="chat-key" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="${chat.key ? esc(masked) : "sk-ant-…"}">
      </label>
      <label class="field"><span>Model</span>
        <select id="chat-model">${Object.entries(MODELS).map(([id, m]) => `<option value="${id}" ${id === chat.model ? "selected" : ""}>${esc(m.label)}</option>`).join("")}</select>
      </label>
      <div class="row">
        <button class="btn primary" data-chat="save-settings">Save</button>
        ${chat.key ? '<button class="btn" data-chat="forget-key">Remove key</button>' : ""}
        ${chat.key ? '<button class="btn" data-chat="close-settings">Cancel</button>' : ""}
      </div>
    </div>`;
}

function bubbles() {
  const conv = chat.conv;
  const items = (conv?.display || []).map((m) => m.role === "user"
    ? `<div class="msg user">${esc(m.text)}</div>`
    : `<div class="msg bot">${m.text ? md(m.text) : ""}${m.note ? `<p class="msg-note">${esc(m.note)}</p>` : ""}${m.cost ? `<div class="msg-meta">≈ ${(m.cost * 100).toFixed(1)}¢ · ${esc((MODELS[m.model] || {}).label || m.model)}</div>` : ""}</div>`);
  if (chat.busy) {
    items.push(`<div class="msg bot" id="live">${chat.live ? md(chat.live) : ""}${chat.status ? `<p class="typing">${esc(chat.status)}</p>` : ""}</div>`);
  }
  return items.join("");
}

function viewAsk() {
  const d = PL.state.data;
  if (chat.showSettings || !chat.key) return settingsHtml();
  const stale = chat.conv && chat.conv.dataAt !== d.generated_at;
  const empty = !chat.conv || !chat.conv.display.length;
  return `
    <div class="chat-top">
      <span class="note" style="margin:0">Ask Claude</span>
      <span class="nowrap">
        <button class="link" data-chat="new">New chat</button>
        <button class="link" data-chat="settings">Settings</button>
      </span>
    </div>
    ${stale ? '<div class="banner">Predictions have updated since this chat started. Start a <button class="link" data-chat="new">new chat</button> for the latest numbers.</div>' : ""}
    <div id="chat-log" class="chat-log" aria-live="polite">
      ${empty ? `<div class="suggest">${SUGGESTIONS.map((s) => `<button class="chip" data-ask="${esc(s)}">${esc(s)}</button>`).join("")}</div>` : bubbles()}
    </div>
    <form id="chat-form" class="chat-input">
      <textarea id="chat-q" rows="1" placeholder="Ask about a match or the odds…" ${chat.busy ? "disabled" : ""}>${esc(chat.draft)}</textarea>
      ${chat.busy
        ? '<button type="button" class="send" data-chat="stop" aria-label="Stop">■</button>'
        : '<button type="submit" class="send" aria-label="Send">↑</button>'}
    </form>`;
}

let mounted = null;
function rerender() {
  if (PL.state.tab !== "ask") return;
  PL.render();
}
function updateLive() {
  const el = document.getElementById("live");
  if (!el) return rerender();
  el.innerHTML = `${chat.live ? md(chat.live) : ""}${chat.status ? `<p class="typing">${esc(chat.status)}</p>` : ""}`;
  scrollToEnd();
}
function scrollToEnd() {
  window.scrollTo({ top: document.body.scrollHeight });
}

function afterRender() {
  mounted = document.getElementById("chat-form");
  const q = document.getElementById("chat-q");
  if (q) {
    const grow = () => { q.style.height = "auto"; q.style.height = `${Math.min(q.scrollHeight, 140)}px`; };
    grow();
    q.addEventListener("input", () => { chat.draft = q.value; grow(); });
    q.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing && window.matchMedia("(hover: hover)").matches) {
        e.preventDefault(); ask(q.value);
      }
    });
  }
  if (chat.conv?.display.length || chat.busy) scrollToEnd();
}

document.addEventListener("submit", (e) => {
  if (e.target.id !== "chat-form") return;
  e.preventDefault();
  ask(document.getElementById("chat-q").value);
});

document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-chat], [data-ask]");
  if (!t) return;
  if (t.dataset.ask) { ask(t.dataset.ask); return; }
  switch (t.dataset.chat) {
    case "new": newConversation(); rerender(); break;
    case "settings": chat.showSettings = true; rerender(); break;
    case "close-settings": chat.showSettings = false; rerender(); break;
    case "stop": chat.abort?.abort(); break;
    case "forget-key": chat.key = ""; store.del("anthropicKey"); rerender(); break;
    case "save-settings": {
      const k = document.getElementById("chat-key").value.trim();
      if (k) { chat.key = k; store.set("anthropicKey", k); }
      chat.model = document.getElementById("chat-model").value;
      store.set("chatModel", chat.model);
      if (chat.key) chat.showSettings = false;
      rerender();
      break;
    }
  }
});

// Called from a match's detail sheet: open the Ask tab with that match pre-asked.
function askAbout(question) {
  PL.setTab("ask");
  if (!chat.key) { chat.draft = question; rerender(); return; }
  ask(question);
}

PL.registerChat({ view: viewAsk, afterRender, askAbout });
