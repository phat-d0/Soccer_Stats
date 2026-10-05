"""Premier League predictions dashboard.

Run locally:  uv run --extra app streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from soccer_stats import dashboard
from soccer_stats.data import current_season, load_fixtures, load_matches

LEAGUE = "E0"
TRAIN_SEASONS = 3  # current season plus two before it
BLUE = "#2a78d6"
BLUE_RAMP = ["#f4f8fd", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]

st.set_page_config(page_title="Premier League Model", page_icon="⚽", layout="wide")


# ---------- data (cached) ----------


@st.cache_data(ttl=6 * 3600, show_spinner="Loading results and odds…")
def load_data():
    season = current_season()
    matches = load_matches([LEAGUE], range(season - TRAIN_SEASONS + 1, season + 1))
    try:
        fixtures = load_fixtures([LEAGUE])
    except Exception:  # fixtures file is optional; the app still works without it
        fixtures = pd.DataFrame(columns=["kickoff", "home", "away"])
    return matches, fixtures, pd.Timestamp.now()


@st.cache_resource(ttl=6 * 3600, show_spinner="Fitting model…")
def get_model(_matches: pd.DataFrame, key: str):
    return dashboard.fit_model(_matches)


@st.cache_data(ttl=12 * 3600, show_spinner="Replaying past matches (first load only)…")
def get_replay(_matches: pd.DataFrame, key: str, start: pd.Timestamp):
    return dashboard.replay(_matches, start)


def pct(x: float) -> str:
    return "–" if pd.isna(x) else f"{x:.0%}"


def odds(x: float) -> str:
    return "–" if pd.isna(x) else f"{x:.2f}"


def heatmap(matrix: np.ndarray, home: str, away: str, max_goals: int = 5) -> go.Figure:
    m = matrix[: max_goals + 1, : max_goals + 1] * 100
    fig = go.Figure(
        go.Heatmap(
            z=m,
            x=list(range(max_goals + 1)),
            y=list(range(max_goals + 1)),
            colorscale=[[i / (len(BLUE_RAMP) - 1), c] for i, c in enumerate(BLUE_RAMP)],
            text=[[f"{v:.0f}%" if v >= 1 else "" for v in row] for row in m],
            texttemplate="%{text}",
            hovertemplate=f"{home} %{{y}} – %{{x}} {away}<br>%{{z:.1f}}%<extra></extra>",
            showscale=False,
            xgap=2,
            ygap=2,
        )
    )
    fig.update_layout(
        height=300,
        margin=dict(l=0, r=0, t=10, b=0),
        xaxis=dict(title=f"{away} goals", side="top", dtick=1),
        yaxis=dict(title=f"{home} goals", autorange="reversed", dtick=1),
    )
    return fig


def match_card(model, home: str, away: str, row: pd.Series | None = None) -> None:
    pred = dashboard.predict_match(model, home, away)
    c1, c2 = st.columns([1, 1])
    with c1:
        st.markdown(
            f"**Expected goals:** {home} {pred['xg_home']:.2f} – {pred['xg_away']:.2f} {away}"
        )
        probs = pd.DataFrame(
            {
                "Model": [
                    pred["p_home"],
                    pred["p_draw"],
                    pred["p_away"],
                    pred["p_over25"],
                    1 - pred["p_over25"],
                    pred["p_btts"],
                ],
            },
            index=[f"{home} win", "Draw", f"{away} win", "Over 2.5", "Under 2.5", "Both score"],
        )
        probs["Fair odds"] = 1 / probs["Model"]
        if row is not None:
            probs["Pinnacle"] = [
                row.get(c)
                for c in ("odds_home", "odds_draw", "odds_away", "odds_over25", "odds_under25")
            ] + [np.nan]
            probs["Pinnacle"] = pd.to_numeric(probs["Pinnacle"], errors="coerce")
            probs["Edge"] = probs["Model"] * probs["Pinnacle"] - 1
        st.dataframe(
            probs.style.format(
                {
                    "Model": pct,
                    "Fair odds": odds,
                    "Pinnacle": odds,
                    "Edge": lambda v: "–" if pd.isna(v) else f"{v:+.1%}",
                },
                na_rep="–",
            ),
            width="stretch",
        )
        st.caption(
            "Most likely scores: "
            + ", ".join(f"{s} ({p:.0%})" for s, p in dashboard.top_scorelines(pred["matrix"]))
        )
    with c2:
        st.plotly_chart(
            heatmap(pred["matrix"], home, away), width="stretch", key=f"hm-{home}-{away}"
        )


# ---------- page ----------

matches, fixtures, loaded_at = load_data()
data_key = f"{len(matches)}-{matches['date'].max():%Y%m%d}"
model = get_model(matches, data_key)
season = current_season()
season_matches = matches[matches["date"] >= f"{season}-07-01"]
teams = sorted(
    set(season_matches["home"]) | set(fixtures["home"]) | set(fixtures["away"])
) or sorted(model.teams)
counts = dashboard.match_counts(matches, teams)

with st.sidebar:
    st.header("Settings")
    min_edge = st.slider(
        "Minimum edge to flag a bet",
        0.0,
        0.15,
        0.03,
        0.01,
        format="%.2f",
        help="Model probability × odds − 1. 0.03 = 3% expected return.",
    )
    if st.button("Refresh data now"):
        load_data.clear()
        st.rerun()
    st.caption(
        f"Data loaded {loaded_at:%d %b %H:%M}. Last result in data: "
        f"{matches['date'].max():%d %b %Y}. Model fit on {len(matches)} matches."
    )
    st.divider()
    st.caption(
        "Odds are Pinnacle prices from football-data.co.uk. An edge only matters if it holds "
        "up in the Track record tab: if the model doesn't beat the closing line there, "
        "treat its picks as entertainment."
    )

st.title("⚽ Premier League model")

tab_up, tab_ratings, tab_record, tab_explore = st.tabs(
    ["Upcoming matches", "Team ratings", "Track record", "Match explorer"]
)

# --- Upcoming ---
with tab_up:
    if fixtures.empty:
        st.info("No upcoming fixtures published yet. Try the Match explorer tab.")
    else:
        preds = dashboard.predict_fixtures(model, fixtures, counts)
        flagged = preds[preds["pick_edge"] >= max(min_edge, 1e-9)]
        st.metric(
            "Value bets this round",
            f"{len(flagged)} of {len(preds)} matches",
            help=f"Matches where the model's best edge is at least {min_edge:.0%}",
        )

        table = pd.DataFrame(
            {
                "Match": preds["home"] + " v " + preds["away"],
                "Value pick": [
                    f"{r.pick.title()} @ {r.pick_odds:.2f} ({r.pick_edge:+.1%})"
                    if r.pick and r.pick_edge >= min_edge
                    else "–"
                    for r in preds.itertuples()
                ],
                "Kick-off": preds["kickoff"].dt.strftime("%a %d %b %H:%M"),
                "Home": preds["p_home"].map(pct),
                "Draw": preds["p_draw"].map(pct),
                "Away": preds["p_away"].map(pct),
                "Over 2.5": preds["p_over25"].map(pct),
                "Odds H/D/A": [
                    f"{odds(r.odds_home)} / {odds(r.odds_draw)} / {odds(r.odds_away)}"
                    for r in preds.itertuples()
                ],
                "Note": np.where(preds["low_data"], "⚠ few matches for one team", ""),
            }
        )
        st.dataframe(table, hide_index=True, width="stretch")

        st.subheader("Match details")
        for _, r in preds.iterrows():
            label = f"{r['home']} v {r['away']} · {r['kickoff']:%a %d %b %H:%M}"
            if r["pick"] and r["pick_edge"] >= min_edge:
                label += f" · value: {r['pick']} ({r['pick_edge']:+.1%})"
            with st.expander(label):
                match_card(model, r["home"], r["away"], r)

# --- Ratings ---
with tab_ratings:
    ratings = dashboard.team_ratings(model, counts, teams)
    st.caption(
        "Goals each team would score and concede per game against an average Premier League "
        "side on a neutral pitch. Recent matches count more."
    )
    c1, c2 = st.columns([1, 1])
    with c1:
        shown = ratings.rename(
            columns={
                "team": "Team",
                "goals_for": "Scores",
                "goals_against": "Concedes",
                "goal_diff": "Net",
                "matches": "Matches in data",
            }
        )
        st.dataframe(
            shown.style.format({"Scores": "{:.2f}", "Concedes": "{:.2f}", "Net": "{:+.2f}"}),
            width="stretch",
            height=740,
        )
    with c2:
        fig = go.Figure(
            go.Scatter(
                x=ratings["goals_for"],
                y=ratings["goals_against"],
                mode="markers+text",
                text=ratings["team"],
                textposition=[
                    "top center" if i % 2 else "bottom center" for i in range(len(ratings))
                ],
                textfont=dict(size=11),
                marker=dict(size=10, color=BLUE, line=dict(width=2, color="rgba(255,255,255,0.9)")),
                hovertemplate="%{text}<br>Scores %{x:.2f} · Concedes %{y:.2f}<extra></extra>",
            )
        )
        fig.update_layout(
            height=740,
            margin=dict(l=0, r=0, t=30, b=0),
            title=dict(text="Attack vs defence (top right is best)", font=dict(size=14)),
            xaxis=dict(title="Goals scored per game →"),
            yaxis=dict(title="Goals conceded per game (fewer is higher)", autorange="reversed"),
        )
        st.plotly_chart(fig, width="stretch")

# --- Track record ---
with tab_record:
    st.caption(
        "The model replayed week by week over last season and this one, using only data "
        "available before each match. Bets are 1 unit at Pinnacle opening odds whenever the "
        "edge clears your threshold."
    )
    start = pd.Timestamp(f"{season - 1}-08-01")
    rec = dashboard.track_record(get_replay(matches, data_key, start), min_edge)
    if not rec["summary"]:
        st.info("Not enough data to replay yet.")
    else:
        s, sc = rec["summary"], rec["scores"]
        c = st.columns(5)
        c[0].metric("Bets", s["bets"])
        c[1].metric("Profit (units)", f"{s.get('profit', 0):+.1f}")
        c[2].metric("ROI", f"{s.get('roi', 0):+.1%}")
        c[3].metric(
            "Avg closing line value",
            f"{s.get('avg_clv', np.nan):+.1%}",
            help="How much better your odds were than the fair closing price. "
            "Consistently positive is the best sign of a real edge.",
        )
        c[4].metric("Beat the close", f"{s.get('pct_beat_close', np.nan):.0%}")

        if {"model", "market"} <= set(sc.index):
            gap = sc.loc["model", "log_loss"] - sc.loc["market", "log_loss"]
            verdict = "better than" if gap < 0 else "worse than"
            st.markdown(
                f"**Accuracy:** the model's predictions were {verdict} the closing odds "
                f"(log loss {sc.loc['model', 'log_loss']:.4f} vs "
                f"{sc.loc['market', 'log_loss']:.4f}, lower is better, "
                f"over {int(sc.loc['model', 'n'])} matches)."
            )

        bets = rec["bets"]
        daily = bets.groupby(bets["date"].dt.normalize()).agg(
            profit=("profit", "sum"), n=("profit", "size")
        )
        daily["cum"] = daily["profit"].cumsum()
        fig = go.Figure(
            go.Scatter(
                x=daily.index,
                y=daily["cum"],
                mode="lines",
                line=dict(color=BLUE, width=2, shape="hv"),
                customdata=np.stack([daily["n"], daily["profit"]], axis=1),
                hovertemplate="%{x|%d %b %Y}<br>%{customdata[0]} bets, %{customdata[1]:+.2f} units"
                "<br>Running total %{y:+.1f} units<extra></extra>",
            )
        )
        fig.add_hline(y=0, line_width=1, line_color="rgba(128,128,128,0.6)")
        fig.update_layout(
            height=340,
            margin=dict(l=0, r=0, t=30, b=0),
            title=dict(text="Running profit (units)", font=dict(size=14)),
        )
        st.plotly_chart(fig, width="stretch")

        with st.expander(f"All {len(bets)} bets"):
            st.dataframe(
                bets.sort_values("date", ascending=False)[
                    ["date", "home", "away", "pick", "price", "model_p", "edge", "clv", "profit"]
                ].style.format(
                    {
                        "date": "{:%d %b %Y}",
                        "price": "{:.2f}",
                        "model_p": "{:.0%}",
                        "edge": "{:+.1%}",
                        "clv": "{:+.1%}",
                        "profit": "{:+.2f}",
                    }
                ),
                hide_index=True,
                width="stretch",
            )

# --- Explorer ---
with tab_explore:
    c1, c2 = st.columns(2)
    home = c1.selectbox("Home team", teams, index=0)
    away = c2.selectbox("Away team", [t for t in teams if t != home], index=0)
    match_card(model, home, away)
