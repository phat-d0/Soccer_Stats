import numpy as np
import pytest

from soccer_stats.markets import asian_handicap_home, btts, match_odds, over_under


@pytest.fixture
def m():
    rng = np.random.default_rng(1)
    m = rng.random((6, 6))
    return m / m.sum()


def test_markets_are_distributions(m):
    for p in (match_odds(m), over_under(m), btts(m)):
        assert p.sum() == pytest.approx(1.0)


def test_handicap_minus_half_is_home_win(m):
    assert asian_handicap_home(m, -0.5) == pytest.approx(match_odds(m)[0])
    assert asian_handicap_home(m, 0.5) == pytest.approx(match_odds(m)[:2].sum())
    with pytest.raises(ValueError):
        asian_handicap_home(m, -1.0)
