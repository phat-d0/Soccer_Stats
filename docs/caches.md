# Caches: keep-alive and the paid-odds archive

Lead brief, 28 Oct. Owner decision: make the paid odds permanent and keep every cache in use. Infra
only, 0 credits; no job here gets `ODDS_API_KEY`.

## Why

GitHub evicts a cache that hasn't been used for 7 days. Once the repository passes 10 GB, it also
evicts the least recently used ones. The odds we bought with credits lived only in caches. After a
quiet spell they could be lost, and buying them back would cost thousands of credits (the comeback
snapshots alone were about 8,000).

## The caches

**Paid** (`cache_archive.PAID`; bought with credits; archived):

| Cache (key prefix) | Paths, as saved | What | Saved by |
| --- | --- | --- | --- |
| `odds-history-` | `data/raw/odds_history` | DraftKings historical h2h snapshots | `backfill.yml` |
| `player-odds-history-` | `data/raw/player_odds_history` | FanDuel historical player lines | `players.yml` |
| `cup-odds-history-` | `data/raw/cup_odds`, `data/raw/cups/espn` | Champions League closes (Pinnacle, DraftKings) | `backfill.yml` `cup_odds` |
| `comeback-odds-` | `data/raw/comeback_odds` | comeback phase 2 in-play snapshots, designs B and C | `odds-check.yml` `comeback-fetch` |
| `archive-probe-` | `data/raw/archive_probe` | in-play archive probes, one snapshot per league-season | `odds-check.yml` `archive-probe` |
| `inplay-probe-` | `data/raw/inplay_probe` | in-play probe answers, live and historical (from 28 Oct) | `odds-check.yml` `inplay-probe` |

**What the first run found** (28 Oct, `caches.yml` dry run 40640065501, before any archive):

| Cache | State | Files | On disk | Packed (tar + zstd) |
| --- | --- | --- | --- | --- |
| `comeback-odds-` | hit (`comeback-odds-40176031079`) | 801 | 37.4 MB | 1.5 MB |
| `player-odds-history-` | hit (`player-odds-history-40311911428`) | 3,541 | 21.8 MB | 1.2 MB |
| `odds-history-` | **missing: already evicted** | – | – | – |
| `cup-odds-history-` | **missing: already evicted** | – | – | – |

- **The two evicted caches are gone.** The raw DraftKings historical snapshots (`odds-history-`)
  and the bought Champions League close bodies (`cup-odds-history-`) can't be archived. Both
  went more than 7 days without a run that used them.
- **What they produced is safe on data-log:** `backtest/E0_dk.json`, `E0_dk_trades.csv`, and
  `odds_history/UCL_closes.jsonl` with `UCL_close_calls.jsonl`. Nothing live reads the raw bodies.
- **What re-running would cost:** the DraftKings backtest's download would buy its snapshots
  again, but `max_credits` defaults to 0, so nothing is bought unless the lead sets a cap. The
  Champions League closes are never bought again for a slot already held in data-log's call
  record.
- **Totals:** 3.50 GB of the 10 GB limit, in 183 caches. `raw-data-` makes up 3.2 GB of that, in
  165 entries, because every publish run saves a new one. Size wasn't what evicted the paid
  caches; going unused for 7 days was.
- **Also missing:** `understat-SP1/D1/I1/F1-` and `cup-corners-`. They stay in the keep-alive
  list, so the next save is kept alive.
- **Present:** the `comebacks-<league>-` caches (13–18 MB each), `cup-espn-` (6.9 MB) and
  `espn-timelines-` (1.4 MB).

Until 28 Oct some paid data was never cached:
- The goalscorer probe and pilot (`odds-check.yml` `goalscorer`) write under
  `data/raw/player_odds_history`, but `odds-check.yml` only restored that cache and never saved it.
  Their bodies lasted one run, plus whatever artifact the task uploaded. Now a paid `goalscorer`
  run saves `player-odds-history-`.
- The in-play probes (`inplay-probe`) printed their results and kept nothing. Now every answer they
  buy is kept under `data/raw/inplay_probe/<league>/` (`live_<time>.json`, `hist_<time>.json`) in the
  paid cache `inplay-probe-`. A historical snapshot already held is read back instead of bought.
- `backfill.yml`'s DraftKings history and `players.yml`'s FanDuel lines were saved by the combined
  `actions/cache` step, which saves only when the job succeeds, so a run that failed after buying
  lost what it bought. Both now save with an explicit save step under `always()`.

**Free** (kept alive only): `raw-data-` (`data/raw`), `cup-espn-`, `cup-corners-`,
`espn-timelines-`, `heavy-espn-`, `understat-SP1/D1/I1/F1-`, `comebacks-E0/SP1/D1/I1/F1-`. CI keeps `playwright-…`
and setup-uv's own caches in use itself.

The `usage` job prints every cache's size and when it was last used, plus the total against the
10 GB limit. A cache's size is only known from a run of `caches.yml`, because the cloud sandbox
can't read GitHub's cache list.

## Bought data can't be left out (owner, 28 Oct: "save the api data permanently")

