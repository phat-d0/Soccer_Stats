import numpy as np
import pytest

from soccer_stats.odds import devig_proportional, devig_shin, edge, kelly, overround


def test_overround():
    assert overround(np.array([2.0, 2.0])) == pytest.approx(0.0)
    assert overround(np.array([1.9, 1.9])) == pytest.approx(2 / 1.9 - 1)


@pytest.mark.parametrize("fn", [devig_proportional, devig_shin])
def test_devig_sums_to_one(fn):
    p = fn(np.array([1.5, 4.2, 7.0]))
    assert p.sum() == pytest.approx(1.0)
    assert np.all(np.diff(p) < 0)


def test_shin_shades_longshots_more():
    odds = np.array([1.5, 4.2, 7.0])
    assert devig_shin(odds)[-1] < devig_proportional(odds)[-1]


def test_edge_and_kelly():
    assert edge(0.55, 2.0) == pytest.approx(0.10)
    assert kelly(0.55, 2.0, fraction=1.0) == pytest.approx(0.10)
    assert kelly(0.40, 2.0) == 0.0
