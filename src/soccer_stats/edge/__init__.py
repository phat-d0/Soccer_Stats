"""Edge research: where could a durable betting edge exist?

Analysis helpers only (no model changes). Findings live in docs/edge.md.

* stats: bootstrap ranges (optionally clustered) and multiple-testing helpers.
* books: football-data.co.uk bookmaker prices, margins by book, and the match trade rule
  replayed at each book's price (line shopping) with closing line value.
* props: which bookmakers price both sides of a player-prop line, and their margins.
* fanduel: FanDuel's over-only player lines sliced by line, price, player and timing.
* run: the command-line entry (`python -m soccer_stats.edge.run ...`), used by the
  odds-check workflow.
"""