Three tests in `tests/test_cache_archive.py` make the rule hard to forget:

- **A cache saved where the key is must be paid or declared free.** Every cache-saving step in a
  job that can receive `ODDS_API_KEY` saves either a cache in `cache_archive.PAID` (with its exact
  paths and a keep-alive row) or one in `cache_archive.FREE`, where each prefix carries the reason it
  holds nothing bought. A new fetcher that saves its bodies under a new cache fails CI until someone
  registers that cache as paid or says why it is free.
- **A paid cache restored where the key is gets saved, even on failure.** A key-holding job that
  restores a paid cache must save it with an explicit `actions/cache/save` step under `always()`,
  because a fetcher may write bought files into the restored folder (the goalscorer pilot did).
- **A paid run archives right away.** A workflow whose key-holding job saves a paid cache has an
  `archive` job that `needs` it and runs `gh workflow run caches.yml -f archive=true`. Today these are
  `backfill.yml`, `players.yml` and `odds-check.yml`. The job runs only on the default branch and
  only for a paid run (`max_credits`/`cap` not 0, not a dry run), even when the buying job failed.
  It starts after the buying job's cache saves, so a purchase reaches the release within minutes,
  not at the next twice-weekly run. Caches saved on another branch are invisible to `caches.yml`,
  so paid fetches run from the default branch (lead, 28 Oct).

What the tests can't see: a fetcher that writes bodies outside any saved folder. The convention is
that every paid fetcher writes under one of `PAID`'s paths, as each one does today.

## Live data is on data-log, not in caches

Everything the scheduled build buys live is parsed into data-log, which is permanent git history:

- DraftKings h2h (and, from PR #125, every book in the call): `odds_log/`.
- Team totals and the match totals: `odds_log/<code>_team_totals_*`.
- Team corners: `odds_log/<code>_corners_*`.
- Their call records.
- The half-time in-play snapshots, when they start: `odds_log/<code>_inplay_*`.
- Paper ledgers, team news and the bought Champions League closes (`odds_history/UCL_closes.jsonl`).

The raw live answers sit only in the free `raw-data-` cache, but every price in them is already in
those logs.

**One exception: the live FanDuel player lines.** `publish.py` fetches them near kickoff (E0;
`player_odds.fetch_live`, `data/raw/player_odds/<league>/`). They go into `data.json` and the
`raw-data-` cache, but not to data-log. Player shots are parked and no player paper trade opens, so
nothing reads them later. Their history lasts only as long as the cache. Logging them to data-log
is the player-props team's call.

## How it works (`.github/workflows/caches.yml`)

- **Keep-alive** runs on Mondays and Thursdays at 04:23 UTC, and on dispatch. Twice a week gives
  a margin against the 7-day eviction when a scheduled run starts late or is dropped; the free
  caches have no archive to fall back on. It restores each cache under its
  exact key prefix and paths, since a restore counts as a use. It logs hit or miss, files and size.
  A paid cache that missed raises a warning: the archive covers it.
- **Archive.** Each paid cache is packed as a deterministic tar + zstd (sorted names, fixed times
  and owners).
  - **Where:** release assets on the tag `paid-odds-archive`. They are permanent, take up to 2 GB a
    file (a bigger pack is split into parts), and stay out of git history. The paid caches are far
    over the ~50 MB that would fit comfortably in data-log.
  - **Manifest:** on data-log at `archive/manifest.json`. Per cache it lists the files, bytes,
    `by_path` counts, the source cache key, the date, `content_sha256`, and each part's size and
    sha256.
  - **When an asset is replaced:** only when `content_sha256` changes. That digest covers every
    file's path, size and sha256, so a re-run with the same files replaces nothing. A cache that
    missed or is empty never replaces its asset.
  - **Verify:** the `verify` job downloads every asset back and checks its sha256.
- **One job does both.** The keep-alive already restores the paid caches, so it also
  refreshes the archive (uploading only on a change). There is no separate archive schedule.
- **Fallback on a miss** (`.github/actions/paid-archive-restore`). It runs right after every step
  that restores a paid cache: 11 steps in `backfill.yml`, `odds-check.yml` and `players.yml`.
  - If any archived path has fewer files on disk than the archive holds, it downloads that cache's
    assets, checks every sha256 against the manifest, and extracts the missing files. Files already
    on disk are kept.
  - A bad checksum, or files still missing afterwards, fails the job before any step that could buy.
  - With no manifest yet, it does nothing.
  - A workflow that saves the cache afterwards (`backfill.yml`, `players.yml`) saves the rebuilt
    copy, which heals the cache.
- **Forced-miss test:** dispatch with `test_fallback`. Each paid cache is restored under a dummy key
  (a guaranteed miss), rebuilt from the release, then checked file for file against the manifest.
  A push to a `team/` or `lead/` branch that edits the workflow runs the same test against that
  run's own packs, as a dry run with no upload and no data-log write.

Tests: `tests/test_cache_archive.py` covers the manifest and checksum code, a real tar + zstd
round trip, every cache step in the workflows being kept alive with its exact paths, the paid
registry matching the workflow, a fallback after every paid restore, and the three rules above.
