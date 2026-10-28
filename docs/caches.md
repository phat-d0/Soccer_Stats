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

Some paid data was never cached:
- The goalscorer probe and pilot (`odds-check.yml` `goalscorer`) write under
  `data/raw/player_odds_history`, but `odds-check.yml` only restores that cache and never saves it.
  Their bodies lasted one run, plus whatever artifact the task uploaded.
- The in-play probes (`inplay-probe`) print their results and keep nothing.

**Free** (kept alive only): `raw-data-` (`data/raw`), `cup-espn-`, `cup-corners-`,
`espn-timelines-`, `understat-SP1/D1/I1/F1-`, `comebacks-E0/SP1/D1/I1/F1-`. CI keeps `playwright-…`
and setup-uv's own caches in use itself.

The `usage` job prints every cache's size and when it was last used, plus the total against the
10 GB limit. A cache's size is only known from a run of `caches.yml`, because the cloud sandbox
can't read GitHub's cache list.

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
  that restores a paid cache: 8 steps in `backfill.yml`, `odds-check.yml` and `players.yml`.
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
registry matching the workflow, and a fallback after every paid restore.
