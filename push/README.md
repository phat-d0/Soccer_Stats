# Bet alerts

A phone alert each time a lead strategy opens a paper trade: **Blend Lean**, and **Model (f)
vs DraftKings corners** that pass the "under 60% to win" filter. Nothing else alerts: no
backcasts, shadows, draw no bet tracks, Model 12%, Pinnacle corners or settlements, and each
trade alerts once.

How it works: the publish run (`soccer-stats bet-alerts`, right after the paper trades) sends
each new trade to a small Cloudflare Worker (this folder), which forwards it by Web Push to
every phone that turned alerts on in the app. A tap on the alert opens the app on that trade.

## Set it up (once, about 10 minutes)

Cloudflare renames buttons now and then; if a name below doesn't match exactly, look for the
nearest one.

1. [ ] **Make the keys** (laptop, Node 20+, in this repo): `node push/scripts/vapid-keys.mjs`.
   It prints `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `ALERT_TOKEN`, `PAIR_CODE`. Keep the
   window open for steps 4 and 5 (nothing is saved; run it again if you lose them, and redo 4
   and 5).
2. [ ] **Connect the Worker.** dash.cloudflare.com → **Workers & Pages** → **Create** →
   **Import a repository** → **GitHub** → `phat-d0/Soccer_Stats` → then:
   - **Project name**: `soccer-model-alerts`
   - **Build command**: leave empty
   - **Deploy command**: `npx wrangler deploy`
   - **Advanced settings** → **Path** (root directory): `push`
   - **Create and deploy**. The first build fails (no storage yet); that's expected.
   - Then the Worker's **Settings** → **Build** → **Branch control** → **Production branch**:
     `claude/soccer-stats-scaffold`, and **Build watch paths** → **Include paths**: `push/*`.
3. [ ] **Make the storage.** **Storage & databases** → **Workers KV** → **Create instance**
   (or **Create namespace**) → **Namespace name** `soccer-model-alerts` → **Create**. Copy its
   **ID**. On github.com open `push/wrangler.jsonc` → pencil icon → replace
   `PASTE_KV_NAMESPACE_ID_HERE` with the ID → **Commit changes** to
   `claude/soccer-stats-scaffold`. That commit builds and deploys the Worker. (Or send the ID to
   the lead, who commits it.)
4. [ ] **Worker secrets.** **Workers & Pages** → `soccer-model-alerts` → **Settings** →
   **Variables and Secrets** → **+ Add**, four times: **Type** `Secret`, **Variable name** and
   **Value** from step 1: `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `ALERT_TOKEN`, `PAIR_CODE` →
   **Deploy**.
   - **Success:** open the Worker's address (on its **Overview**, like
     `https://soccer-model-alerts.<you>.workers.dev`) plus `/health`. It shows `{"ok":true}`.
5. [ ] **GitHub secrets.** github.com/phat-d0/Soccer_Stats → **Settings** → **Secrets and
   variables** → **Actions** → **New repository secret**, twice (**Name**, **Secret**, **Add
   secret**):
   - `ALERT_URL`: the Worker's address from step 4, without `/health`;
   - `ALERT_TOKEN`: the same value as in step 4.
6. [ ] **Turn alerts on, on the phone.** After the next site update (within the hour):
   - iPhone: alerts work only in the home-screen app, iOS 16.4 or later. In Safari: **Share** →
     **Add to Home Screen**, then open Soccer Model from the home screen.
   - **Portfolio** → scroll to **Bet alerts** → type the `PAIR_CODE` → **Turn on** → **Allow**.
   - **Success:** the card says **On**; **Send a test** brings "Bet alerts are on" within
     seconds.
7. [ ] **Check the whole path** (optional, the same route a real trade takes): github.com →
   **Actions** → **Publish app** → **Run workflow** → tick **Send a test alert** → **Run
   workflow**.
   - **Success:** the phone shows "Test alert from Soccer Model", and the run's log has
     "Bet alerts: test sent to 1 phone".

After that, every publish run logs "Bet alerts: N sent (Lean L, corners C), M skipped".

To stop alerts on one phone, tap **Turn off** there. To stop them everywhere, delete the
`ALERT_URL` GitHub secret.

## What runs where

| Piece | File | Notes |
| --- | --- | --- |
| Worker | `push/src/index.js`, `push/src/webpush.js` | Routes `GET /health`, `GET /vapid-public-key`, `POST /subscribe` (pairing code), `POST /unsubscribe`, `POST /test` (pairing code), `POST /alert` (Bearer `ALERT_TOKEN`; a phone answering 404 or 410 is dropped). Web Push with VAPID and aes128gcm on WebCrypto only, no libraries. Subscriptions live in the KV namespace bound as `SUBS`. |
| Tests | `push/test/worker.test.mjs` | `node --test push/test/*.test.mjs` (CI runs it): RFC 8291's worked example byte for byte, the VAPID signature, every route against a stub push service. No Cloudflare account needed. |
| Keys | `push/scripts/vapid-keys.mjs` | Run once (step 1). |
| Sender | `src/soccer_stats/bet_alerts.py`, `soccer-stats bet-alerts` | In `publish.yml` after `paper`. Off without `ALERT_URL`/`ALERT_TOKEN` or off the default branch; never fails the publish. Sent ids in data-log `alerts/sent.jsonl` (the record across runs). Log line "Bet alerts: N sent (Lean L, corners C), M skipped". `--test` (publish.yml's **Send a test alert** input) sends one test alert. |
| App | `web/app.js` (Portfolio → Bet alerts), `web/sw.js` (push, notificationclick) | The Worker's address comes from `data.json` → `bet_alerts.worker` (the `ALERT_URL` secret); the pairing code is kept on the phone. |

The pairing code stops strangers subscribing to your alerts, and the token stops anyone else
sending them. Neither is in the repo or in `data.json`; only the Worker's address is public.
