import pandas as pd

from soccer_stats.data import normalize, season_code


def test_season_code():
    assert season_code(2023) == "2324"
    assert season_code(1999) == "9900"


def test_normalize_handles_missing_columns_and_blank_rows():
    raw = pd.DataFrame(
        {
            "Div": ["E0", "E0", None],
            "Date": ["12/08/2023", "13/08/23", None],
            "HomeTeam": ["Arsenal", "Brentford", None],
            "AwayTeam": ["Forest", "Tottenham", None],
            "FTHG": [2, 2, None],
            "FTAG": [1, 2, None],
            "PSH": [1.3, 3.1, None],
            "PSD": [6.0, 3.6, None],
            "PSA": [11.0, 2.3, None],
        }
    )
    df = normalize(raw, league="E0", season="2324")
    assert len(df) == 2
    assert df["date"].iloc[1] == pd.Timestamp("2023-08-13")
    assert df["close_home"].isna().all()
    assert df["home_goals"].dtype.kind == "i"
