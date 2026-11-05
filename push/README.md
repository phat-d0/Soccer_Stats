# Bet alerts

A phone alert each time a lead strategy opens a paper trade: **Blend Lean**, and **Model (f)
vs DraftKings corners** that pass the "under 60% to win" filter. Nothing else alerts: no
backcasts, shadows, draw no bet tracks, Model 12%, Pinnacle corners or settlements, and each
trade alerts once.

How it works: the publish run (`soccer-stats bet-alerts`, right after the paper trades) sends
each new trade to a small Cloudflare Worker (this folder), which forwards it by Web Push to
every phone that turned alerts on in the app. A tap on the alert opens the app on that trade.

## Set it up (once, about 10 minutes)

You need Node 20 or later on the laptop for step 1 only.

1. **Make the keys.** In this repo on the laptop, run:

   ```
   node push/scripts/vapid-keys.mjs
   ```

   It prints four values: `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `ALERT_TOKEN` and
   `PAIR_CODE`. Keep the window open for steps 4 and 5. Nothing is saved; if you lose them,
   run it again and redo steps 4 and 5.
   (`npx web-push generate-vapid-keys` works too for the two VAPID keys.)

2. **Connect the Worker**, as for your other project: Cloudflare dashboard → **Workers &
   Pages** → **Create** → **Import a repository** → `phat-d0/Soccer_Stats`.
   - Project name: `soccer-model-alerts`
   - Production branch: `claude/soccer-stats-scaffold`
   - Root directory (under Advanced / Build settings): `push`
   - Build command: leave empty. Deploy command: `npx wrangler deploy`
   - Build watch paths: `push/*` (so other pushes to the repo don't redeploy it)

   The first deploy fails until step 3 is done. That's expected.

3. **Make the storage.** Dashboard → **Storage & Databases** → **KV** → **Create** → name it
   `soccer-model-alerts`. Copy its **ID**. Open `push/wrangler.jsonc` on GitHub (pencil icon),
   replace `PASTE_KV_NAMESPACE_ID_HERE` with the ID and commit to
   `claude/soccer-stats-scaffold`. That commit deploys the Worker. (Or send the ID to the lead,
   who will commit it.)

4. **Set the Worker's secrets.** Dashboard → Workers & Pages → `soccer-model-alerts` →
   **Settings** → **Variables and Secrets** → **Add**, type **Secret**, four times:
   `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `ALERT_TOKEN`, `PAIR_CODE`, each with its value
   from step 1. Then open the Worker's address (shown on its page, like
   `https://soccer-model-alerts.<you>.workers.dev`) followed by `/health`: it should say
   `{"ok":true}`.

5. **Tell GitHub.** github.com/phat-d0/Soccer_Stats → **Settings** → **Secrets and variables**
   → **Actions** → **New repository secret**, twice:
   - `ALERT_URL`: the Worker's address from step 4 (no `/health`).
   - `ALERT_TOKEN`: the same value as in step 4.

6. **Turn alerts on in the app.** After the next site update (within the hour):
   - On iPhone, alerts work only in the home-screen app, iOS 16.4 or later: in Safari, Share
     → Add to Home Screen, then open Soccer Model from the home screen.
   - Portfolio → scroll to **Bet alerts** → type the `PAIR_CODE` from step 1 → **Turn on** →
     allow notifications. Then **Send a test**.

To stop alerts on a phone, tap **Turn off** there. To stop them everywhere, delete the
`ALERT_URL` GitHub secret.

## What runs where

| Piece | File | Notes |
| --- | --- | --- |
| Worker | `push/src/index.js`, `push/src/webpush.js` | Routes `GET /health`, `GET /vapid-public-key`, `POST /subscribe` (pairing code), `POST /unsubscribe`, `POST /test` (pairing code), `POST /alert` (Bearer `ALERT_TOKEN`; a phone answering 404 or 410 is dropped). Web Push with VAPID and aes128gcm on WebCrypto only, no libraries. Subscriptions live in the KV namespace bound as `SUBS`. |
| Tests | `push/test/worker.test.mjs` | `node --test push/test/*.test.mjs` (CI runs it): RFC 8291's worked example byte for byte, the VAPID signature, every route against a stub push service. No Cloudflare account needed. |
| Keys | `push/scripts/vapid-keys.mjs` | Run once (step 1). |
| Sender | `src/soccer_stats/bet_alerts.py`, `soccer-stats bet-alerts` | In `publish.yml` after `paper`. Off without `ALERT_URL`/`ALERT_TOKEN` or off the default branch; never fails the publish. Sent ids in data-log `alerts/sent.jsonl`. Log line "Bet alerts: N sent (Lean L, corners C), M skipped". |
| App | `web/app.js` (Portfolio → Bet alerts), `web/sw.js` (push, notificationclick) | The Worker's address comes from `data.json` → `bet_alerts.worker` (the `ALERT_URL` secret); the pairing code is kept on the phone. |

The pairing code stops strangers subscribing to your alerts, and the token stops anyone else
sending them. Neither is in the repo or in `data.json`; only the Worker's address is public.
